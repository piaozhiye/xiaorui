#!/usr/bin/env python3
"""RKLLM 本地大模型服务(板上运行): ctypes 绑定 librkllmrt.so + OpenAI 兼容 SSE.

给 xiaozhi 服务端当离线大脑: 无需任何网络, qwen2.5-1.5B 在 NPU 上推理.
服务端 LLM 配置指向 http://127.0.0.1:8112/v1 即可(与 llm_stub.py 同接口).

用法: python3 llm_rkllm.py [--model /data/xiaorui-server/qwen25-1.5b-rk3576-w4a16.rkllm]
首次加载模型约 20-40 s, 出现 ready 后再接入服务端.
"""
import argparse
import fcntl
import glob
from contextlib import contextmanager
import ctypes
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# RK3562: RKLLM 与 RockX 并行推理会改变 TTS 原始波形。
# 通过同一文件的 flock 协调独立进程；未配置时保持其他平台的行为。
@contextmanager
def npu_exclusive():
    path = os.environ.get("XR_NPU_LOCK_FILE")
    if not path:
        yield
        return
    with open(path, "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

def tts_waiting():
    path = os.environ.get("XR_NPU_LOCK_FILE")
    if not path:
        return False
    waiting = False
    for marker in glob.glob(path + ".tts-*.pending"):
        try:
            pid = int(marker.rsplit(".tts-", 1)[1].split(".", 1)[0])
            os.kill(pid, 0)
            waiting = True
        except ProcessLookupError:
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass
        except (ValueError, FileNotFoundError):
            continue
        except PermissionError:
            waiting = True
    return waiting


PORT = 8112
RUN_LOCK = threading.Lock()  # RKLLM 句柄不支持并发推理


# ---------------- ctypes 绑定 librkllmrt ----------------
class RKLLMExtendParam(ctypes.Structure):
    _fields_ = [
        ("base_domain_id", ctypes.c_int32),
        ("embed_flash", ctypes.c_int8),
        ("enabled_cpus_num", ctypes.c_int8),
        ("enabled_cpus_mask", ctypes.c_uint32),
        ("n_batch", ctypes.c_uint8),
        ("use_cross_attn", ctypes.c_int8),
        ("reserved", ctypes.c_int8 * 104),
    ]


class RKLLMParam(ctypes.Structure):
    _fields_ = [
        ("model_path", ctypes.c_char_p),
        ("max_context_len", ctypes.c_int32),
        ("max_new_tokens", ctypes.c_int32),
        ("top_k", ctypes.c_int32),
        ("n_keep", ctypes.c_int32),
        ("top_p", ctypes.c_float),
        ("temperature", ctypes.c_float),
        ("repeat_penalty", ctypes.c_float),
        ("frequency_penalty", ctypes.c_float),
        ("presence_penalty", ctypes.c_float),
        ("mirostat", ctypes.c_int32),
        ("mirostat_tau", ctypes.c_float),
        ("mirostat_eta", ctypes.c_float),
        ("skip_special_token", ctypes.c_bool),
        ("ignore_eos_token", ctypes.c_bool),
        ("is_async", ctypes.c_bool),
        ("extend_param", RKLLMExtendParam),
    ]


class RKLLMResultLastHiddenLayer(ctypes.Structure):
    _fields_ = [("hidden_states", ctypes.POINTER(ctypes.c_float)),
                ("embd_size", ctypes.c_int),
                ("num_tokens", ctypes.c_int)]


class RKLLMResultLogits(ctypes.Structure):
    _fields_ = [("logits", ctypes.POINTER(ctypes.c_float)),
                ("vocab_size", ctypes.c_int),
                ("num_tokens", ctypes.c_int)]


class RKLLMPerfStat(ctypes.Structure):
    _fields_ = [("prefill_time_ms", ctypes.c_float), ("prefill_tokens", ctypes.c_int),
                ("generate_time_ms", ctypes.c_float), ("generate_tokens", ctypes.c_int),
                ("memory_usage_mb", ctypes.c_float)]


class RKLLMResult(ctypes.Structure):
    _fields_ = [("text", ctypes.c_char_p), ("token_id", ctypes.c_int32),
                ("last_hidden_layer", RKLLMResultLastHiddenLayer),
                ("logits", RKLLMResultLogits), ("perf", RKLLMPerfStat)]


LLMResultCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(RKLLMResult),
                                     ctypes.c_void_p, ctypes.c_int)


class RKLLMCallback(ctypes.Structure):
    _fields_ = [("result_callback", LLMResultCallback), ("result_userdata", ctypes.c_void_p),
                ("tokenizer_callback", ctypes.c_void_p), ("tokenizer_userdata", ctypes.c_void_p),
                ("embed_callback", ctypes.c_void_p), ("embed_userdata", ctypes.c_void_p)]


class RKLLMTokenInput(ctypes.Structure):
    _fields_ = [("input_ids", ctypes.POINTER(ctypes.c_int32)), ("n_tokens", ctypes.c_size_t)]


class RKLLMEmbedInput(ctypes.Structure):
    _fields_ = [("embed", ctypes.POINTER(ctypes.c_float)), ("n_tokens", ctypes.c_size_t)]


class RKLLMImageInput(ctypes.Structure):
    _fields_ = [("image_embed", ctypes.POINTER(ctypes.c_float)),
                ("n_image_tokens", ctypes.c_size_t), ("n_image", ctypes.c_size_t),
                ("image_start", ctypes.c_char_p), ("image_end", ctypes.c_char_p),
                ("image_content", ctypes.c_char_p),
                ("image_width", ctypes.c_size_t), ("image_height", ctypes.c_size_t)]


class RKLLMVideoInput(ctypes.Structure):
    _fields_ = [("video_embed", ctypes.POINTER(ctypes.c_float)),
                ("n_video_tokens", ctypes.c_size_t), ("n_frame_tokens", ctypes.c_size_t),
                ("n_video", ctypes.c_size_t), ("n_frame", ctypes.c_size_t)]


class RKLLMAudioInput(ctypes.Structure):
    _fields_ = [("audio_embed", ctypes.POINTER(ctypes.c_float)),
                ("n_audio_tokens", ctypes.c_size_t), ("n_audio", ctypes.c_size_t),
                ("audio_start", ctypes.c_char_p), ("audio_end", ctypes.c_char_p),
                ("audio_content", ctypes.c_char_p)]


class RKLLMMultimodalInput(ctypes.Structure):
    _fields_ = [("prompt", ctypes.c_char_p), ("image", RKLLMImageInput),
                ("video", RKLLMVideoInput), ("audio", RKLLMAudioInput)]


class RKLLMInput(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("prompt_input", ctypes.c_char_p), ("token_input", RKLLMTokenInput),
                    ("embed_input", RKLLMEmbedInput), ("multimodal_input", RKLLMMultimodalInput)]
    _fields_ = [("role", ctypes.c_char_p), ("enable_thinking", ctypes.c_bool),
                ("input_type", ctypes.c_int32), ("u", _U)]


class RKLLMInferParam(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int32), ("lora_params", ctypes.c_void_p),
                ("prompt_cache_params", ctypes.c_void_p), ("sampling_params", ctypes.c_void_p),
                ("keep_history", ctypes.c_int), ("max_new_tokens", ctypes.c_int32)]


RKLLM_RUN_NORMAL, RKLLM_RUN_WAITING, RKLLM_RUN_FINISH, RKLLM_RUN_ERROR = 0, 1, 2, 3
HANDLE_T = ctypes.c_void_p


class RkLlm:
    def __init__(self, model_path, system_prompt):
        lib = ctypes.CDLL("/data/xiaorui-server/librkllmrt.so")
        lib.rkllm_createDefaultParam.restype = RKLLMParam
        lib.rkllm_init.argtypes = [ctypes.POINTER(HANDLE_T), ctypes.POINTER(RKLLMParam),
                                   ctypes.POINTER(RKLLMCallback)]
        lib.rkllm_run.argtypes = [HANDLE_T, ctypes.POINTER(RKLLMInput),
                                  ctypes.POINTER(RKLLMInferParam), ctypes.c_void_p]
        lib.rkllm_is_running.argtypes = [HANDLE_T]
        lib.rkllm_abort.argtypes = [HANDLE_T]
        lib.rkllm_clear_kv_cache.argtypes = [HANDLE_T, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_void_p]
        self.lib = lib

        param = lib.rkllm_createDefaultParam()
        param.model_path = model_path.encode()
        param.max_context_len = 2048
        param.max_new_tokens = 256
        param.top_k = 1
        param.top_p = 0.9
        param.temperature = 0.7
        param.skip_special_token = True

        self.q = queue.Queue()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.cooperative = os.environ.get("XR_NPU_COOPERATIVE", "0") == "1"
        self.generated_tokens = 0
        self.last_token = -1
        self.pause_requested = False
        self.token_budget = 256

        @LLMResultCallback
        def on_result(result, _userdata, state):
            try:
                if state in (RKLLM_RUN_NORMAL, RKLLM_RUN_WAITING):
                    if result and result.contents.token_id >= 0:
                        self.last_token = result.contents.token_id
                        self.generated_tokens += 1
                    text = result.contents.text if result else None
                    if text:
                        self.q.put(text.decode("utf-8", "ignore"))
                    # 只有已有完整文本回调时暂停，避免在 EOS 或半个 UTF-8 字符上续写。
                    if self.cancel.is_set() or (self.cooperative and text
                            and self.generated_tokens < self.token_budget and tts_waiting()):
                        self.pause_requested = True
                        return 1
                elif state == RKLLM_RUN_FINISH:
                    self.done.set()
                elif state == RKLLM_RUN_ERROR:
                    self.q.put("[RKLLM_ERROR]")
                    self.done.set()
            except Exception:
                pass
            return 0

        self.cb = on_result  # 防 GC
        cb = RKLLMCallback(result_callback=self.cb, result_userdata=None,
                           tokenizer_callback=None, tokenizer_userdata=None,
                           embed_callback=None, embed_userdata=None)
        self.handle = HANDLE_T()
        with npu_exclusive():
            st = lib.rkllm_init(ctypes.byref(self.handle), ctypes.byref(param), ctypes.byref(cb))
        if st != 0:
            raise RuntimeError(f"rkllm_init failed: {st}")
        lib.rkllm_set_chat_template(
            self.handle,
            system_prompt.encode(), b"<|im_start|>user\n", b"<|im_end|>\n<|im_start|>assistant\n")
        print("[rkllm] model ready", flush=True)

    def chat(self, user_text):
        with RUN_LOCK:
            yield from self._chat_locked(user_text)

    def _chat_locked(self, user_text):
        """阻塞生成, 逐 token 产出(yield)."""
        inp = RKLLMInput()
        inp.role = b"user"
        inp.enable_thinking = False
        inp.input_type = 0  # RKLLM_INPUT_PROMPT
        inp.u.prompt_input = user_text.encode("utf-8")
        infer = RKLLMInferParam()
        infer.mode = 0  # GENERATE
        infer.lora_params = None
        infer.prompt_cache_params = None
        infer.sampling_params = None
        infer.keep_history = 1
        infer.max_new_tokens = 256

        self.done.clear()
        self.cancel.clear()
        self.generated_tokens = 0
        self.last_token = -1
        self.token_budget = 256
        self.q = queue.Queue()
        run_err = []

        def runner():
            nonlocal inp
            # 恢复时需要最后一个已采样 token（不是空输入），保留其 Python 引用。
            resume_tokens = None
            try:
                while not self.cancel.is_set():
                    self.pause_requested = False
                    with npu_exclusive():
                        if self.cancel.is_set():
                            break
                        st = self.lib.rkllm_run(self.handle, ctypes.byref(inp), ctypes.byref(infer), None)
                        if self.lib.rkllm_is_running(self.handle):
                            self.lib.rkllm_abort(self.handle)
                            raise RuntimeError("rkllm_run returned before native task stopped")
                    if st:
                        raise RuntimeError(f"rkllm_run failed: {st}")
                    if not self.pause_requested or self.cancel.is_set():
                        break
                    wait_start = time.monotonic()
                    while tts_waiting() and not self.cancel.is_set():
                        if time.monotonic() - wait_start > 60:
                            raise RuntimeError("TTS NPU turn exceeded 60s")
                        time.sleep(0.005)
                    if self.cancel.is_set():
                        break
                    print(f"[rkllm] cooperative yield tokens={self.generated_tokens} "
                          f"wait={time.monotonic()-wait_start:.3f}s", flush=True)
                    if self.last_token < 0 or self.generated_tokens >= self.token_budget:
                        break
                    resume_tokens = (ctypes.c_int32 * 1)(self.last_token)
                    inp = RKLLMInput()
                    inp.role = b"user"
                    inp.input_type = 1  # RKLLM_INPUT_TOKEN：续上最后一个已采样 token。
                    inp.u.token_input = RKLLMTokenInput(resume_tokens, 1)
                    infer.max_new_tokens = self.token_budget - self.generated_tokens
            except Exception as exc:
                run_err.append(str(exc))
            finally:
                self.done.set()

        worker = threading.Thread(target=runner, daemon=True)
        worker.start()
        try:
            while True:
                try:
                    piece = self.q.get(timeout=0.05)
                    if piece == "[RKLLM_ERROR]":
                        raise RuntimeError("rkllm inference error")
                    yield piece
                except queue.Empty:
                    if self.done.is_set():
                        break
            if run_err:
                raise RuntimeError(run_err[0])
        finally:
            self.cancel.set()
            # RUN_LOCK 在 runner 收尾之前不能让下一轮重置上述回调状态。
            if worker.is_alive() and self.lib.rkllm_is_running(self.handle):
                self.lib.rkllm_abort(self.handle)
            worker.join()


PERSONA = os.environ.get("XR_RKLLM_PERSONA") or (
    "你是小瑞，运行在 RK3576 开发板上的端侧语音助手，大脑在本机 NPU 上本地推理。"
    "回答控制在两三句话内，适合语音播报，不要用列表。"
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rkllm: RkLlm = None

    def log_message(self, fmt, *args):
        print(f"[rkllm] {fmt % args}", flush=True)

    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        user_text = ""
        for m in reversed(req.get("messages", [])):
            if m.get("role") == "user":
                user_text = str(m.get("content", "")).strip()
                break
        print(f"[rkllm] chat: {user_text!r}", flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(b: bytes):
            self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")

        def sse(text):
            return ("data: " + json.dumps(
                {"id": "rkllm", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                ensure_ascii=False) + "\n\n").encode("utf-8")

        try:
            for piece in self.rkllm.chat(user_text):
                if piece:
                    chunk(sse(piece))
                    self.wfile.flush()
        except Exception as e:
            print(f"[rkllm] run error: {e}", flush=True)
        chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="/data/xiaorui-server/qwen25-1.5b-rk3576-w4a16.rkllm")
    args = ap.parse_args()

    print(f"[rkllm] loading {args.model} (20-40 s)...", flush=True)
    t0 = time.time()
    Handler.rkllm = RkLlm(args.model, PERSONA)
    print(f"[rkllm] ready in {time.time()-t0:.0f} s, SSE on http://127.0.0.1:{PORT}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

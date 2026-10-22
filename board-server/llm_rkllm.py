#!/usr/bin/env python3
"""RKLLM 本地大模型服务(板上运行): ctypes 绑定 librkllmrt.so + OpenAI 兼容 SSE.

给 xiaozhi 服务端当离线大脑: 无需任何网络, qwen2.5-1.5B 在 NPU 上推理.
服务端 LLM 配置指向 http://127.0.0.1:8112/v1 即可(与 llm_stub.py 同接口).

用法: python3 llm_rkllm.py [--model /data/xiaorui-server/qwen25-1.5b-rk3576-w4a16.rkllm]
首次加载模型约 20-40 s, 出现 ready 后再接入服务端.
"""
import argparse
import ctypes
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class RKLLMInput(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("prompt_input", ctypes.c_char_p)]
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

        @LLMResultCallback
        def on_result(result, _userdata, state):
            try:
                if state in (RKLLM_RUN_NORMAL, RKLLM_RUN_WAITING):
                    if result and result.contents.text:
                        self.q.put(result.contents.text.decode("utf-8", "ignore"))
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
        run_err = []

        def runner():
            st = self.lib.rkllm_run(self.handle, ctypes.byref(inp), ctypes.byref(infer), None)
            if st != 0:
                run_err.append(st)
            self.done.set()

        threading.Thread(target=runner, daemon=True).start()
        while True:
            try:
                piece = self.q.get(timeout=0.2)
                if piece == "[RKLLM_ERROR]":
                    raise RuntimeError("rkllm inference error")
                yield piece
            except queue.Empty:
                if self.done.is_set():
                    break
        if run_err:
            raise RuntimeError(f"rkllm_run failed: {run_err[0]}")


PERSONA = (
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

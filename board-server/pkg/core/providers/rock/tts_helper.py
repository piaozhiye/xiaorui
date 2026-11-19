#!/usr/bin/env python3
"""RockTTS helper 进程: 常驻, stdin 收文本, stdout 回 24k/mono/int16 PCM.

用法(由 provider 拉起):
  tts_helper.py --lib-dir DIR --data-dir DIR --auth-config FILE
                [--alpha 1.0] [--timeout 30]

协议见 framing.py:
  请求: {"cmd":"tts","text":"...","alpha":1.0}
  响应: {"ok":true,"sample_rate":24000,"elapsed_s":..} + body=PCM
        {"ok":false,"error":".."}

模型常驻(加载 ~3.5s 只付一次); 实测首包 0.467s, 整句 0.476s(12 字)。
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import framing  # noqa: E402
import rockx  # noqa: E402

SAMPLE_RATE = 24000


class TtsEngine:
    def __init__(self, lib_dir, data_dir, auth_config):
        self.rx = rockx.load_rockx(lib_dir, "librocktts.so")
        self.cb = rockx.RKTTS_CALLBACK(self._on_audio)
        self.dummy = rockx.RESULT_CALLBACK(lambda *a: None)

        self.handle = self.rx.create()
        self.rx.set_str(self.handle, None, rockx.ROCKX_KEY_MODULE_NAME,
                        "RKTTSPipelineV1")
        self.rx.set_str(self.handle, None, rockx.ROCKX_KEY_DATA_PATH, data_dir)
        self.rx.set_str(self.handle, None, rockx.ROCKX_KEY_AUTH_CONFIG, auth_config)
        # 注意: node 必须是 RKTTSPipelineV1(demo 源码里的 node 名是错的)
        self.rx.set_ptr(self.handle, "RKTTSPipelineV1", "RKTTSPipelineV1Callback",
                        rockx.ctypes.cast(self.cb, rockx.ctypes.c_void_p), 0)
        self.rx.init(self.handle)
        self.rx.set_output_info(self.handle, "out", ["wav_data"],
                                rockx.ctypes.cast(self.dummy, rockx.ctypes.c_void_p))

        self._chunks = []
        self._last_frame = False
        self._done = threading.Event()

    def _on_audio(self, wav_data, wav_len, is_last_frame, userdata):
        if wav_len > 0:
            self._chunks.append(rockx.ctypes.string_at(wav_data, wav_len * 2))
        if is_last_frame:
            self._last_frame = True
            self._done.set()

    def synthesize(self, text, alpha, timeout_s):
        if not text.strip():
            raise rockx.RockXError("空文本")
        self._chunks = []
        self._last_frame = False
        self._done.clear()
        t0 = time.monotonic()

        inp = self.rx.input_create(self.handle)
        pkt0 = self.rx.input_get_packet(inp, 0)
        rc = self.rx.packet_set_string(pkt0, text)
        if rc != rockx.ROCKX_RET_SUCCESS:
            raise rockx.RockXError(f"设置文本失败 ret={rc}")
        pkt1 = self.rx.input_get_packet(inp, 1)
        rc = self.rx.packet_set_float(pkt1, float(alpha))
        if rc != rockx.ROCKX_RET_SUCCESS:
            raise rockx.RockXError(f"设置语速失败 ret={rc}")
        rc = self.rx.process_async(self.handle, inp)
        if rc != rockx.ROCKX_RET_SUCCESS:
            raise rockx.RockXError(f"RockXProcessAsync 失败 ret={rc}")

        if not self._done.wait(timeout_s):
            raise rockx.RockXError(f"TTS 超时 ({timeout_s}s，未收到末帧)")
        self.rx.wait_finish(self.handle, inp)
        self.rx.input_destroy(self.handle, inp)
        elapsed = time.monotonic() - t0
        pcm = b"".join(self._chunks)
        if not pcm:
            raise rockx.RockXError(
                f"TTS 无输出 (last_frame={self._last_frame}, {elapsed:.1f}s)")
        return pcm, elapsed



def _redirect_c_stdout_to_stderr():
    """SDK(rkauth/rockx)的日志直接写 C 层 fd1, 会污染帧协议.

    协议改走 dup 出来的 fd, fd1 重定向到 stderr —— C 库的输出全部去 stderr。
    必须在加载 .so 之前调用。
    """
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(proto_fd, "wb")



def _set_oom_adj():
    """可牺牲进程: 内存吃紧时内核 OOM killer 先杀我, 别杀网关/前端。

    helper 死了下次请求会自动重拉(HelperProcess.alive 检测),
    所以它是主链路里最该让位的那个。同罗总开发笔记那套 oom_score_adj 思路。
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("200")
    except Exception:  # noqa: BLE001
        pass


def _watch_parent_exit():
    """父进程(网关)死了就自杀: 否则变孤儿进程继续占 NPU 内存, 网关重启
    再拉一份就是双份模型, 2GB 板子直接 OOM 死机(实测)。

    注意: 不能用 PR_SET_PDEATHSIG —— 网关是从守护线程 fork 本进程的,
    那个线程 fork 完就退出了, 内核会在**线程**退出时立刻发 SIGKILL,
    helper 刚握手成功就被自己杀掉(实测: 日志"就绪"后进程秒退)。
    改用 getppid() 轮询: 父进程没了会被 init 收养, ppid 变成 1。
    """
    orig_ppid = os.getppid()

    def _watch():
        while True:
            time.sleep(2.0)
            ppid = os.getppid()
            if ppid != orig_ppid or ppid == 1:
                print("[tts_helper] 父进程已退出, 主动退出", file=sys.stderr)
                os._exit(0)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


def main():
    _set_oom_adj()
    _watch_parent_exit()
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib-dir", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--auth-config", required=True)
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    stdin = sys.stdin.buffer
    stdout = _redirect_c_stdout_to_stderr()

    try:
        engine = TtsEngine(args.lib_dir, args.data_dir, args.auth_config)
    except Exception as e:  # noqa: BLE001
        framing.send_msg(stdout, {"ok": False, "error": f"TTS init 失败: {e}"})
        return 3
    framing.send_msg(stdout, {"ok": True, "event": "ready"})

    while True:
        try:
            header, _body = framing.recv_msg(stdin)
        except framing.ProtocolError as e:
            print(f"[tts_helper] 退出: {e}", file=sys.stderr)
            return 0
        cmd = header.get("cmd")
        if cmd == "ping":
            framing.send_msg(stdout, {"ok": True, "event": "pong"})
            continue
        if cmd != "tts":
            framing.send_msg(stdout, {"ok": False, "error": f"未知命令: {cmd}"})
            continue
        text = header.get("text", "")
        alpha = header.get("alpha", args.alpha)
        try:
            pcm, elapsed = engine.synthesize(text, alpha, args.timeout)
            framing.send_msg(stdout, {"ok": True, "sample_rate": SAMPLE_RATE,
                                      "elapsed_s": round(elapsed, 3)}, body=pcm)
        except Exception as e:  # noqa: BLE001
            framing.send_msg(stdout, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return 4


if __name__ == "__main__":
    sys.exit(main())

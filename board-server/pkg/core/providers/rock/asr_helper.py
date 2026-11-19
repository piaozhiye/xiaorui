#!/usr/bin/env python3
"""RockASR helper 进程: 常驻, stdin 收 PCM 帧, stdout 回 JSON.

用法(由 provider 拉起, 不直接命令行使用):
  asr_helper.py --lib-dir DIR --data-dir DIR --auth-config FILE
                [--lang zh] [--timeout 20] [--log-file FILE]

协议见 framing.py:
  请求: {"cmd":"asr"} + body=16k/mono/int16 PCM
  响应: {"ok":true,"text":"...","elapsed_s":..} / {"ok":false,"error":".."}

模型常驻(加载 ~3s 只付一次); 每轮语音用非流式(transcribe)整段识别,
2026-11-18 实测 10s 音频 1.1s 识别完(RTF 0.11), 质量优于半流式。
"""
import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import framing  # noqa: E402
import rockx  # noqa: E402

SAMPLE_RATE = 16000


class AsrEngine:
    def __init__(self, lib_dir, data_dir, auth_config, lang="zh"):
        langs = {"zh": rockx.ROCKASR_LANG_CHINESE,
                 "en": rockx.ROCKASR_LANG_ENGLISH}
        src = langs.get(lang, rockx.ROCKASR_LANG_CHINESE)
        tgt = rockx.ROCKASR_LANG_ENGLISH if src == rockx.ROCKASR_LANG_CHINESE \
            else rockx.ROCKASR_LANG_CHINESE

        self.rx = rockx.load_rockx(
            lib_dir, "librockasr.so", extra_preload=("libonnxruntime.so",))
        self.cb = rockx.ROCKASR_RESULT_CALLBACK(self._on_result)
        self.dummy = rockx.RESULT_CALLBACK(lambda *a: None)

        init_param = rockx.RockXAsrInitParam()
        init_param.modelName = b"ASR_SS_ZH_EN"
        init_param.workMode = rockx.ROCKASR_MODE_TRANSCRIBE
        init_param.langMode.source = src
        init_param.langMode.target = tgt
        init_param.sampleRate = SAMPLE_RATE
        init_param.userdata = None
        init_param.resultCallback = rockx.ctypes.cast(self.cb, rockx.ctypes.c_void_p)
        # init_param 被 SDK 以 needRelease=1 接管, 但释放回调为 NULL:
        # 由本进程持有引用即可(进程常驻, 不重复 init)。
        self._init_param = init_param

        self.handle = self.rx.create()
        self.rx.set_str(self.handle, None, rockx.ROCKX_KEY_MODULE_NAME, "ASR")
        self.rx.set_str(self.handle, None, rockx.ROCKX_KEY_DATA_PATH, data_dir)
        self.rx.set_str(self.handle, None, rockx.ROCKX_KEY_AUTH_CONFIG, auth_config)
        self.rx.set_int(self.handle, None, "src_sample_rate", SAMPLE_RATE)
        self.rx.set_ptr(self.handle, "ASR", "param",
                        rockx.ctypes.byref(init_param), 1)
        self.rx.init(self.handle)
        self.rx.set_output_info(self.handle, "out", ["asr_result"],
                                rockx.ctypes.cast(self.dummy, rockx.ctypes.c_void_p))

        self._events = []
        self._done = threading.Event()
        self._last_event_at = 0.0

    def _on_result(self, result, count, userdata):
        now = time.monotonic()
        for i in range(count):
            r = result[i]
            text = r.result.decode("utf-8", "replace") if r.result else ""
            self._events.append((r.state, text, now))
            self._last_event_at = now
            if r.state == rockx.ASR_STATE_COMPLETE:
                self._done.set()

    def transcribe(self, pcm, timeout_s):
        if not pcm or len(pcm) < 2:
            return "", 0.0
        self._events = []
        self._done.clear()
        t0 = time.monotonic()

        inp = self.rx.input_create(self.handle)
        pkt = self.rx.input_get_packet(inp, 0)
        rc = self.rx.packet_set_pcm(pkt, pcm, SAMPLE_RATE,
                                    rockx.ROCKX_STREAM_STATE_FIRST)
        if rc != rockx.ROCKX_RET_SUCCESS:
            raise rockx.RockXError(f"设置音频包失败 ret={rc}")
        rc = self.rx.process_async(self.handle, inp)
        if rc != rockx.ROCKX_RET_SUCCESS:
            raise rockx.RockXError(f"RockXProcessAsync 失败 ret={rc}")

        # 完成判定(板上实测): COMPLETE 不一定发(transcribe 短音频),
        # 用"结果静默"兜底 —— 收到过结果且 0.8s 无新事件即认为收尾。
        deadline = t0 + timeout_s
        while time.monotonic() < deadline:
            if self._done.is_set():
                break
            if self._events and (time.monotonic() - self._last_event_at) > 0.8:
                break
            time.sleep(0.05)
        if time.monotonic() >= deadline and not self._done.is_set():
            raise rockx.RockXError(f"ASR 超时 ({timeout_s}s)")
        self.rx.input_destroy(self.handle, inp)
        elapsed = time.monotonic() - t0

        # transcribe 模式: result 字段整段累加, 取最后一个非空
        text = ""
        for state, t, _ in self._events:
            if t:
                text = t
        return text, elapsed



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
    """父进程(网关)死了就自杀: 否则变孤儿进程继续占 ~600MB NPU 内存,
    网关重启再拉一份就是双份模型, 2GB 板子直接 OOM 死机(实测)。

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
                print("[asr_helper] 父进程已退出, 主动退出", file=sys.stderr)
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
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    stdin = sys.stdin.buffer
    stdout = _redirect_c_stdout_to_stderr()

    try:
        engine = AsrEngine(args.lib_dir, args.data_dir, args.auth_config, args.lang)
    except Exception as e:  # noqa: BLE001
        framing.send_msg(stdout, {"ok": False, "error": f"ASR init 失败: {e}"})
        return 3
    framing.send_msg(stdout, {"ok": True, "event": "ready"})

    while True:
        try:
            header, body = framing.recv_msg(stdin)
        except framing.ProtocolError as e:
            print(f"[asr_helper] 退出: {e}", file=sys.stderr)
            return 0
        cmd = header.get("cmd")
        if cmd == "ping":
            framing.send_msg(stdout, {"ok": True, "event": "pong"})
            continue
        if cmd != "asr":
            framing.send_msg(stdout, {"ok": False, "error": f"未知命令: {cmd}"})
            continue
        try:
            text, elapsed = engine.transcribe(body, args.timeout)
            framing.send_msg(stdout, {"ok": True, "text": text,
                                      "elapsed_s": round(elapsed, 3)})
        except Exception as e:  # noqa: BLE001
            framing.send_msg(stdout, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return 4


if __name__ == "__main__":
    sys.exit(main())

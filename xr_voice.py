#!/usr/bin/env python3
"""麦克风语音客户端: 端侧 KWS 唤醒 -> 端侧流式 ASR -> detect+text -> TTS 播放.

状态机(半双工): 睡眠只喂 KWS(3.3M int8) -> 命中后发唤醒词、起流式识别(14M int8,
带端点检测) -> 端点静音出句发 detect -> 等 TTS 播完(期间拾音聋, 防自拾音) ->
排空回声窗口 + Reset KWS -> 回睡眠.

依赖都在服务端 venv 里: sherpa_onnx numpy websockets opuslib_next.
模型目录默认 ~/rk/models/{kws-zh-3.3M,stream-zh-14M}.

用法:
    run-client 同款环境: xz_voice.py                 # 麦克风实时
    xz_voice.py --wav a.wav b.wav                    # 离线过 KWS+ASR(回归用)
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time

import numpy as np
import sherpa_onnx

import xz_client
from xz_client import Client, log

FRAME_MS = 100
FRAME_SAMPLES = 16000 * FRAME_MS // 1000
KWS_DIR = os.path.expanduser("~/rk/models/kws-zh-3.3M")
ASR_DIR = os.path.expanduser("~/rk/models/stream-para-bilingual")  # 板上同款 232MB 双语流式
ASR_DIR_SMALL = os.path.expanduser("~/rk/models/stream-zh-14M")    # 14M zipformer(备用)

SLEEP, LISTEN, RESPOND = range(3)
STATE_NAME = {SLEEP: "睡眠", LISTEN: "聆听", RESPOND: "应答"}
# 文本唤醒匹配的识别变体(实测本机 14M 模型对"小瑞小瑞"的听写飘, 这些都是真实出现过的)
WAKE_TEXTS = ("小瑞小瑞", "小子小子", "小子一小子", "小瑞小子", "小子小瑞")


def wake_text_hit(tail):
    """文本唤醒判定: 整词变体, 或短句内"小"出现两次+近音字(容错听写飘移)."""
    for w in WAKE_TEXTS:
        if w in tail:
            return w
    if tail.count("小") >= 2 and ("瑞" in tail or "子" in tail):
        return "小瑞小瑞"
    return ""


def build_kws(keywords_file, threshold):
    return sherpa_onnx.KeywordSpotter(
        tokens=f"{KWS_DIR}/tokens.txt",
        encoder=f"{KWS_DIR}/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        decoder=f"{KWS_DIR}/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        joiner=f"{KWS_DIR}/joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        keywords_file=keywords_file,
        num_threads=2,
        keywords_threshold=threshold,
        keywords_score=1.0,
    )


def build_recognizer(asr_dir=ASR_DIR):
    """按目录内容自动选型: 有 joiner=transducer zipformer, 否则流式 paraformer."""
    import os
    if os.path.exists(f"{asr_dir}/joiner-epoch-99-avg-1.onnx"):
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=f"{asr_dir}/tokens.txt",
            encoder=f"{asr_dir}/encoder-epoch-99-avg-1.int8.onnx",
            decoder=f"{asr_dir}/decoder-epoch-99-avg-1.onnx",
            joiner=f"{asr_dir}/joiner-epoch-99-avg-1.onnx",
            num_threads=2,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=1.2,
            rule2_min_trailing_silence=0.9,
            decoding_method="greedy_search",
        )
    return sherpa_onnx.OnlineRecognizer.from_paraformer(
        tokens=f"{asr_dir}/tokens.txt",
        encoder=f"{asr_dir}/encoder.int8.onnx",
        decoder=f"{asr_dir}/decoder.int8.onnx",
        num_threads=2,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=1.2,
        rule2_min_trailing_silence=0.9,
    )


def _make_beep(freq, dur):
    """生成状态提示音 wav(启动时一次), 返回文件路径."""
    path = f"/tmp/xz_beep_{freq}.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration={dur}",
                    "-ar", "16000", "-ac", "2", "-c:a", "pcm_s16le", path], check=True)
    return path


def read_wav_16k_mono(path):
    """ffmpeg 解成 16k mono s16, 与 arecord 同格式; 返回 float32 归一化."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1",
         "-ar", "16000", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


def kws_result_text(kws, stream, result):
    if hasattr(result, "keyword"):
        return result.keyword
    return str(result)


class MicPump:
    """arecord 子进程 -> asyncio 队列(100ms 帧); 队列满丢最旧(保持最新).

    ACP 数字麦被快速反复开关后可能整流全零, restart() 由状态机在
    检测到连续零帧时调用, 泵线程读 EOF 后自动接新进程.
    """

    def __init__(self, device, loop, queue):
        self.device = device
        self.loop = loop
        self.queue = queue
        self.proc = self._spawn()
        import threading
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _spawn(self):
        return subprocess.Popen(
            ["arecord", "-D", self.device, "-q", "-t", "raw", "-f", "S16_LE",
             "-r", "16000", "-c", "1"],
            stdout=subprocess.PIPE)

    def restart(self):
        try:
            self.proc.terminate()
        except Exception:
            pass
        self.proc = self._spawn()

    def _pump(self):
        while True:
            out = self.proc.stdout
            while True:
                data = out.read(FRAME_SAMPLES * 2)
                if not data:
                    break
                while len(data) < FRAME_SAMPLES * 2:
                    more = out.read(FRAME_SAMPLES * 2 - len(data))
                    if not more:
                        break
                    data += more
                self.loop.call_soon_threadsafe(self._enqueue, data)
            rc = self.proc.poll()
            if rc is not None and rc not in (0, -15):
                log(f"[mic] 采集进程退出 code={rc}, 2 s 后重新拉起")
            time.sleep(2)  # 让驱动彻底释放
            if self.proc.poll() is not None:
                self.proc = self._spawn()  # 死了就重生, 设备忙/坏状态都能自愈

    def _enqueue(self, data):
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(data)


class VoiceClient(Client):
    """在 Client 上叠加语音状态机: stdin 位置换成麦克风循环."""

    def __init__(self, url, device_id, args):
        super().__init__(url, device_id, speaker=args.speaker)
        self.args = args
        self.kws = build_kws(args.keywords, args.threshold)
        self.rec = build_recognizer(args.asr_dir)
        self.kws_stream = self.kws.create_stream()
        self.tts_events = asyncio.Queue()
        self.beep_hi = _make_beep(880, 0.15)   # 开始聆听
        self.beep_lo = _make_beep(660, 0.10)   # 回睡眠

    def beep(self, path):
        subprocess.Popen(["aplay", "-D", self.speaker, "-q", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def on_text_event(self, ev):
        if ev.get("type") == "tts":
            await self.tts_events.put((ev.get("state"), ev.get("text")))

    async def stdin_loop(self):
        queue = asyncio.Queue(maxsize=400)
        if self.args.wav:
            await self._wav_mode(queue)
            return
        self.mic = MicPump(self.args.device, asyncio.get_running_loop(), queue)
        log(f"[mic] {self.args.device} 采集已启动 ({FRAME_MS} ms 帧)")
        await self._voice_loop(queue)

    async def _voice_loop(self, queue):
        state = SLEEP
        continuous = self.args.wake == "none"  # 一直在线: 不需要唤醒词
        session = self.args.wake == "session"  # 喊一次后连续对话
        after_respond = SLEEP  # 应答(问候)播完后的去向: SLEEP 或 LISTEN
        rec_stream = None
        deadline = 0.0
        last_partial = ""
        frame_no = 0
        win_max_rms = 0.0
        need_shadow = self.args.wake == "text" or self.args.debug_asr
        shadow = self.rec.create_stream() if need_shadow else None
        last_heard = ""
        zero_run = 0
        last_mic_restart = 0.0
        session_deadline = 0.0  # session 模式: 会话自动回睡眠的时刻
        if continuous:  # 一直在线模式: 直接进聆听, 说完一段答一段
            rec_stream = self.rec.create_stream()
            state = LISTEN
            deadline = time.monotonic() + self.args.listen_timeout
            log("[state] 一直在线模式: 直接说话即可")
        while not self.closing:
            frame_b = await queue.get()
            frame = np.frombuffer(frame_b, dtype=np.int16).astype(np.float32) / 32768.0
            frame_no += 1
            if state == SLEEP:
                rms = float(np.sqrt(np.mean(np.square(frame))))
                win_max_rms = max(win_max_rms, rms)
                if frame_no % 30 == 0:  # 每 3 s 报一次窗口内最大电平, 看麦克风灵敏度
                    log(f"[mic] 电平 max_rms={win_max_rms:.1f}")
                    win_max_rms = 0.0
                # 严格全零(数字静音)才算坏流; 安静房间是 rms<1 的小值, 属正常
                zero_run = zero_run + 1 if float(np.abs(frame).max()) == 0.0 else 0
                if frame_no % 300 == 0:  # 30 s 心跳, 防服务端按无语音空闲断连
                    await self.send_json({"type": "ping"})  # 失败即上抛→自动重连
                if zero_run >= 30 and time.monotonic() - last_mic_restart > 10:
                    log("[mic] 连续 3 s 全零, 重启采集(ACP 数字麦坏状态自愈)")
                    self.mic.restart()
                    zero_run = 0
                    last_mic_restart = time.monotonic()
                if shadow is None and need_shadow:
                    shadow = self.rec.create_stream()
                    last_heard = ""
                heard_tail = ""
                if shadow is not None:
                    shadow.accept_waveform(16000, frame)
                    while self.rec.is_ready(shadow):
                        self.rec.decode_stream(shadow)
                    heard = self.rec.get_result(shadow).strip()
                    if heard and heard != last_heard:
                        last_heard = heard
                        log(f"[hear] {heard}")
                    heard_tail = last_heard.replace(" ", "")[-10:]
                if self.args.wake == "text":
                    # 文本唤醒: 流式识别听到唤醒词(含近音变体)即唤醒
                    hit = wake_text_hit(heard_tail)
                else:
                    self.kws_stream.accept_waveform(16000, frame)
                    while self.kws.is_ready(self.kws_stream):
                        self.kws.decode_streams([self.kws_stream])
                    r = self.kws.get_result(self.kws_stream)
                    hit = kws_result_text(self.kws, self.kws_stream, r) if r else ""
                if hit:
                    log(f"[wake] 命中({self.args.wake}): {hit}")
                    self.beep(self.beep_hi)
                    shadow = None
                    await self.say("小瑞小瑞")  # 服务端按唤醒词回复/致意
                    # 先聋等问候播完(否则问候回声会混进聆听窗口), 再开始聆听
                    state = RESPOND
                    after_respond = LISTEN
            elif state == LISTEN:
                rec_stream.accept_waveform(16000, frame)
                while self.rec.is_ready(rec_stream):
                    self.rec.decode_stream(rec_stream)
                text = self.rec.get_result(rec_stream).strip()
                if text and text != last_partial:
                    last_partial = text
                    log(f"[asr] {text}")
                if self.rec.is_endpoint(rec_stream):
                    if text:
                        await self.say(text)
                        state = RESPOND
                    elif continuous or session:
                        if session and time.monotonic() > session_deadline:
                            state = SLEEP
                            self.beep(self.beep_lo)
                            self.kws.reset_stream(self.kws_stream)
                            log("[state] 会话超时, 回睡眠(咚)")
                        else:
                            rec_stream = self.rec.create_stream()  # 空段, 继续听
                    else:
                        state = SLEEP
                        self.beep(self.beep_lo)
                        self.kws.reset_stream(self.kws_stream)
                        log("[state] 空段, 回睡眠(咚)")
                elif time.monotonic() > deadline:
                    if continuous or session:
                        if session and time.monotonic() > session_deadline:
                            state = SLEEP
                            self.beep(self.beep_lo)
                            self.kws.reset_stream(self.kws_stream)
                            log("[state] 会话超时, 回睡眠(咚)")
                        else:
                            rec_stream = self.rec.create_stream()
                            deadline = time.monotonic() + self.args.listen_timeout
                    else:
                        state = SLEEP
                        self.beep(self.beep_lo)
                        self.kws.reset_stream(self.kws_stream)
                        log("[state] 聆听超时, 回睡眠(咚)")
            elif state == RESPOND:
                # 等 TTS 播完(半双工: 期间不喂 KWS/ASR); 问候等待给短超时
                wait_cap = self.args.tts_timeout if after_respond == SLEEP else 6.0
                try:
                    while True:
                        st, _ = await asyncio.wait_for(
                            self.tts_events.get(), timeout=wait_cap)
                        if st == "stop":
                            break
                except asyncio.TimeoutError:
                    log("[state] 等待 TTS 超时")
                self._drain(queue)  # 排空播放期拾到的回声窗口
                self.kws.reset_stream(self.kws_stream)  # 防扬声器回声自触发
                if after_respond == LISTEN or continuous or session:
                    after_respond = SLEEP
                    rec_stream = self.rec.create_stream()
                    state = LISTEN
                    deadline = time.monotonic() + self.args.listen_timeout
                    if session:
                        session_deadline = time.monotonic() + self.args.session_timeout
                    last_partial = ""
                    if continuous:
                        log("[state] 继续聆听")
                    else:
                        self.beep(self.beep_hi)
                        log("[state] 问候播完, 开始聆听(叮=该说话了)"
                            + (f", {self.args.session_timeout:.0f} s 无话自动睡" if session else ""))
                else:
                    state = SLEEP
                    self.beep(self.beep_lo)
                    log("[state] 回睡眠, 等唤醒词(咚)")

    def _drain(self, queue):
        while not queue.empty():
            queue.get_nowait()

    async def _wav_mode(self, queue):
        """离线回归: wav 依次过 KWS 和流式 ASR, 不联网."""
        for path in self.args.wav:
            pcm = read_wav_16k_mono(path)
            ks = self.kws.create_stream()
            ks.accept_waveform(16000, pcm)
            while self.kws.is_ready(ks):
                self.kws.decode_streams([ks])
            r = self.kws.get_result(ks)
            hit = kws_result_text(self.kws, ks, r) if r else ""
            rs = self.rec.create_stream()
            rs.accept_waveform(16000, pcm)
            rs.input_finished()
            while self.rec.is_ready(rs):
                self.rec.decode_stream(rs)
            text = self.rec.get_result(rs).strip()
            log(f"[wav] {path}\n    KWS: {hit or '(未命中)'}\n    ASR: {text}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="ws://127.0.0.1:8000/xiaorui/v1/")
    ap.add_argument("--device-id", default="AA:BB:CC:DD:EE:FF")
    ap.add_argument("--device", default="default", help="arecord 采集设备")
    ap.add_argument("--asr-dir", default=ASR_DIR,
                    help=f"流式模型目录(默认板上同款 paraformer; {ASR_DIR_SMALL}=14M zipformer)")
    ap.add_argument("--speaker", default="plughw:1,0",
                    help="aplay 播放设备(默认模拟输出口; default 会走 HDMI 没声音)")
    ap.add_argument("--keywords", default=f"{__import__('os').path.dirname(__file__)}/keywords.txt")
    ap.add_argument("--wake", choices=["session", "text", "none", "kws"], default="session",
                    help="唤醒方式: session=喊一次连续对话一段时间不说话自动睡(默认); "
                         "text=一句一醒; none=一直在线; kws=3.3M 声学模型(板端)")
    ap.add_argument("--session-timeout", type=float, default=60.0,
                    help="session 模式: 距上次对话多久自动回睡眠(秒)")
    ap.add_argument("--threshold", type=float, default=0.25, help="KWS 命中阈值, 误唤醒调高")
    ap.add_argument("--no-debug-asr", action="store_true",
                    help="睡眠期不打印 [hear] 碎片(省 CPU)")
    ap.add_argument("--listen-timeout", type=float, default=10.0)
    ap.add_argument("--tts-timeout", type=float, default=60.0)
    ap.add_argument("--wav", nargs="*", help="离线模式: wav 过 KWS+ASR")
    args = ap.parse_args()
    args.debug_asr = not args.no_debug_asr
    try:
        asyncio.run(VoiceClient(args.url, args.device_id, args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

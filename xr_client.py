#!/usr/bin/env python3
"""网关 WS 协议客户端(PC 原型, 端侧 ASR 路线).

验证 detect+text 路径: 本地出文字 -> 服务端 LLM -> TTS opus 回程 -> 本机播放.
这是未来 sherpa_live C 客户端的协议参照, 刻意只依赖 websockets + opuslib_next
(与服务端同一套库, opus 帧格式天然对齐).

用法:
    python3 xz_client.py --url ws://127.0.0.1:8000/xiaorui/v1/
然后逐行输入"识别结果"(模拟 sherpa_live 的最终文本), 回车发送;
喇叭应播出 TTS 回答. 播放中再输入会先发 abort 打断. Ctrl-D 退出.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time

import opuslib_next
import websockets

SAMPLE_RATE = 16000
FRAME_SAMPLES = 960  # 60 ms @ 16 kHz, 与服务端 AUDIO_FRAME_DURATION 对齐


def log(msg):
    print(msg, flush=True)


def spawn_aplay(device="default"):
    return subprocess.Popen(
        ["aplay", "-D", device, "-q", "-t", "raw", "-f", "S16_LE",
         "-r", str(SAMPLE_RATE), "-c", "2"],
        stdin=subprocess.PIPE,
    )


def mono_to_stereo(pcm):
    """把 16-bit LE 单声道 PCM 复制到左右声道，不改变时长或采样率。"""
    if len(pcm) & 1:
        raise ValueError("PCM byte count must be even")
    stereo = bytearray(len(pcm) * 2)
    stereo[0::4] = pcm[0::2]
    stereo[1::4] = pcm[1::2]
    stereo[2::4] = pcm[0::2]
    stereo[3::4] = pcm[1::2]
    return stereo


class Client:
    def __init__(self, url, device_id, speaker="default"):
        self.url = url
        self.device_id = device_id
        self.speaker = speaker
        self.decoder = opuslib_next.Decoder(SAMPLE_RATE, 1)
        self.aplay = None
        self.speaking = False
        self.ws = None
        self.closing = False
        self.pcm_seconds = 0.0

    async def send_json(self, obj):
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def hello(self):
        await self.send_json({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "features": {"mcp": False, "aec": False},
            "audio_params": {
                "format": "opus",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "frame_duration": 60,
            },
        })
        reply = json.loads(await self.ws.recv())
        if reply.get("type") != "hello":
            raise RuntimeError(f"first frame not server hello: {reply}")
        log(f"[handshake] server hello ok")

    async def say(self, text):
        """发送一句端侧识别结果(detect+text = 官方端侧 ASR 入口)."""
        if self.speaking:
            await self.send_json({"type": "abort"})
            self.speaking = False
        await self.send_json({"type": "listen", "state": "detect", "text": text})
        log(f"[>>] detect: {text}")

    def feed_pcm(self, pcm):
        if self.aplay is not None and self.aplay.poll() is not None:
            rc = self.aplay.returncode
            self.aplay = None
            if rc not in (0, -15):
                time.sleep(0.3)  # 设备忙等场景下退避, 别每帧风暴式重试
        if self.aplay is None:
            self.aplay = spawn_aplay(self.speaker)
        try:
            self.aplay.stdin.write(mono_to_stereo(pcm))
        except BrokenPipeError:
            self.aplay = None
        self.pcm_seconds += len(pcm) / 2 / SAMPLE_RATE

    async def recv_loop(self):
        async for msg in self.ws:
            if isinstance(msg, bytes):
                self.feed_pcm(self.decoder.decode(msg, FRAME_SAMPLES))
                continue
            ev = json.loads(msg)
            t = ev.get("type")
            if t == "tts":
                state = ev.get("state")
                if state == "start":
                    self.speaking = True
                elif state == "stop":
                    self.speaking = False
                log(f"[<<] tts {state}" + (f": {ev['text']}" if ev.get("text") else ""))
            elif t == "stt":
                log(f"[<<] stt: {ev.get('text')}")
            elif t == "llm":
                log(f"[<<] llm emotion: {ev.get('text')}")
            else:
                log(f"[<<] {str(ev)[:100]}")
            await self.on_text_event(ev)

    async def on_text_event(self, ev):
        """子类钩子: 语音状态机据此跟踪 tts start/stop."""

    async def stdin_loop(self):
        loop = asyncio.get_running_loop()
        while not self.closing:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            text = line.strip()
            if text:
                await self.say(text)
        self.closing = True

    async def run(self):
        async for ws in websockets.connect(
            self.url,
            additional_headers={
                "device-id": self.device_id,
                "client-id": f"{self.device_id}-client",
            },
        ):
            try:
                self.ws = ws
                await self.hello()
                recv_task = asyncio.create_task(self.recv_loop())
                session_error = None
                try:
                    await self.stdin_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 会话内异常(对端断开/发送失败): 记下并走重连
                    session_error = e
                    log(f"[conn] {type(e).__name__}: {e}, 5 s 后重连")
                finally:
                    recv_task.cancel()
                    try:
                        await recv_task
                    except asyncio.CancelledError:
                        pass
                    try:
                        await ws.close()
                    except Exception:
                        pass
                if session_error is None:
                    log(f"[done] 播放累计 {self.pcm_seconds:.1f} s PCM")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"[conn] {e}, 5 s 后重连")
            await asyncio.sleep(5)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="ws://127.0.0.1:8000/xiaorui/v1/")
    ap.add_argument("--device-id", default="AA:BB:CC:DD:EE:FF")
    args = ap.parse_args()
    try:
        asyncio.run(Client(args.url, args.device_id).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

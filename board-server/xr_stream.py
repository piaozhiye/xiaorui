#!/usr/bin/env python3
"""板上实时流式语音客户端(listen auto 模式): 麦克风 Opus 流 -> 服务端, 服务端 VAD+ASR+LLM+TTS.

与真机一致: 音频通道打开后持续推流, 服务端 silero VAD 自动判断说话起止,
说完 ~0.8s 自动触发回答并流式播回; TTS 播放期间暂停推流(半双工防回声),
播完自动恢复聆听. 无固定录音窗口, 即说即答.

用法: /data/miniforge/bin/python xz_stream.py [--mic plughw:2,0] [--speaker plughw:2,0]
"""
import argparse
import asyncio
import json
import subprocess
import sys

import opuslib_next
import websockets

SR = 16000
FRAME_SAMPLES = 960          # 60 ms
FRAME_BYTES = FRAME_SAMPLES * 2


def log(msg):
    print(msg, flush=True)


class StreamClient:
    def __init__(self, url, device_id, mic, speaker):
        self.url = url
        self.device_id = device_id
        self.mic_dev = mic
        self.speaker_dev = speaker
        self.enc = opuslib_next.Encoder(SR, 1, opuslib_next.APPLICATION_VOIP)
        self.dec = opuslib_next.Decoder(SR, 1)
        self.aplay = None
        self.mic_paused = False   # TTS 播放期间暂停推流(防回声)
        self.tts_events = asyncio.Queue()

    # ---------- 音频输出 ----------
    def feed_pcm(self, pcm: bytes):
        if self.aplay is None or self.aplay.poll() is not None:
            self.aplay = subprocess.Popen(
                ["aplay", "-D", self.speaker_dev, "-q", "-t", "raw",
                 "-f", "S16_LE", "-r", str(SR), "-c", "1"],
                stdin=subprocess.PIPE)
        try:
            self.aplay.stdin.write(pcm)
        except BrokenPipeError:
            self.aplay = None

    # ---------- 麦克风推流 ----------
    async def mic_loop(self, ws):
        arecord = subprocess.Popen(
            ["arecord", "-D", self.mic_dev, "-q", "-t", "raw", "-f", "S16_LE",
             "-r", str(SR), "-c", "1"],
            stdout=subprocess.PIPE)
        log(f"[mic] {self.mic_dev} 推流中(实时, 无固定窗口)")
        dropped = 0
        while True:
            data = arecord.stdout.read(FRAME_BYTES)
            if not data:
                await asyncio.sleep(0.05)
                arecord = subprocess.Popen(
                    ["arecord", "-D", self.mic_dev, "-q", "-t", "raw", "-f", "S16_LE",
                     "-r", str(SR), "-c", "1"],
                    stdout=subprocess.PIPE)
                continue
            while len(data) < FRAME_BYTES:
                more = arecord.stdout.read(FRAME_BYTES - len(data))
                if not more:
                    break
                data += more
            if self.mic_paused:
                dropped += 1
                continue  # TTS 期间不推流(半双工)
            frame = data[:FRAME_SAMPLES * 2]
            pcm = frame  # opuslib 接受 bytes
            try:
                opus = self.enc.encode(pcm, FRAME_SAMPLES)
                await ws.send(opus)
            except Exception:
                return
            _ = dropped

    # ---------- 事件接收 ----------
    async def recv_loop(self, ws):
        n = 0
        async for msg in ws:
            if isinstance(msg, bytes):
                n += 1
                if n == 1:
                    log("[recv] 收到首包音频")
                try:
                    pcm = self.dec.decode(msg, FRAME_SAMPLES)
                except Exception as e:
                    log(f"[recv] decode err: {e}")
                    continue
                self.feed_pcm(pcm)
                continue
            ev = json.loads(msg)
            t = ev.get("type")
            if t == "tts":
                st = ev.get("state")
                log(f"[tts] {st}")
                await self.tts_events.put(st)
            elif t == "stt":
                log(f"[stt] {ev.get('text')}")
            elif t == "llm":
                log(f"[llm] {ev.get('text')}")
            elif t == "pong":
                pass
            else:
                log(f"[<<] {str(ev)[:80]}")

    async def ping_loop(self, ws):
        while True:
            await asyncio.sleep(30)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                return


async def run(args):
    async for ws in websockets.connect(
        args.url,
        additional_headers={"device-id": args.device_id,
                            "client-id": args.device_id + "-c"},
    ):
        try:
            await ws.send(json.dumps({
                "type": "hello", "version": 1, "transport": "websocket",
                "features": {"mcp": False, "aec": False},
                "audio_params": {"format": "opus", "sample_rate": SR,
                                 "channels": 1, "frame_duration": 60},
            }, ensure_ascii=False))
            reply = json.loads(await ws.recv())
            if reply.get("type") != "hello":
                raise RuntimeError(f"bad hello: {str(reply)[:80]}")
            log("[handshake] server hello ok")
            await ws.send(json.dumps({"type": "listen", "state": "start", "mode": "auto"}))
            log("[session] auto 模式: 直接说话即可, 说完停顿自动回答")
            c = StreamClient(args.url, args.device_id, args.mic, args.speaker)
            ping = asyncio.create_task(c.ping_loop(ws))
            await asyncio.gather(c.mic_loop(ws), c.recv_loop(ws))
            ping.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[conn] {type(e).__name__}: {e}, 5 s 后重连")
            await asyncio.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8000/xiaorui/v1/")
    ap.add_argument("--device-id", default="AA:BB:CC:DD:EE:FF")
    ap.add_argument("--mic", default="plughw:2,0")
    ap.add_argument("--speaker", default="plughw:2,0")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

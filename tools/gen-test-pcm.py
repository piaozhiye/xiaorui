#!/usr/bin/env python3
"""生成 Snapcast 测试用裸 PCM(与 server 的 [stream] sampleformat 对应)。

用法:
    python3 tools/gen-test-pcm.py --seconds 60 > /tmp/demo.pcm
    python3 tools/gen-test-pcm.py --seconds 60 --rate 24000 --channels 1 \
        --out /tmp/demo.pcm
    python3 tools/gen-test-pcm.py --mode sync --seconds 120 --out /tmp/sync.pcm

模式:
    melody(默认) 每 0.5 s 换一个音的简单旋律 + 每 1 s 一个 5 ms 的 3 kHz 短脉冲
    sync       只保留脉冲: 每 1 s 的正中间一个 5 ms 的 3 kHz 突发(声学同步测量用),
               两突发之间是一段很轻的 1 kHz 底噪(便于听感确认在放音)

脉冲沿就是"同一时刻"的声学锚点(双设备录音互相关时用)。
格式: 裸 PCM, s16le, 交织多声道, 无文件头(server 的 file:// 源就是这种裸 PCM)。
"""
import argparse
import math
import struct
import sys

NOTES = [440, 494, 523, 587, 659, 587, 523, 494]
PULSE_FREQ = 3000.0
PULSE_MS = 5.0


def build(args) -> bytes:
    rate, channels = args.rate, args.channels
    total = int(rate * args.seconds)
    out = bytearray()
    pulse_len = int(rate * PULSE_MS / 1000.0)
    for n in range(total):
        t = n / rate
        if args.mode == "dual":
            # 双频探针: 左声道 3kHz(板A), 右声道 5kHz(板B), 位置完全相同
            in_pulse = ((t % 1.0) >= 0.5) & ((t % 1.0) < 0.5 + PULSE_MS / 1000.0)
            vl = 0.8 * math.sin(2 * math.pi * 3000 * t) if in_pulse else 0.0
            vr = 0.8 * math.sin(2 * math.pi * 5000 * t) if in_pulse else 0.0
            out += struct.pack("<hh", int(vl * args.amplitude), int(vr * args.amplitude))
            continue
        if args.mode == "sync":
            # 每秒正中间放一个 5ms 的 3kHz 突发
            in_pulse = (t % 1.0) >= 0.5 and (t % 1.0) < 0.5 + PULSE_MS / 1000.0
            if in_pulse:
                v = 1.0 * math.sin(2 * math.pi * PULSE_FREQ * t)   # 满幅, 好让麦克风听清
            else:
                v = 0.08 * math.sin(2 * math.pi * 1000 * t)
        else:
            env = 0.25 * (0.5 + 0.5 * math.sin(2 * math.pi * 2 * t))
            v = env * math.sin(2 * math.pi * NOTES[int(t * 2) % len(NOTES)] * t)
            if t % 1.0 < PULSE_MS / 1000.0:  # 每秒一个 3 kHz 短脉冲
                v += 0.6 * math.sin(2 * math.pi * PULSE_FREQ * t)
        s = int(max(-1.0, min(1.0, v)) * args.amplitude)
        frame = struct.pack("<h", s) * channels
        out += frame
    return bytes(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=("melody", "sync", "dual"), default="melody")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--rate", type=int, default=24000)
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--amplitude", type=int, default=20000)
    p.add_argument("--out", default="-")
    args = p.parse_args()

    data = build(args)
    if args.out == "-":
        sys.stdout.buffer.write(data)
    else:
        with open(args.out, "wb") as f:
            f.write(data)
        dur = len(data) / (2 * args.channels * args.rate)
        print(f"{args.out}: {len(data)} bytes = {dur:.2f}s @ {args.rate}Hz/{args.channels}ch/s16le"
              f" (mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""真实后端 100 轮 E2E 的声学判读: 语音(真实 TTS)期间 3kHz 音乐应消失, 之后恢复.
用法: python3 -I analyze_e2e.py <wav> [expect_turns]
"""
import sys, wave
import numpy as np

path = sys.argv[1]
expect = int(sys.argv[2]) if len(sys.argv) > 2 else 100

with wave.open(path) as w:
    sr = w.getframerate(); n = w.getnframes(); ch = w.getnchannels()
    x = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64)
if ch > 1:
    x = x.reshape(-1, ch).mean(1)
dur = len(x) / sr
win = 4800; hop = 2400          # 100ms 窗 / 50ms 步进
frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
sp = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1))
b3 = int(round(3000 * win / sr))
e3 = sp[:, b3]
t = np.arange(len(e3)) * hop / sr
ref = np.median(e3)
music = e3 > 0.20 * ref          # 音乐存在
# 找"音乐消失"的连续段(语音让位)
def segs(mask, want, minlen):
    out = []; i = 0
    while i < len(mask):
        if mask[i] == want:
            j = i
            while j < len(mask) and mask[j] == want: j += 1
            if (j - i) * hop / sr >= minlen: out.append((i, j))
            i = j
        else: i += 1
    return out
pauses = segs(music, False, 0.8)
print(f"录音 {dur:.1f}s, 3k 参考 {ref:.0f}, 检出音乐暂停段 {len(pauses)} 个 (期望 {expect})")
if pauses:
    ds = [(j - i) * hop / sr for i, j in pauses]
    print(f"暂停段时长: p50 {np.median(ds):.2f}s  p95 {np.percentile(ds,95):.2f}s  max {max(ds):.2f}s")
    # 暂停段之间音乐占比
    print(f"非暂停段 3k 中位/参考: {np.median(e3[music])/ref:.2f}")

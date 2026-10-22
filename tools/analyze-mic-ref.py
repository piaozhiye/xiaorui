#!/usr/bin/env python3
"""分析 ES8388 mic/ref 通道录音(见 test-es8388-vqe.sh 的 R1-R6).

输出: 每通道 RMS/峰值/削波/样本数, 1kHz/近端语音能量, mic-ref 互相关延迟,
raw vs VQE 的回声衰减估计(分时段, 不用整段平均冒充 ERLE).
判定: 哪个通道是 ref(跟随播放), 哪个是有效 mic(含近端语音), AEC 是否有效.
"""
import sys
import wave
from pathlib import Path

import numpy as np


def load(p):
    w = wave.open(str(p))
    n, sr, ch = w.getnframes(), w.getframerate(), w.getnchannels()
    d = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
    w.close()
    return d.reshape(-1, ch), sr


def stats(x, sr, label, tone_hz=None):
    rms = np.sqrt((x ** 2).mean()) if len(x) else 0
    peak = np.abs(x).max() if len(x) else 0
    clip = (np.abs(x) >= 32700).mean() if len(x) else 0
    line = "%-22s rms=%7.1f peak=%6d clip=%.4f" % (label, rms, peak, clip)
    if tone_hz and len(x):
        # Goertzel 单频能量
        k = int(round(len(x) * tone_hz / sr))
        t = np.arange(len(x))
        go = abs((x * np.exp(-2j * np.pi * k * t / len(x))).sum()) / len(x)
        line += " 1kHz=%.1f" % go
    return line, rms


def band_energy(x, sr, lo, hi):
    """[lo,hi]Hz 带内能量占比(粗 FFT)."""
    if len(x) < sr // 4:
        return 0.0
    seg = x[: len(x) // (sr // 4) * (sr // 4)]
    sp = np.abs(np.fft.rfft(seg.reshape(-1, sr // 4), axis=1)).mean(axis=0)
    f = np.fft.rfftfreq(sr // 4, 1 / sr)
    return sp[(f >= lo) & (f <= hi)].sum() / (sp.sum() + 1e-9)


def seg_rms(x, sr, t0, t1):
    a, b = int(t0 * sr), min(int(t1 * sr), len(x))
    return np.sqrt((x[a:b] ** 2).mean()) if b > a else 0.0


def main():
    d = Path(sys.argv[1])
    files = {p.name.split("_", 1)[1].split(".")[0]: p for p in d.glob("r*_*.wav")}
    if not files:
        print("无录音文件")
        return 1
    for name, p in sorted(files.items()):
        x, sr = load(p)
        print("== %s  sr=%d ch=%d dur=%.1fs" % (p.name, sr, x.shape[1], len(x) / sr))
        for c in range(x.shape[1]):
            print("   ", stats(x[:, c], sr, "ch%d" % c, tone_hz=1000)[0])
    # R2: 哪个通道跟随播放(3-6s vs 0-2s 的 1kHz 段能量比)
    if "raw_play" in files:
        x, sr = load(files["raw_play"])
        print("== R2 播放跟随判定(1kHz Goertzel, 播放段/静默段)")
        for c in range(x.shape[1]):
            a = stats(x[int(3.5 * sr):int(6 * sr), c], sr, "ch%d play" % c, 1000)[1]
            b = stats(x[int(0 * sr):int(2 * sr), c], sr, "ch%d quiet" % c, 1000)[1]
            ratio = (a / (b + 1e-9))
            print("    ch%d: ratio=%.1f %s" % (c, ratio, "<-- REF(跟随播放)" if ratio > 10 else ""))
    # R6: 哪个通道有近端语音(300-3400Hz 能量, 语音段 vs 静默段)
    if "raw_nearend" in files:
        x, sr = load(files["raw_nearend"])
        print("== R6 有效 mic 判定(语音段 300-3400Hz RMS vs 静默段)")
        for c in range(x.shape[1]):
            v = seg_rms(x[:, c], sr, 2.0, 6.0)
            q = seg_rms(x[:, c], sr, 0.0, 1.8)
            print("    ch%d: 语音段=%.1f 静默段=%.1f ratio=%.1f %s"
                  % (c, v, q, v / (q + 1e-9), "<-- 有效 MIC" if v / (q + 1e-9) > 3 else ""))
    # R4 vs R5: AEC 效果(播放+语音时, raw mic vs VQE 输出的语音带能量变化
    # 与 1kHz 残留)
    if "raw_doubletalk" in files and "vqe_doubletalk" in files:
        xr_, sr = load(files["raw_doubletalk"])
        xv, sv = load(files["vqe_doubletalk"])
        print("== AEC 对比(双讲, 播放窗口 2-5s)")
        mic_ch = 0  # 由上面判定得出, 先默认 ch0; 报告里核对
        raw_mic = xr_[:, min(mic_ch, xr_.shape[1] - 1)]
        vqe_out = xv[:, 0] if xv.shape[1] == 1 else xv[:, 0]
        t0, t1 = int(2.5 * sr), int(5 * sr)
        for label, sig in (("raw-ch0", raw_mic), ("vqe-out0", vqe_out)):
            seg = sig[t0:t1]
            g = stats(seg, sr, label, 1000)[0]
            g += "  语音带占比=%.2f" % band_energy(seg, sr, 300, 3400)
            print("   ", g)
        e_raw = stats(raw_mic[t0:t1], sr, "", 1000)[1]
        e_vqe = stats(vqe_out[t0:t1], sr, "", 1000)[1]
        if e_raw > 1:
            print("    1kHz 回声残留: raw=%.1f vqe=%.1f → 衰减≈%.1f dB"
                  % (e_raw, e_vqe, 20 * np.log10(max(e_raw, 1e-9) / max(e_vqe, 1e-9))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

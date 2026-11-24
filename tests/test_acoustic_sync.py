#!/usr/bin/env python3
"""声学同步测量工具的自测(合成已知延迟的录音, 看分析器能不能读回来)。"""
import math
import sys
import unittest
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from snapcast_acoustic_sync import analyze, find_pulse_train  # noqa: E402

RATE = 48000
PULSE_FREQ = 3000.0
PULSE_MS = 5.0


def make_pulse_train(seconds, rate=RATE, period=1.0, offset=0.5):
    """每秒正中间一个 5ms 的 3kHz 突发。"""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    in_pulse = ((t % period) >= offset) & ((t % period) < offset + PULSE_MS / 1000.0)
    x = np.where(in_pulse, 0.5 * np.sin(2 * math.pi * PULSE_FREQ * t), 0.0)
    return x


def write_wav(path, x, rate=RATE):
    # x 是 [-1, 1] 的浮点, 要缩放到 int16 满量程
    pcm = np.clip(x * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


class AcousticSyncAnalysisTests(unittest.TestCase):
    def test_single_source_reports_single_pulses(self):
        # 标定段: 只有一路 -> 报每窗口的脉冲时刻, 不算时间差
        x = make_pulse_train(6)
        path = Path("/tmp/opencode/acoustic-selftest-single.wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, x)
        res = analyze(str(path), expect_pairs=False, verbose=False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "single")
        self.assertEqual(res["pulses"], 6)
        # 脉冲在每 1 秒的正中间
        for t0 in res["pulse_times_s"]:
            self.assertAlmostEqual(t0 % 1.0, 0.5, delta=0.01)

    def test_matched_filter_finds_pulse(self):
        x = make_pulse_train(3)
        env = find_pulse_train(x, RATE)
        # 第一个脉冲在 0.5s 处(在 0.4~0.6s 窗口里取峰, 不要全局 argmax:
        # 三个脉冲的匹配滤波幅度几乎相同, 全局 argmax 可能落在最后一个)
        seg = env[int(RATE * 0.4): int(RATE * 0.6)]
        peak = int(np.argmax(seg)) + int(RATE * 0.4)
        self.assertAlmostEqual(peak / RATE, 0.5, delta=0.01)


if __name__ == "__main__":
    unittest.main()


class SingleRecordingMethodTests(unittest.TestCase):
    """单次连续录音法(静音分段 + 相位差)的自测。"""

    # 自测用 8kHz/短段: 3kHz 脉冲在 8kHz 下不混叠, 但数据量小 ~36 倍,
    # 单测才能跑在几十毫秒级(用 48k/20s 会变成分钟级)
    TEST_RATE = 8000

    # cal 6s(不是 2s): 分析器的质量门槛要求每段 >=3 个脉冲, 2s 只给 2 个
    def _record_three_segments(self, offset_ms, cal=6.0, both=6.0, rate=TEST_RATE):
        """合成: [0,cal) 只有 A; [cal,2cal) 只有 B; [2cal,..) 两路。
        B 相对 A 偏移 offset_ms。"""
        total = 2 * cal + both
        n = int(rate * total)
        x = np.zeros(n)
        shift_b = int(rate * offset_ms / 1000.0)
        a = make_pulse_train(total, rate=rate)
        if shift_b >= 0:          # B 晚
            b = np.concatenate([np.zeros(shift_b), a[:-shift_b]]) if shift_b else a.copy()
        else:                     # B 早: 左移
            k = -shift_b
            b = np.concatenate([a[k:], np.zeros(k)])
        seg_a = slice(0, int(cal * rate))
        seg_b = slice(int(cal * rate), int(2 * cal * rate))
        seg_both = slice(int(2 * cal * rate), n)
        x[seg_a] += a[seg_a]
        x[seg_b] += b[seg_b]
        x[seg_both] += a[seg_both] + 0.5 * b[seg_both]
        return x

    def test_known_offset_is_recovered(self):
        from snapcast_acoustic_sync import analyze_single
        for offset_ms in (3.0, 0.0, -4.0):
            x = self._record_three_segments(offset_ms)
            path = Path(f"/tmp/opencode/acoustic-selftest-single-{offset_ms}.wav")
            path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(path, x, rate=self.TEST_RATE)
            res = analyze_single(str(path), 6.0, verbose=False)
            self.assertTrue(res["ok"], res)
            self.assertAlmostEqual(res["phase_diff_ms"], offset_ms, delta=0.6,
                                   msg=f"offset {offset_ms}")

    def test_missing_segment_reports_error(self):
        from snapcast_acoustic_sync import analyze_single
        x = np.zeros(int(self.TEST_RATE * 4))
        path = Path("/tmp/opencode/acoustic-selftest-empty.wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, x, rate=self.TEST_RATE)
        res = analyze_single(str(path), 3.0, verbose=False)
        self.assertFalse(res["ok"])
        self.assertIn("error", res)


class ReviewerFindingsTests(unittest.TestCase):
    """复核提出的问题, 先写成会失败的用例。"""

    def test_fit_segment_recovers_real_phase(self):
        # 复核复现: 已知相位 0.38 被算成 0.0
        # 原因: pos_mid = a + b*(t_mid-a)/b 代数上恒等于 t_mid, 返回的是段中点相位
        from snapcast_acoustic_sync import fit_segment
        peaks = [(i + 0.38, 1.0) for i in range(6)]
        fit = fit_segment(peaks, 0.4, 5.6)
        self.assertIsNotNone(fit, "6 个脉冲应该够拟合")
        phase, drift_ppm, n = fit
        self.assertAlmostEqual(phase % 1.0, 0.38, delta=0.005,
                               msg=f"拟合相位应为 0.38, 实得 {phase}")

    def test_fit_segment_recovers_drift(self):
        # 周期 1.001s = +1000ppm, 起始相位 0.38
        from snapcast_acoustic_sync import fit_segment
        peaks = [(0.38 + i * 1.001, 1.0) for i in range(20)]
        fit = fit_segment(peaks, 0.0, 21.0)
        self.assertIsNotNone(fit)
        phase, drift_ppm, n = fit
        self.assertAlmostEqual(phase % 1.0, 0.38, delta=0.005)
        self.assertAlmostEqual(drift_ppm, 1000.0, delta=50.0)


class DualProbeTests(unittest.TestCase):
    """双频探针: L=3kHz(A), R=5kHz(B), 同一条流同一时间线。"""

    RATE = 24000          # 自测用小采样率, 5kHz 仍在 Nyquist 内
    CAL = 0.0

    def _dual_recording(self, offset_ms, rate=None, seconds=8.0,
                        mute_b=False, mute_a=False):
        rate = rate or self.RATE
        n = int(rate * seconds)
        t = np.arange(n) / rate
        in_pulse = ((t % 1.0) >= 0.5) & ((t % 1.0) < 0.505)
        a = np.where(in_pulse, 0.5 * np.sin(2 * math.pi * 3000 * t), 0.0)
        b = np.where(in_pulse, 0.5 * np.sin(2 * math.pi * 5000 * t), 0.0)
        shift = int(rate * offset_ms / 1000.0)
        if shift >= 0:
            b = np.concatenate([np.zeros(shift), b[:-shift]]) if shift else b
        else:
            k = -shift
            b = np.concatenate([b[k:], np.zeros(k)])
        if mute_a:
            a[:] = 0
        if mute_b:
            b[:] = 0
        return a + b

    def test_known_offset_is_recovered(self):
        from snapcast_acoustic_sync import analyze_dual
        for offset_ms in (2.0, 0.0, -3.0):
            x = self._dual_recording(offset_ms)
            path = Path(f"/tmp/opencode/dual-{offset_ms}.wav")
            path.parent.mkdir(parents=True, exist_ok=True)
            write_wav(path, x, rate=self.RATE)
            res = analyze_dual(str(path), verbose=False)
            self.assertTrue(res["ok"], res)
            self.assertAlmostEqual(res["delta_ms"]["median"], offset_ms, delta=0.6,
                                   msg=f"offset {offset_ms}: {res['delta_ms']}")

    def test_silent_board_fails(self):
        # 复核复现: 同播段全静音也要失败, 不能报成功
        from snapcast_acoustic_sync import analyze_dual
        x = self._dual_recording(3.0, mute_b=True)      # B 完全没出声
        path = Path("/tmp/opencode/dual-mute-b.wav")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, x, rate=self.RATE)
        res = analyze_dual(str(path), verbose=False)
        self.assertFalse(res["ok"], f"B 静音时不能报成功: {res}")

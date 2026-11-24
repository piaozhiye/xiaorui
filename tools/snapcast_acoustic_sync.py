#!/usr/bin/env python3
"""双设备声学同步测量(PC + RK3562 各当一台播放端)。

原理:
  snapcast 保证两台客户端播放**同一条 PCM 时间线**的同一个采样点(按 server 时钟对齐),
  所以"两台设备听到同一个脉冲的时刻差" = 两台设备的输出延迟差 + 麦克风的几何偏移。
  几何偏移是常数(麦克风到两个喇叭的距离差 / 343 m/s), 减去即可。

测量链:
  1) 标定段: 只开 PC 播 -> 录 6s; 只开板子播 -> 录 6s
     (认准每路的脉冲幅度与波形, 后面两路同录时据此归属)
  2) 测量段: 两路同时播 -> 录 N s
     逐秒做 3kHz 突发匹配滤波, 找出两个到达峰, 算每对的到达时间差
  3) 报 median / p95 / max(两路到达时间差) 与各自的抖动

用法:
  tools/snapcast-acoustic-sync.py --serial <SERIAL> --seconds 60
  tools/snapcast-acoustic-sync.py --analyze-only --out /tmp/opencode/acoustic   # 只重算

注意:
  - 麦克风请放在**两个喇叭之间**(或者你方便的位置, 但脚本会按实际几何算补偿? 不, 脚本只报
    原始差值, 几何偏移请自己按距离算: dt = (d1-d2)/343 m/s)
  - 板端要出声必须先停前端(板子的 hw 播放是独占的), 脚本结束会自动恢复
  - 板端客户端要连 PC 的**真实 Wi-Fi 地址**, 不要走 adb 隧道(隧道不代表真实网络路径)
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import wave

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE_FREQ = 3000.0      # 同步脉冲频率(Hz)
PULSE_MS = 5.0           # 脉冲宽度(ms)
PULSE_PERIOD = 1.0       # 每秒一个脉冲(s)
SAMPLE_RATE = 48000
SNAPCAST_VERSION = "v0.35.0"    # 探针按这个版本的客户端行为标定


# ---------------------------------------------------------------- 工具

def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def adb(serial, cmd, timeout=120):
    return subprocess.run(["adb", "-s", serial, "shell", cmd],
                          capture_output=True, text=True, timeout=timeout)


def find_pulse_train(x, rate, freq=PULSE_FREQ):
    """匹配滤波出脉冲到达时刻。

    返回 (峰时刻列表, 峰幅度列表)。用 3kHz 突发做模板, FFT 互相关。
    """
    n = int(rate * PULSE_MS / 1000.0)
    t = np.arange(n) / rate
    template = np.sin(2 * np.pi * freq * t).astype(np.float64)
    # 归一化互相关
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    corr = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(template, size)), size)[:len(x)]
    env = np.abs(corr)
    # 平滑一下, 去掉载波起伏
    win = max(1, int(rate * 0.0005))
    env = np.convolve(env, np.ones(win) / win, mode="same")
    return env


def find_peaks_in_windows(env, rate, count, guard_ms=1.5, want=2):
    """把录音按 1 秒切成窗口, 每个窗口里找最强的 `want` 个峰(彼此相隔超过 guard_ms)。"""
    guard = int(rate * guard_ms / 1000.0)
    out = []
    for k in range(count):
        lo = int(k * PULSE_PERIOD * rate)
        hi = lo + int(PULSE_PERIOD * rate)
        if hi > len(env):
            break
        seg = env[lo:hi].copy()
        # 抹掉窗口开头(上一个脉冲的尾巴)
        if lo > 0:
            seg[:guard] = 0
        peaks = []
        work = seg.copy()
        for _ in range(want):
            idx = int(np.argmax(work))
            if work[idx] <= 0:
                break
            peaks.append((lo + idx, float(work[idx])))
            a = max(0, idx - guard)
            b = min(len(work), idx + guard)
            work[a:b] = 0
        peaks.sort()
        out.append(peaks if len(peaks) == want else None)
    return out


def analyze(wav_path, expect_pairs=True, verbose=True, amps=None):
    """分析一段录音。

    expect_pairs=True  : 测量段, 每个 1 秒窗口里应有**两路**脉冲 -> 算到达时间差
    expect_pairs=False : 标定段, 只有一路 -> 报每窗口的脉冲时刻与幅度
    amps               : {"A": 幅度, "B": 幅度} 标定得到的幅度指纹。给了就按幅度
                         就近把每个峰归给 A/B(而不是"先到=近的"), 顺序不再混淆。
    """
    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    env = find_pulse_train(data, rate)
    duration = len(data) / rate
    count = int(duration / PULSE_PERIOD)
    want = 2 if expect_pairs else 1
    windows = find_peaks_in_windows(env, rate, count, want=want)
    hits = [w for w in windows if w]
    if not hits:
        return {"ok": False, "error": "没找到脉冲", "duration": duration}

    if not expect_pairs:
        times = [p[0][0] / rate for p in hits]
        amps = [p[0][1] for p in hits]
        res = {
            "ok": True,
            "duration": duration,
            "mode": "single",
            "pulses": len(hits),
            "pulse_times_s": [round(t, 4) for t in times],
            "amp_median": float(np.median(amps)),
            "amp_min": float(np.min(amps)),
        }
        if verbose:
            print(f"  录音 {duration:.1f}s, 单路脉冲 {len(hits)} 个, "
                  f"幅度中位 {res['amp_median']:.0f} (最小 {res['amp_min']:.0f})")
            print(f"  脉冲时刻(相对录音起点): {res['pulse_times_s'][:6]} ...")
        return res

    # 每个窗口里两个峰 -> 算 B 相对 A 的到达时间
    deltas = []
    ordered = []          # (tA, aA, tB, aB) 便于复核
    for p in hits:
        (t1, a1), (t2, a2) = p
        if amps:
            # 按幅度指纹归: 谁更接近 A 的标定幅度, 谁就是 A
            d1 = abs(np.log(max(a1, 1e-9)) - np.log(max(amps["A"], 1e-9)))
            d2 = abs(np.log(max(a2, 1e-9)) - np.log(max(amps["A"], 1e-9)))
            if d1 <= d2:
                tA, aA, tB, aB = t1, a1, t2, a2
            else:
                tA, aA, tB, aB = t2, a2, t1, a1
        else:
            # 没指纹: 默认先到的是 A
            tA, aA, tB, aB = t1, a1, t2, a2
        deltas.append((tB - tA) / rate)
        ordered.append((tA / rate, aA, tB / rate, aB))
    amp_first = [o[1] for o in ordered]
    amp_second = [o[3] for o in ordered]
    res = {
        "ok": True,
        "duration": duration,
        "mode": "pair",
        "assignment": "fingerprint" if amps else "first-arrival",
        "pairs": len(hits),
        "delta_ms": {
            "median": float(np.median(deltas) * 1000),
            "p95": float(np.percentile(deltas, 95) * 1000),
            "max": float(np.max(deltas) * 1000),
            "min": float(np.min(deltas) * 1000),
            "std": float(np.std(deltas) * 1000),
        },
        "amp_first_median": float(np.median(amp_first)),
        "amp_second_median": float(np.median(amp_second)),
        "per_pair_delta_ms": [round(d * 1000, 3) for d in deltas],
    }
    if verbose:
        print(f"  录音 {duration:.1f}s, 配对 {len(hits)} 对")
        print(f"  两路到达时间差: median {res['delta_ms']['median']:.2f} ms  "
              f"p95 {res['delta_ms']['p95']:.2f} ms  max {res['delta_ms']['max']:.2f} ms  "
              f"std {res['delta_ms']['std']:.2f} ms")
        who = "按幅度指纹" if amps else "按先到/后到"
        print(f"  归属方式: {who}   A 峰幅度 {res['amp_first_median']:.0f} / B 峰幅度 {res['amp_second_median']:.0f}")
        print(f"  B 相对 A 的到达时间: {res['delta_ms']['median']:+.2f} ms (正=B 晚)")
    return res


# ---------------------------------------------------------------- 单次连续录音法

def find_pulse_train_chunked(x, rate, freq=PULSE_FREQ, chunk_s=60.0):
    """分块做匹配滤波, 长录音(10 分钟)也不吃几 GB 内存。

    每块多读一个模板长度, 保证块边界的脉冲也完整; 结果存 float32。
    """
    n = int(rate * PULSE_MS / 1000.0)
    step = int(chunk_s * rate)
    env = np.zeros(len(x), dtype=np.float32)
    for i in range(0, len(x), step):
        seg = x[i:min(len(x), i + step + n)]
        if len(seg) < n:
            break
        e = find_pulse_train(seg, rate, freq).astype(np.float32)
        take = min(len(seg), len(x) - i)
        env[i:i + take] = e[:take]
    return env


def find_all_peaks(env, rate, min_gap_ms=10.0, threshold_ratio=0.1, ref_max=None):
    """在整段录音里找所有匹配滤波峰(按幅度从高到低, 彼此至少间隔 min_gap_ms)。"""
    gap = max(1, int(rate * min_gap_ms / 1000.0))
    work = env.copy()
    # ref_max: 用"两路里较强那一路"当参考, 而不是各自的最大值 ——
    # 否则一路静音时, 它会在自己的噪声里"找到峰", 存在性检查就失效了(复核发现)
    peak_val = float(env.max() if ref_max is None else ref_max)
    if not np.isfinite(peak_val) or peak_val <= 0:
        return []                      # 静音: 直接返回, 否则阈值 0 会死循环
    thr = peak_val * threshold_ratio
    peaks = []
    while True:
        idx = int(np.argmax(work))
        if work[idx] <= 0 or work[idx] < thr:
            break
        peaks.append((idx / rate, float(work[idx])))
        a = max(0, idx - gap)
        b = min(len(work), idx + gap)
        work[a:b] = 0
    peaks.sort()
    return peaks


def one_peak_per_second(peaks, t0, t1, period=PULSE_PERIOD):
    """每 1 秒窗口只保留最强的那个峰。

    匹配滤波对一个 5ms 突发会在它周围产生若干旁瓣峰; 直接把"所有峰"拿去做相位/拟合
    会被这些旁瓣污染(实测一秒能找出 5-6 个峰, 漂移拟合直接算成 0 ppm)。
    """
    out = []
    k = int(np.floor(t0 / period))
    while k * period < t1:
        lo = max(k * period, t0)          # 窗口要跟标定段求交, 别把段外的峰拿进来
        hi = min((k + 1) * period, t1)
        seg = [p for p in peaks if lo <= p[0] < hi]
        if seg:
            out.append(max(seg, key=lambda x: x[1]))
        k += 1
    return out


def pulse_phase(peaks, t0, t1, period=PULSE_PERIOD):
    """某时间段的脉冲相位(圆均值) + 圆标准差(ms)。

    相位 = 脉冲相对录音 1s 栅格的位置。同一段连续录音里, 两台板子各自的相位
    可以直接相减 -> 就是它们的到达时间差, 而且没有"谁是谁"的歧义。
    """
    sel = [p[0] for p in one_peak_per_second(peaks, t0, t1, period)]
    if not sel:
        return None, 0, None
    ang = np.array([2 * np.pi * (x % period) / period for x in sel])
    z = np.exp(1j * ang).mean()
    phase = (np.angle(z) % (2 * np.pi)) / (2 * np.pi) * period
    # 圆标准差 -> 折成 ms
    R = abs(z)
    circ_std = np.sqrt(-2 * np.log(max(R, 1e-12))) / (2 * np.pi) * period
    return float(phase), len(sel), float(circ_std * 1000)


def fit_segment(peaks, t0, t1, period=PULSE_PERIOD):
    """对一段里的脉冲做线性拟合 t_k = a + b*k, 返回 (a, ppm 漂移, 脉冲数)。

    a 是 k=0 处(该段第一个脉冲)的拟合时刻 -> `a %% period` 才是脉冲相位。
    b 是拟合周期 -> (b/period - 1)*1e6 是相对漂移(ppm)。

    ⚠ 之前的实现用 pos_mid = a + b*(t_mid-a)/b, 代数上恒等于 t_mid —— 返回的其实是
    段中点相位, 已知 380ms 的相位会被算成 0(复核发现)。这里直接返回 a。
    """
    sel = [p[0] for p in one_peak_per_second(peaks, t0, t1, period)]
    if len(sel) < 4:
        return None
    k = np.round((np.array(sel) - sel[0]) / period).astype(int)
    b, a = np.polyfit(k, np.array(sel), 1)
    return float(a), float((b / period - 1.0) * 1e6), len(sel)


def phase_diff_ms(pa, pb, period=PULSE_PERIOD):
    """两个相位的环差(B-A), 单位 ms, 落在 (-period/2, period/2]。"""
    d = (pb - pa + period / 2) % period - period / 2
    return d * 1000


def provenance(serial=None, board_bin=None, status_path=None, queue_target_ms=200):
    """采集产物要带的可追溯信息(P0 要求): 脚本版本、二进制哈希、板端配置、队列目标。"""
    import hashlib
    info = {}
    try:
        r = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        info["repo_commit"] = r.stdout.strip() or None
        r = subprocess.run(["git", "-C", REPO, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=5)
        info["repo_dirty"] = bool(r.stdout.strip())
    except Exception:
        pass
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            info["script_sha256"] = hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        pass
    info["queue_target_ms"] = queue_target_ms
    info["snapclient"] = SNAPCAST_VERSION
    if serial and board_bin:
        r = adb(serial, f"sha256sum {board_bin} 2>/dev/null | cut -c1-16")
        info["board_bin_sha256"] = r.stdout.strip() or None
    if serial and status_path:
        r = adb(serial, f"cat /proc/asound/$(dirname $(dirname {status_path}) | sed 's#.*/##')/hw_params 2>/dev/null")
        hp = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                hp[k.strip()] = v.strip()
        if hp:
            info["hw_params"] = hp
    return info


def assess_quality(peaks, t0, t1, period=PULSE_PERIOD, max_std_ms=15.0, min_ratio=0.5, min_count=3):
    """一段里的脉冲质量自检: 每秒应恰好 1 个脉冲, 且相位要聚得住。

    阈值依据实测: 单台(有房间反射)的相位抖动约 2.5~10ms, 而"听到的不是脉冲"
    (音源没放/被污染)时是 58~155ms —— 15ms 能干净区分。
    注意: 两台同步好时"逐窗口差值"的 std 会远小于单台的相位抖动(共模抵消),
    所以不能拿单台 std 当成同步精度。

    返回 (ok, 说明)。圆 std > max_std_ms 说明抓到的不是干净脉冲(噪声/别的音源),
    检出率 < min_ratio 说明漏检。分析绝不能在这些情况下给数字。
    """
    sel = one_peak_per_second(peaks, t0, t1, period)
    expect = max(1, int((t1 - t0) / period))
    # 漏检位置(按 1s 窗口编号), 便于定位是过渡丢包还是真的没声
    got = {int(round((x[0] - t0) / period)) for x in sel}
    missed = [k for k in range(expect) if k not in got]
    miss_txt = f", 漏检窗口 {missed[:8]}" if missed else ""
    if not sel:
        return False, f"0/{expect} 个脉冲{miss_txt}"
    ang = np.array([2 * np.pi * ((x[0] % period) / period) for x in sel])
    z = np.exp(1j * ang).mean()
    std_ms = float(np.sqrt(-2 * np.log(max(abs(z), 1e-12))) / (2 * np.pi) * period * 1000)
    ratio = len(sel) / expect
    # 静音切换的过渡(HTTP + 环预滚/排空)每段会吃掉 1~3 秒脉冲 —— 实测每段
    # 只剩 5/8 左右, 但这些脉冲本身很干净(圆std <1ms). 所以不要求高检出率,
    # 只要求"够用的样本 + 相位聚得住", 否则会把有效数据误判成无效.
    if len(sel) < min_count:
        return False, f"样本太少 {len(sel)} < {min_count} 个{miss_txt}"
    if ratio < min_ratio:
        return False, f"检出率 {len(sel)}/{expect} < {min_ratio:.0%}{miss_txt}"
    if std_ms > max_std_ms:
        return False, f"相位圆std {std_ms:.1f} ms > {max_std_ms:.0f} ms(不是干净脉冲){miss_txt}"
    return True, f"{len(sel)}/{expect} 个({ratio:.0%}), 圆std {std_ms:.2f} ms{miss_txt}"


def preflight(host_card, seconds=6.0, wav_out="/tmp/opencode/preflight.wav"):
    """起测前的现场自检: 录一小段, 确认听到的是规则脉冲(而不是噪声/别的音源)。

    6 秒(5~6 个脉冲)才够统计: 4 秒只有 2~3 个, 漏一个就从 100% 掉到 67%,
    会把"其实没问题"误判成不通过(实测踩到)。同时打印逐脉冲时刻/幅度,
    漏检出时能直接看出漏在哪。
    """
    record(host_card, seconds, wav_out)
    with wave.open(wav_out, "rb") as w:
        rate = w.getframerate(); raw = w.readframes(w.getnframes()); ch = w.getnchannels()
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    env = find_pulse_train(x, rate)
    peaks = find_all_peaks(env, rate)
    ok, msg = assess_quality(peaks, 0.5, seconds - 0.5)
    level = float(np.sqrt((x * x).mean()))
    sel = one_peak_per_second(peaks, 0.5, seconds - 0.5)
    detail = " ".join(f"{t_:.2f}s/{a:.0f}" for t_, a in sel)
    return ok, f"电平 RMS {level:.0f}, 脉冲质量: {msg}; 逐个: [{detail}]"


def analyze_single(wav_path, cal_seconds, verbose=True):
    """单次连续录音的分析。

    录音时间线(由调用方用客户端静音切出来):
        [0, cal)         只有 A 出声
        [cal, 2*cal)     只有 B 出声
        [2*cal, end)     两路同时
    用前两段拿到 A/B 各自的"脉冲相位"(脉冲相对录音 1s 栅格的位置),
    第三段里每个窗口找两个峰, 按相位就近归属 -> 算 B 相对 A 的到达时间。
    """
    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    env = find_pulse_train(data, rate)
    duration = len(data) / rate
    peaks = find_all_peaks(env, rate)

    # 静音切换的过渡: HTTP 往返 + 前端环的预滚/排空 + 客户端软件音量生效,
    # 实测边界附近会丢 2~3 个脉冲 -> 保护带取 1.5s(实测: 0.4s 时检出率只有 6/9).
    # 但要按段长收敛: 短段(合成自测 2s)会被吃光, 取段长的 20% 上限。
    guard = min(1.5, cal_seconds * 0.2)
    # ★ 质量自检: 两段标定都必须"每秒一个干净脉冲", 否则直接判无效
    qa, ma = assess_quality(peaks, guard, cal_seconds - guard)
    qb, mb = assess_quality(peaks, cal_seconds + guard, 2 * cal_seconds - guard)
    if not (qa and qb):
        res_bad = {"ok": False, "duration": duration, "mode": "single-recording",
                   "error": f"脉冲质量不合格: A [{ma}] / B [{mb}]"}
        if verbose:
            print(f"  录音 {duration:.1f}s, 峰 {len(peaks)} 个")
            print(f"  !! 判无效: A [{ma}] / B [{mb}]")
            print("     (说明听到的不是规则脉冲: 音源没在放/音量太低/被别的音源污染)")
        return res_bad
    phase_a, na, sd_a = pulse_phase(peaks, guard, cal_seconds - guard)
    phase_b, nb, sd_b = pulse_phase(peaks, cal_seconds + guard, 2 * cal_seconds - guard)
    res = {"ok": False, "duration": duration, "peaks": len(peaks),
           "phase_a": phase_a, "phase_b": phase_b,

           "pulses_a": na, "pulses_b": nb,
           "phase_a_std_ms": sd_a, "phase_b_std_ms": sd_b,
           "phase_a_n": na, "phase_b_n": nb}
    if phase_a is None or phase_b is None:
        res["error"] = "标定段没找到脉冲"
        return res

    # 两路段的配对
    period = PULSE_PERIOD
    def circ_dist(x, y):
        d = abs(x - y) % period
        return min(d, period - d)

    deltas = []
    k = int(np.ceil(2 * cal_seconds)) + 1
    while True:
        t0 = k * period
        t1 = t0 + period
        if t1 > duration:
            break
        seg = [p for p in peaks if t0 <= p[0] < t1]
        # 每窗取最强的两个(相隔 > 0.5ms 的两个独立峰)
        seg.sort(key=lambda p: -p[1])
        pick = []
        for tt, aa in seg:
            if all(abs(tt - u) > 0.0003 for u, _ in pick):
                pick.append((tt, aa))
            if len(pick) == 2:
                break
        if len(pick) == 2:
            pick.sort()
            (t1p, a1p), (t2p, a2p) = pick
            # 归属: 谁的相位更接近标定相位, 谁就是谁
            if circ_dist(t1p % period, phase_a) <= circ_dist(t1p % period, phase_b):
                tA, tB = t1p, t2p
            else:
                tA, tB = t2p, t1p
            deltas.append(tB - tA)
        k += 1

    # 配对只是参考: 两路脉冲重叠(<1个脉冲宽)时它们本来就分不开, 不能因此判失败
    deltas = np.array(deltas) if deltas else np.array([])
    # 主指标: 两段标定的**原始相位差**(同一录音、同一时钟).
    # ★ 不做漂移拟合并外推: 交接文档明确要求"不能拟合或补值"; 而且那条路径曾因
    #   (t*-a)%T 的符号约定把 +3ms 算成 -3ms(自测抓到).
    primary = phase_diff_ms(phase_a, phase_b)

    res.update({
        "ok": True,
        "mode": "single-recording",
        # 主指标: 两段标定的相位差(同一录音、同一时钟、无归属歧义)
        "phase_diff_ms": primary,
        # 不确定度: 两段相位各自的圆std/sqrt(n) 合成(保守, 忽略共模)
        "uncertainty_ms": (round(float(np.sqrt(
            (sd_a or 0) ** 2 / max(na, 1) + (sd_b or 0) ** 2 / max(nb, 1))), 2)
            if na and nb else None),
        "pairs": len(deltas),
        "delta_ms": ({
            "median": float(np.median(deltas) * 1000),
            "p95": float(np.percentile(deltas, 95) * 1000),
            "max": float(np.max(deltas) * 1000),
            "min": float(np.min(deltas) * 1000),
            "std": float(np.std(deltas) * 1000),
        } if len(deltas) else None),
        "per_pair_delta_ms": [round(x * 1000, 3) for x in deltas],
    })
    if verbose:
        print(f"  单次录音 {duration:.1f}s, 峰 {len(peaks)} 个")
        print(f"  A 相位 {phase_a*1000:.2f} ms (n={na}, 圆std {sd_a:.2f} ms) / "
              f"B 相位 {phase_b*1000:.2f} ms (n={nb}, 圆std {sd_b:.2f} ms)")
        unc = res.get("uncertainty_ms")
        print(f"  >>> B 相对 A 的到达时间差 = {primary:+.2f} ms"
              + (f"  (不确定度 ±{unc:.2f} ms)" if unc is not None else ""))
        # 两路段的配对只在两路脉冲"分得开"时才有意义; 分不开(重叠)时配不出对, 正常
        if len(deltas):
            print(f"  [参考, 两路段配对] {len(deltas)} 对: median {res['delta_ms']['median']:+.2f} ms")
        else:
            print("  [参考, 两路段配对] 两路脉冲重叠, 分不开(说明同步很好), 跳过")
    return res


# ---------------------------------------------------------------- 双频探针法(推荐)

def analyze_dual(wav_path, verbose=True, freq_a=PULSE_FREQ, freq_b=5000.0,
                 period=PULSE_PERIOD):
    """双频探针: 一条流里 L 声道是 freq_a 脉冲(板A), R 声道是 freq_b 脉冲(板B)。

    两台各自只放自己那个声道(板端 asound.conf 里的 sync_left / sync_right),
    所以同一条流、同一条时间线, 但**声学上频率不同, 天然能分源**。
    关键: 每个 1 秒窗口必须**两个频率都能找到脉冲**, 否则说明有一台没出声 -> 判失败。
    (复核指出: 只比先后标定的相位, 同播段全静音也能"通过", 这不成立。)
    """
    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    duration = len(data) / rate
    windows = int(duration / period)
    if duration > 120:
        env_a = find_pulse_train_chunked(data, rate, freq=freq_a)
        env_b = find_pulse_train_chunked(data, rate, freq=freq_b)
    else:
        env_a = find_pulse_train(data, rate, freq=freq_a)
        env_b = find_pulse_train(data, rate, freq=freq_b)
    ref = max(float(env_a.max()), float(env_b.max()))
    peaks_a = one_peak_per_second(find_all_peaks(env_a, rate, ref_max=ref), 0, duration, period)
    peaks_b = one_peak_per_second(find_all_peaks(env_b, rate, ref_max=ref), 0, duration, period)

    deltas = []
    detail = []
    win_a = win_b = 0
    for k in range(windows):
        lo, hi = k * period, (k + 1) * period
        pa = [p for p in peaks_a if lo <= p[0] < hi]
        pb = [p for p in peaks_b if lo <= p[0] < hi]
        if pa:
            win_a += 1
        if pb:
            win_b += 1
        d = None
        if pa and pb:
            ta = max(pa, key=lambda x: x[1])[0]
            tb = max(pb, key=lambda x: x[1])[0]
            d = tb - ta
            deltas.append(d)
        detail.append({"k": k, "t": round(lo, 3),
                       "a": bool(pa), "b": bool(pb),
                       "delta_ms": round(d * 1000, 3) if d is not None else None})

    res = {
        "ok": False,
        "mode": "dual-probe",
        "duration": duration,
        "windows": windows,
        "windows_with_a": win_a,
        "windows_with_b": win_b,
        "pairs": len(deltas),
        "freq_a": freq_a,
        "freq_b": freq_b,
        "windows_detail": detail,
    }
    need = max(3, int(np.ceil(0.9 * windows)))
    if win_a < need or win_b < need or len(deltas) < need:
        res["error"] = (f"两路没有全程都在: A 有 {win_a}/{windows} 个窗口, "
                        f"B 有 {win_b}/{windows} (要求 >= {need}); 配出 {len(deltas)} 对")
        if verbose:
            print(f"  录音 {duration:.1f}s, 窗口 {windows}, A {win_a}, B {win_b}, 配对 {len(deltas)}")
            print(f"  !! 判失败: {res['error']}")
        return res

    d = np.array(deltas)
    res.update({
        "ok": True,
        "delta_ms": {
            "median": float(np.median(d) * 1000),
            "p95": float(np.percentile(d, 95) * 1000),
            "max": float(np.max(d) * 1000),
            "min": float(np.min(d) * 1000),
            "std": float(np.std(d) * 1000),
        },
        "per_pair_delta_ms": [round(x * 1000, 3) for x in d],
        "windows_detail": detail,
    })
    if verbose:
        dm = res["delta_ms"]
        print(f"  录音 {duration:.1f}s, 窗口 {windows} (A {win_a}, B {win_b}), 配对 {len(d)}")
        print(f"  B 相对 A: median {dm['median']:+.2f} ms  p95 {dm['p95']:+.2f}  "
              f"max {dm['max']:+.2f}  std {dm['std']:.2f}")
    return res


# ---------------------------------------------------------------- 现场

SYNC_PCM_CONF = """
# ---- Snapcast 双频探针用(工具自动追加, 可整段删除) ----
# 只把左声道/右声道送到两个喇叭, 用于"板A 放 3kHz / 板B 放 5kHz"
pcm.sync_left {
    type route
    slave.pcm "xr_spk"
    slave.channels 2
    ttable.0.0 1.0
    ttable.0.1 1.0
}
pcm.sync_right {
    type route
    slave.pcm "xr_spk"
    slave.channels 2
    ttable.1.0 1.0
    ttable.1.1 1.0
}
# ---- 双频探针配置结束 ----
"""


def ensure_sync_pcms(serial):
    """把 sync_left / sync_right 追加到板端 asound.conf(幂等)。

    注意: `grep -c` 在没匹配时打印 0 且退出码为 1, 后面再加 `|| echo 0` 会输出
    "0\n0" —— 之前就是被这个误判成"已经装过", 结果根本没装上。
    """
    def count():
        out = adb(serial, "grep -c 'pcm.sync_left' /etc/asound.conf 2>/dev/null").stdout
        return out.strip() or "0"

    if count() not in ("", "0"):
        return True
    adb(serial, "cp /etc/asound.conf /etc/asound.conf.bak.sync 2>/dev/null || true")
    with open("/tmp/opencode/_sync_pcm.conf", "w") as f:
        f.write(SYNC_PCM_CONF)
    subprocess.run(["adb", "-s", serial, "push", "/tmp/opencode/_sync_pcm.conf",
                    "/tmp/sync_pcm.conf"], capture_output=True)
    adb(serial, "cat /tmp/sync_pcm.conf >> /etc/asound.conf; rm -f /tmp/sync_pcm.conf")
    return count() not in ("", "0")


def wait_playing(serial, status_path, timeout=20.0):
    """等这块板子的声卡真的在跑(state=RUNNING 且 hw_ptr 在推进), 最多等 timeout 秒。

    只 sleep 固定几秒是不够的: 板子连 Wi-Fi / 客户端启动 / 缓冲填充的耗时不一样,
    之前板 B 就是这样被录成了"没有声音"。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = adb(serial, f"cat {status_path} 2>/dev/null | grep -E '^(state|hw_ptr)'")
        out = r.stdout
        state = None
        ptr = None
        for line in out.splitlines():
            if line.startswith("state:"):
                state = line.split(":", 1)[1].strip()
            elif line.startswith("hw_ptr"):
                try:
                    ptr = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        if state == "RUNNING" and ptr is not None and ptr != last:
            return True, ptr
        last = ptr
        time.sleep(0.5)
    return False, last


def start_recorder(host_card, seconds, path, channels=1):
    """后台开录音, 返回 Popen(冷启测试要"先录再起客户端")。"""
    cmd = (f"arecord -D {host_card} -f S16_LE -r {SAMPLE_RATE} -c {channels} "
           f"-d {int(seconds)} {path}")
    return subprocess.Popen(cmd, shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def record(host_card, seconds, path):
    # USB 麦是单声道; 旧的内置麦(ALC257)只支持双声道。按设备自适应。
    cmd = (f"arecord -D {host_card} -f S16_LE -r {SAMPLE_RATE} -c 1 "
           f"-d {int(seconds)} {path}")
    r = sh(cmd, timeout=seconds + 60)
    if not os.path.exists(path):
        raise RuntimeError(f"录音失败: {r.stderr[:300]}")
    return path


def run_dual(args):
    """双频探针: L=3kHz 给 A, R=5kHz 给 B, 同一条流; 免标定, 天然验"两路都在"。"""
    if not args.serial or not args.serial2:
        print("dual 模式需要 --serial 和 --serial2", file=sys.stderr)
        return 2
    state = {"frontend": False}
    procs = []

    def cleanup():
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        if state["frontend"]:
            print("== 恢复板端前端 ==")
            adb(args.serial, "pkill -x xiaorui_live 2>/dev/null; sleep 2")
            adb(args.serial, "(setsid sh /data/xiaorui/start-frontend.sh "
                             ">>/data/xiaorui/frontend.log 2>&1 </dev/null &); true")
            time.sleep(4)
            r = adb(args.serial, "pgrep -x xiaorui_live")
            print("  前端" + ("已恢复" if r.stdout.strip() else "!! 没起来"))

    try:
        r = adb(args.serial, "pgrep -x xiaorui_live")
        state["frontend"] = bool(r.stdout.strip())
        if state["frontend"]:
            print("== 停板A 前端(腾出独占的 hw) ==")
            adb(args.serial, "pkill -x xiaorui_live 2>/dev/null; sleep 2")

        print("== 清掉板上可能残留的客户端(保证是干净的起点) ==")
        for s in (args.serial, args.serial2):
            adb(s, "pkill -x snapclient 2>/dev/null; true")
        time.sleep(1)

        print("== 装板端 sync_left / sync_right 声道路由 ==")
        for s in (args.serial, args.serial2):
            ok = ensure_sync_pcms(s)
            print(f"  {s}: " + ("ok" if ok else "!! 失败"))

        fifo = "/tmp/opencode/snapcast/dualfifo"
        os.makedirs("/tmp/opencode/snapcast", exist_ok=True)
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
        conf = os.path.join(args.out, "server-dual.conf")
        with open(conf, "w") as f:
            f.write(f"""[server]
threads = 2
mdns_enabled = 0
[stream]
source = pipe://{fifo}?name=dual
codec = pcm
sampleformat = 48000:16:2
chunk_ms = 20
buffer = 1000
[tcp-control]
enabled = 1
port = 17105
[tcp-streaming]
enabled = 1
port = 17104
[http]
enabled = 1
port = 17180
""")
        print("== 起 snapserver(17104) ==")
        procs.append(subprocess.Popen([args.server_bin, "-c", conf],
                                      stdout=open(os.path.join(args.out, "server.log"), "w"),
                                      stderr=subprocess.STDOUT))
        time.sleep(2)
        # start_new_session: 让 ffmpeg 自成进程组, 暂停时能连子进程一起杀掉
        # (shell=True 时 terminate() 只杀 /bin/sh, ffmpeg 会活着 -> 之前"暂停"无效)
        feeder = subprocess.Popen(
            f"ffmpeg -re -hide_banner -loglevel error -f s16le -ar 48000 -ac 2 "
            f"-i {args.signal} -f s16le pipe:1 > {fifo}",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        procs.append(feeder)
        time.sleep(2)

        def start_board(serial, card, tag):
            host = args.host_ip2 if (tag == "B" and args.host_ip2) else args.host_ip
            cmd = (f"cd /data/xiaorui/snapcast && (setsid {args.board_client} "
                   f"tcp://{host}:17104 --soundcard '{card}' --player '{args.player1}' "
                   f"--mixer software --logsink stdout --logfilter '*:warn' "
                   f"> /data/xiaorui/snapcast/sync-{tag}.log 2>&1 &); true")
            adb(serial, cmd)
            return wait_playing(serial, args.status1 if tag == "A" else args.status2)

        print("== 起两台播放端(各放自己那个声道) ==")
        ok_a, _ = start_board(args.serial, "sync_left", "A")
        print(f"  A " + ("在播" if ok_a else "!! 没出声"))
        ok_b, _ = start_board(args.serial2, "sync_right", "B")
        print(f"  B " + ("在播" if ok_b else "!! 没出声"))
        if not (ok_a and ok_b):
            print("!! 有板子没出声", file=sys.stderr)
            return 1

        rec_path = os.path.join(args.out, "dual.wav")
        # ★ 起测前自检: 确认听到的是规则脉冲(否则后面的数都是噪声)
        if not args.skip_preflight:
            ok, msg = preflight(args.host_card)
            print(f"== 起测自检: {'通过' if ok else '不通过'} ({msg}) ==")
            if not ok:
                print("!! 自检不通过, 不开始正式测量(音源/音量/摆放先确认)", file=sys.stderr)
                return 1
        if args.cold_start:
            print(f"== 冷启: 先录音 {args.seconds:.0f}s, 2s 后再起客户端 ==")
            rec = start_recorder(args.host_card, args.seconds, rec_path)
            time.sleep(2)
            t0 = time.time()
            ok_a, _ = start_board(args.serial, "sync_left", "A")
            ok_b, _ = start_board(args.serial2, "sync_right", "B")
            print(f"  客户端起完用时 {time.time()-t0:.1f}s (A={'在播' if ok_a else '无'} B={'在播' if ok_b else '无'})")
            try:
                rec.wait(timeout=args.seconds + 20)
            except subprocess.TimeoutExpired:
                rec.terminate()
        elif args.pause_at > 0:
            print(f"== 录音 {args.seconds:.0f}s, 第 {args.pause_at:.0f}s 暂停音源 {args.pause_for:.0f}s ==")
            rec = start_recorder(args.host_card, args.seconds, rec_path)
            time.sleep(args.pause_at)
            print(f"  [{args.pause_at:.0f}s] 暂停音源(杀整个进程组)")
            try:
                os.killpg(os.getpgid(feeder.pid), signal.SIGTERM)
            except Exception as exc:
                print("  杀 feeder 失败:", exc)
            time.sleep(args.pause_for)
            print("  恢复音源")
            new_feeder = subprocess.Popen(
                f"ffmpeg -re -hide_banner -loglevel error -f s16le -ar 48000 -ac 2 "
                f"-i {args.signal} -f s16le pipe:1 > {fifo}",
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            procs.append(new_feeder)
            try:
                rec.wait(timeout=args.seconds + 20)
            except subprocess.TimeoutExpired:
                rec.terminate()
        else:
            print(f"== 录音 {args.seconds:.0f}s(两路同时在播) ==")
            record(args.host_card, args.seconds, rec_path)

        print()
        print("== 分析(双频探针) ==")
        res = analyze_dual(rec_path)
        with open(os.path.join(args.out, "dual-summary.json"), "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, default=float)
        print(f"产物: {args.out}/dual.wav + dual-summary.json")
        return 0 if res.get("ok") else 1
    finally:
        cleanup()


def run_single(args):
    """单次连续录音法: 一段录音里用客户端静音切出 [A] [B] [A+B] 三段。"""
    if not args.serial or not args.serial2:
        print("single 模式需要 --serial 和 --serial2", file=sys.stderr)
        return 2
    sys.path.insert(0, os.path.join(REPO, "tools", "snapcast_center"))
    from snapcast_control import SnapcastClient  # noqa: E402

    state = {"frontend": False}
    procs = []

    def cleanup():
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        if state["frontend"]:
            print("== 恢复板端前端 ==")
            adb(args.serial, "pkill -x xiaorui_live 2>/dev/null; sleep 2")
            adb(args.serial, "(setsid sh /data/xiaorui/start-frontend.sh "
                             ">>/data/xiaorui/frontend.log 2>&1 </dev/null &); true")
            time.sleep(4)
            r = adb(args.serial, "pgrep -x xiaorui_live")
            print("  前端" + ("已恢复" if r.stdout.strip() else "!! 没起来"))

    try:
        r = adb(args.serial, "pgrep -x xiaorui_live")
        state["frontend"] = bool(r.stdout.strip())
        if state["frontend"]:
            print("== 停板A 前端(腾出独占的 hw) ==")
            adb(args.serial, "pkill -x xiaorui_live 2>/dev/null; sleep 2")

        # server + 实时脉冲源
        fifo = "/tmp/opencode/snapcast/syncfifo"
        os.makedirs("/tmp/opencode/snapcast", exist_ok=True)
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
        conf = os.path.join(args.out, "server.conf")
        with open(conf, "w") as f:
            f.write(f"""[server]
threads = 2
mdns_enabled = 0
[stream]
source = pipe://{fifo}?name=sync
codec = pcm
sampleformat = 48000:16:2
chunk_ms = 20
buffer = 1000
[tcp-control]
enabled = 1
port = 17105
[tcp-streaming]
enabled = 1
port = 17104
[http]
enabled = 1
port = 17180
""")
        print("== 起 snapserver(17104/17180) ==")
        procs.append(subprocess.Popen([args.server_bin, "-c", conf],
                                      stdout=open(os.path.join(args.out, "server.log"), "w"),
                                      stderr=subprocess.STDOUT))
        time.sleep(2)
        procs.append(subprocess.Popen(
            f"ffmpeg -re -hide_banner -loglevel error -f s16le -ar 48000 -ac 2 "
            f"-i {args.signal} -f s16le pipe:1 > {fifo}",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(2)

        def start_board(serial, card, tag, player, status):
            host = args.host_ip2 if (tag == "B" and args.host_ip2) else args.host_ip
            cmd = (f"cd /data/xiaorui/snapcast && (setsid {args.board_client} "
                   f"tcp://{host}:17104 --soundcard '{card}' --player '{player}' "
                   f"--mixer software --logsink stdout --logfilter '*:warn' "
                   f"> /data/xiaorui/snapcast/sync-{tag}.log 2>&1 &); true")
            adb(serial, cmd)
            ok, ptr = wait_playing(serial, status)
            print(f"  {tag} " + (f"已在播(hw_ptr={ptr})" if ok else "!! 没出声"))
            return ok

        print("== 起两台播放端 ==")
        ok_a = start_board(args.serial, args.board_play_card, "A", args.player1, args.status1)
        ok_b = start_board(args.serial2, args.card2, "B", args.player2, args.status2)
        if not (ok_a and ok_b):
            print("!! 有板子没出声, 先排查再测", file=sys.stderr)
            return 1

        ctl = SnapcastClient(host="127.0.0.1", port=17180, timeout=5)
        # ★ 测前必须"恰好两个在线客户端", 且能按 MAC 前缀解析到 A/B:
        #   残留客户端/一台板子两个网卡都连上来时, 静音切段会控制错对象(实测踩过)
        online = [p for p in ctl.players() if p["connected"]]
        if len(online) != 2:
            print(f"!! 在线客户端应为 2 个, 实际 {len(online)}: "
                  f"{[(p['id'][:12], p['stream_id']) for p in online]}", file=sys.stderr)
            print("   先清干净: 两端 pkill -x snapclient, 再各起一个", file=sys.stderr)
            return 1
        id_a = id_b = None
        for pl in online:
            if pl["id"].lower().startswith(args.id_a.lower()):
                id_a = pl["id"]
            elif pl["id"].lower().startswith(args.id_b.lower()):
                id_b = pl["id"]
        if not (id_a and id_b):
            print(f"!! 按 MAC 前缀没解析到 A/B (A={id_a} B={id_b}); "
                  f"在线: {[p['id'] for p in online]}", file=sys.stderr)
            return 1
        print(f"== 客户端: A={id_a}  B={id_b} ==")

        if args.compensate_ms:
            print(f"== 给 B 补延迟 {args.compensate_ms:+.1f} ms ==")
            ctl.set_latency(id_b, int(round(args.compensate_ms)))
            time.sleep(2)

        if not args.skip_preflight:
            ok, msg = preflight(args.host_card)
            print(f"== 起测自检: {'通过' if ok else '不通过'} ({msg}) ==")
            if not ok:
                print("!! 自检不通过, 不开始正式测量", file=sys.stderr)
                return 1
        cal = args.cal_seconds
        total = 2 * cal + args.seconds + 2
        rec_path = os.path.join(args.out, "single.wav")
        print(f"== 开始录音 {total:.0f}s(时间线: A {cal:.0f}s / B {cal:.0f}s / 两路 {args.seconds:.0f}s) ==")
        rec = subprocess.Popen(
            f"arecord -D {args.host_card} -f S16_LE -r {SAMPLE_RATE} -c 1 "
            f"-d {int(total)} {rec_path}",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(rec)
        time.sleep(0.5)

        # 段 1: 只 A 出声
        ctl.set_volume(id_b, 0, muted=True)
        ctl.set_volume(id_a, 100, muted=False)
        time.sleep(cal)
        # 段 2: 只 B 出声
        ctl.set_volume(id_a, 0, muted=True)
        ctl.set_volume(id_b, 100, muted=False)
        time.sleep(cal)
        # 段 3: 两路
        ctl.set_volume(id_a, 100, muted=False)
        time.sleep(args.seconds + 0.5)
        try:
            rec.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rec.terminate()

        print()
        print("== 分析 ==")
        res = analyze_single(rec_path, cal)
        with open(os.path.join(args.out, "single-summary.json"), "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"产物: {args.out}/single.wav + single-summary.json")
        return 0 if res.get("ok") else 1
    finally:
        cleanup()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="板子1 serial(播放端 A)")
    ap.add_argument("--serial2", help="板子2 serial(播放端 B; 可选)")
    ap.add_argument("--card2", default="8", help="板子2 的播放设备(索引或名字)")
    ap.add_argument("--status1", default="/proc/asound/card0/pcm0p/sub0/status",
                    help="板子1 播放 PCM 的 status 路径")
    ap.add_argument("--status2", default="/proc/asound/card1/pcm0p/sub0/status",
                    help="板子2 播放 PCM 的 status 路径")
    ap.add_argument("--player1", default="alsa", help="板子1 的 player(可带参数, 如 alsa:buffer_time=40,periods=4)")
    ap.add_argument("--player2", default="alsa", help="板子2 的 player(可带参数)")
    ap.add_argument("--seconds", type=float, default=60.0, help="测量段录音时长")
    ap.add_argument("--out", default="/tmp/opencode/acoustic", help="产物目录")
    ap.add_argument("--host-ip", default="127.0.0.1", help="PC 的 Wi-Fi/局域网地址(板端要连它；默认用于本机隧道)")
    ap.add_argument("--host-ip2", default="", help="板2 连的地址(默认同 --host-ip; 走 USB 隧道时填 127.0.0.1)")
    ap.add_argument("--host-card", default="hw:3,0", help="PC 的录音设备(麦克风)")
    ap.add_argument("--host-play-card", default="17", help="PC 的播放设备索引(snapclient --list)")
    ap.add_argument("--board-play-card", default="xr_spk", help="板子1 的播放设备(xr_spk plug 或索引)")
    ap.add_argument("--snapclient", default="/tmp/opencode/snapcast/client-deb/usr/bin/snapclient",
                    help="宿主机上的 snapclient(PC 当播放端用)")
    ap.add_argument("--board-client", default="/data/xiaorui/snapcast/snapclient",
                    help="板上的 snapclient")
    ap.add_argument("--server-bin", default="/tmp/opencode/snapcast/server-deb/usr/bin/snapserver")
    ap.add_argument("--signal", default="/tmp/opencode/snapcast/media/sync-signal.pcm",
                    help="脉冲测试音源(裸 PCM 48k/2ch)")
    ap.add_argument("--method", choices=("separate", "single", "dual"), default="dual",
                    help="single=一次连续录音+静音分段(推荐, 能可靠认设备); separate=三段各自录音")
    ap.add_argument("--cal-seconds", type=float, default=8.0,
                    help="single 模式下每段标定时长")
    ap.add_argument("--id-a", default="aa:bb:cc",
                    help="板A 客户端 id 前缀(走 USB 隧道时客户端报 eth0 的 MAC)")
    ap.add_argument("--id-b", default="dd:ee:ff",
                    help="板B 客户端 id 前缀(eth0; 板B 的另一接口 MAC 前缀需按现场配置, 隧道下不出现)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="跳过起测前的 4 秒脉冲自检(不建议)")
    ap.add_argument("--cold-start", action="store_true",
                    help="先开录音再起客户端(测冷启收敛时间)")
    ap.add_argument("--pause-at", type=float, default=0.0,
                    help="录音开始后第 N 秒暂停音源(kill feeder)")
    ap.add_argument("--pause-for", type=float, default=10.0,
                    help="暂停多久(秒)")
    ap.add_argument("--compensate-ms", type=float, default=0.0,
                    help="测量段给 B 补的客户端延迟(ms, 正=让 B 提前); 0=不补")
    ap.add_argument("--analyze-only", action="store_true", help="只分析已有录音")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if args.method == "dual" and not args.analyze_only:
        return run_dual(args)
    if args.method == "single" and not args.analyze_only:
        return run_single(args)
    if args.analyze_only:
        for name in ("calib-host.wav", "calib-board.wav", "measure.wav"):
            p = os.path.join(args.out, name)
            if os.path.exists(p):
                print(f"== {name} ==")
                analyze(p)
        return 0

    if not args.serial:
        print("需要 --serial(播放端 A)", file=sys.stderr)
        return 2
    if not args.serial2:
        print("需要 --serial2(播放端 B)", file=sys.stderr)
        return 2
    for path in (args.snapclient, args.server_bin, args.signal):
        if not os.path.exists(path):
            print(f"缺文件: {path}", file=sys.stderr)
            return 2

    state = {"frontend": False}
    procs = []

    def cleanup():
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.5)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        if state["frontend"]:
            print("== 恢复板端前端 ==")
            adb(args.serial, "pkill -x xiaorui_live 2>/dev/null; sleep 2")
            adb(args.serial,
                "(setsid sh /data/xiaorui/start-frontend.sh >>/data/xiaorui/frontend.log 2>&1 </dev/null &); true")
            time.sleep(4)
            r = adb(args.serial, "pgrep -x xiaorui_live")
            print("  前端" + ("已恢复" if r.stdout.strip() else "!! 没起来"))

    try:
        print("== 记录现场 ==")
        r = adb(args.serial, "pgrep -x xiaorui_live")
        state["frontend"] = bool(r.stdout.strip())
        print(f"  前端: {'在跑(将停)' if state['frontend'] else '没跑'}")
        if state["frontend"]:
            adb(args.serial, "pkill -x xiaorui_live 2>/dev/null; sleep 2")
            print("  已停前端(板子 hw 播放独占)")

        # ---- server + 实时脉冲音源 ----
        fifo = "/tmp/opencode/snapcast/syncfifo"
        os.makedirs("/tmp/opencode/snapcast", exist_ok=True)
        if not os.path.exists(fifo):
            os.mkfifo(fifo)
        conf = os.path.join(args.out, "server.conf")
        with open(conf, "w") as f:
            f.write(f"""[server]
threads = 2
mdns_enabled = 0
[stream]
source = pipe://{fifo}?name=sync
codec = pcm
sampleformat = 48000:16:2
chunk_ms = 20
buffer = 1000
[tcp-control]
enabled = 1
port = 17105
[tcp-streaming]
enabled = 1
port = 17104
[http]
enabled = 1
port = 17180
""")
        print("== 起 snapserver(独立端口 17104/17105/17180) ==")
        procs.append(subprocess.Popen([args.server_bin, "-c", conf],
                                      stdout=open(os.path.join(args.out, "server.log"), "w"),
                                      stderr=subprocess.STDOUT))
        time.sleep(2)
        print("== 实时喂脉冲音源(ffmpeg -re) ==")
        procs.append(subprocess.Popen(
            f"ffmpeg -re -hide_banner -loglevel error -f s16le -ar 48000 -ac 2 "
            f"-i {args.signal} -f s16le pipe:1 > {fifo}",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(2)

        # ---- 播放端: 两台板子, 都走真实 Wi-Fi 连 PC 的 server ----
        def start_board(serial, card, tag, player=None, status=None):
            print(f"== 起播放端 {tag}({serial}) ==")
            player = player or (args.player1 if tag == "A" else args.player2)
            host = args.host_ip2 if (tag == "B" and args.host_ip2) else args.host_ip
            cmd = (f"cd /data/xiaorui/snapcast && (setsid {args.board_client} "
                   f"tcp://{host}:17104 --soundcard '{card}' "
                   f"--player '{player}' --mixer software --logsink stdout --logfilter '*:warn' "
                   f"> /data/xiaorui/snapcast/sync-{tag}.log 2>&1 &); true")
            adb(serial, cmd)
            if status:
                ok, ptr = wait_playing(serial, status)
                print(f"  {tag} " + (f"已在播(hw_ptr={ptr})" if ok else "!! 20s 内没出声"))
                if not ok:
                    r = adb(serial, f"tail -6 /data/xiaorui/snapcast/sync-{tag}.log")
                    print("  客户端日志尾部:", r.stdout.strip()[-300:])
            else:
                time.sleep(3)

        def stop_board(serial, tag):
            adb(serial, "pkill -x snapclient 2>/dev/null")
            time.sleep(1)

        start_board(args.serial, args.board_play_card, "A", args.player1, args.status1)
        start_board(args.serial2, args.card2, "B", args.player2, args.status2)

        # ---- 标定段: 分别只开一路, 认准谁是谁 ----
        print("== 标定段: 只开 A ==")
        stop_board(args.serial2, "B")
        record(args.host_card, 6, os.path.join(args.out, "calib-a.wav"))
        print("== 标定段: 只开 B ==")
        stop_board(args.serial, "A")
        start_board(args.serial2, args.card2, "B", args.player2, args.status2)
        record(args.host_card, 6, os.path.join(args.out, "calib-b.wav"))

        # ---- 测量段: 两路同播同录 ----
        print("== 测量段: 两路同时播 ==")
        start_board(args.serial, args.board_play_card, "A", args.player1, args.status1)
        if args.compensate_ms:
            # 用 Client.SetLatency 给 B 补延迟(正=让它提前), 然后重等它出声
            sys.path.insert(0, os.path.join(REPO, "tools", "snapcast_center"))
            from snapcast_control import SnapcastClient  # noqa: E402
            ctl = SnapcastClient(host="127.0.0.1", port=17180, timeout=5)
            target = None
            for pl in ctl.players():
                # B 走 USB 隧道, host 里是 127.0.0.1; 用它区别于 A(走 Wi-Fi 时也不一样)
                if pl["id"].lower().startswith("dd:ee:ff"):
                    target = pl["id"]
            if target:
                print(f"== 给 B({target}) 补延迟 {args.compensate_ms:+.1f} ms ==")
                ctl.set_latency(target, int(round(args.compensate_ms)))
                time.sleep(2)
            else:
                print("!! 没找到 B 的客户端, 跳过补偿")
        print(f"== 测量段: 两路同录 {args.seconds:.0f}s ==")
        record(args.host_card, args.seconds, os.path.join(args.out, "measure.wav"))
        stop_board(args.serial, "A")
        stop_board(args.serial2, "B")

        print()
        print("== 标定: 只开 A ==")
        cal_a = analyze(os.path.join(args.out, "calib-a.wav"), expect_pairs=False)
        print("== 标定: 只开 B ==")
        cal_b = analyze(os.path.join(args.out, "calib-b.wav"), expect_pairs=False)
        amps = None
        if cal_a.get("ok") and cal_b.get("ok"):
            amps = {"A": cal_a["amp_median"], "B": cal_b["amp_median"]}
            print(f"== 幅度指纹: A {amps['A']:.0f} / B {amps['B']:.0f} "
                  f"(相差 {20*np.log10(max(amps['B'],1)/max(amps['A'],1)):+.1f} dB) ==")
        print("== 测量: 两路同播 ==")
        meas = analyze(os.path.join(args.out, "measure.wav"), amps=amps)

        summary = {"calib_a": cal_a, "calib_b": cal_b, "measure": meas}
        with open(os.path.join(args.out, "summary.json"), "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print()
        print(f"产物: {args.out}/")
        print("  calib-a.wav / calib-b.wav / measure.wav / summary.json")
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

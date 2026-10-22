#!/usr/bin/env python3
"""生成 HA 灯控声学语料 (PC 上跑; tests/audio 不入库, 本脚本是复现入口).

cmd_ha_*.wav  = matcha 合成单指令(响度对齐 rms≈4558, 防削顶)
wake2_ha_*.wav = wake_tts_pure(放大 x2.63 对齐 wake2 语料) + 5.0s 静音 + 指令
                 (5.0s 间隙 = wake2_time.wav 同款: ack 死窗 1.25s + 余量)
前置: tts-matcha/matcha-icefall-zh-baker + 上游 venv(见 gen-test-audio.py).
"""
import re, subprocess, wave, array
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
A = REPO / "tests" / "audio"
MODEL = REPO / "tts-matcha" / "matcha-icefall-zh-baker"
VENV = __import__("os").path.expanduser("~/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python")
SNIPPET = re.search(r"GEN_SNIPPET = r'''(.*?)'''",
                    (REPO / "tools/gen-test-audio.py").read_text(), re.S).group(1)

CMDS = {"ha_on": "开灯", "ha_off": "帮我关灯", "ha_on2": "帮我把灯打开",
        "ha_plug_on": "打开插座", "ha_plug_off": "把插座关了"}
WAKE_RMS, GAP = 4558, 110250  # wake2_time.wav 唤醒段实测 rms / 5.0s@22050


def rd(p):
    s = array.array("h")
    with wave.open(str(p)) as w:
        s.frombytes(w.readframes(w.getnframes()))
        return s, w.getframerate()


def wr(p, sr, data):
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(data.tobytes() if hasattr(data, "tobytes") else data)


def amp(s, target_rms):
    cur = (sum(x * x for x in s) / len(s)) ** 0.5
    g = min(target_rms / cur, 32000 / max(abs(x) for x in s))
    return array.array("h", [int(x * g) for x in s]), g


def main():
    assert MODEL.is_dir(), "缺 PC matcha 模型: %s" % MODEL
    wake, sr = rd(A / "wake_tts_pure.wav")
    wake, gw = amp(wake, WAKE_RMS)
    for name, text in CMDS.items():
        raw = Path("/tmp/%s_raw.wav" % name)
        r = subprocess.run([VENV, "-c", SNIPPET, str(MODEL), text, str(raw)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-400:]
        cmd, sr2 = rd(raw)
        assert sr2 == sr, "采样率不一致: %d vs %d" % (sr2, sr)
        cmd, gc = amp(cmd, WAKE_RMS)
        wr(A / ("cmd_%s.wav" % name), sr, cmd.tobytes())
        wr(A / ("wake2_%s.wav" % name), sr, wake.tobytes() + b"\x00\x00" * GAP + cmd.tobytes())
        print(name, text, "wake_gain=%.2f cmd_gain=%.2f" % (gw, gc))


if __name__ == "__main__":
    main()

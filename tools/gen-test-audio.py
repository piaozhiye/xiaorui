#!/usr/bin/env python3
"""生成板端声学验收语料 (PC 上跑).

用 PC matcha 中文 TTS 合成 16k/mono 指令语料到 tests/audio/, 写 manifest.json
(文本/采样率/SHA256/来源/生成参数), 干净检出后可复跑再生成. 唤醒词音频是
真实录音(板上 /data/kws-corp), 由调用方 adb pull 后登记进 manifest(本脚本
发现同目录 wake_*.wav 会一并登记). matcha 模型与 sherpa_onnx 在 PC 私有环
境, 缺失时明确报错退出(不生成空目录假成功).

用法: tools/gen-test-audio.py [--venv /path/to/python]
"""
import argparse
import hashlib
import json
import subprocess
import os
import sys
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "tests" / "audio"
MODEL = Path(os.path.expanduser("~/rk/xiaozhi-edge-client/tts-matcha/matcha-icefall-zh-baker"))
DEFAULT_VENV = os.path.expanduser("~/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python")

# (文件名, 文本, 类别)  类别: cmd=普通指令, short=两字短指令, long=长回答提问
CORPUS = [
    ("cmd_time",       "现在几点了", "cmd"),
    ("cmd_weather",    "今天天气怎么样", "cmd"),
    ("cmd_alarm",      "帮我定一个十分钟的闹钟", "cmd"),
    ("cmd_joke",       "给我讲一个笑话", "cmd"),
    ("cmd_name",       "你叫什么名字", "cmd"),
    ("cmd_music",      "放一首轻音乐", "cmd"),
    ("cmd_thanks",     "谢谢你的帮助", "cmd"),
    ("cmd_mute",       "停止播放", "cmd"),
    ("cmd_repeat",     "再说一遍好吗", "cmd"),
    ("cmd_volume",     "声音大一点", "cmd"),
    ("short_stop",     "停下", "short"),
    ("short_more",     "再来", "short"),
    ("long_story",     "给我讲一个长一点的睡前故事", "long"),
    ("long_history",   "介绍一下人工智能的发展历史", "long"),
    ("long_cook",      "详细说说红烧肉怎么做", "long"),
    ("long_sci",       "给我推荐几本经典的科幻小说并说明理由", "long"),
    ("long_travel",    "计划一次三天的北京旅行给我一些建议", "long"),
]

GEN_SNIPPET = r'''
# 与 board-server/pkg/core/providers/tts/sherpa_local.py 同一套构造方式
import sys, wave, glob, os
from sherpa_onnx import (OfflineTts, OfflineTtsConfig,
                         OfflineTtsMatchaModelConfig, OfflineTtsModelConfig)
d = sys.argv[1]
m = OfflineTtsMatchaModelConfig(
    acoustic_model=sorted(glob.glob(os.path.join(d, "model*.onnx")))[0],
    vocoder=(sorted(glob.glob(os.path.join(d, "hifigan*.onnx"))) or
             sorted(glob.glob(os.path.join(d, "vocoder*.onnx"))))[0],
    lexicon=os.path.join(d, "lexicon.txt"),
    tokens=os.path.join(d, "tokens.txt"),
    dict_dir=os.path.join(d, "dict"))
tts = OfflineTts(OfflineTtsConfig(model=OfflineTtsModelConfig(matcha=m, num_threads=4)))
gen = tts.generate(sys.argv[2], sid=0, speed=1.0)
audio = gen.samples if hasattr(gen, "samples") else gen[0]
if not hasattr(audio, "astype"):
    import numpy as np
    audio = np.asarray(audio, dtype="float32") if max(abs(x) for x in audio[:100]) < 1 else np.asarray(audio)
    audio = audio * 32767 if abs(audio).max() < 1 else audio
sr = gen.sample_rate if hasattr(gen, "sample_rate") else tts.sample_rate
with wave.open(sys.argv[3], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(audio.astype("int16").tobytes())
print(sr)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venv", default=DEFAULT_VENV)
    args = ap.parse_args()
    if not MODEL.is_dir():
        print("缺少 PC matcha 模型: %s (私有环境, 不入库)" % MODEL)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"generator": "matcha-icefall-zh-baker sid=0 speed=1.0",
                "sample_rate": 16000, "items": []}
    for name, text, cat in CORPUS:
        wav = OUT / ("%s.wav" % name)
        # 崩溃残留的 0 帧空壳视为不存在(实测踩坑: wave.open 先建头, 崩在写帧)
        if wav.exists() and wave.open(str(wav)).getnframes() == 0:
            wav.unlink()
        if not wav.exists():
            r = subprocess.run([args.venv, "-c", GEN_SNIPPET, str(MODEL), text, str(wav)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print("合成失败 %s: %s" % (name, r.stderr[-300:]))
                return 2
        with wave.open(str(wav), "rb") as w:
            sr, n = w.getframerate(), w.getnframes()
        manifest["items"].append({
            "file": str(wav.relative_to(REPO)), "id": name, "text": text,
            "category": cat, "sample_rate": sr, "frames": n,
            "sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
            "source": "pc-matcha"})
    # 响度归一: 指令语料对齐真实唤醒录音的 RMS(0.8 倍, 留触发门余量),
    # 防止安静房间合成音频过响/吵闹房间过轻(2026-10-14 底噪 -59dB 实测踩坑)
    import array
    wakes = [w for w in OUT.glob("wake_*.wav")]
    if wakes:
        def _rms(p):
            w = wave.open(str(p)); d = array.array("h"); d.frombytes(w.readframes(w.getnframes())); w.close()
            return (sum(x * x for x in d) / max(1, len(d))) ** 0.5
        target = max(_rms(w) for w in wakes) * 0.95
        for item in manifest["items"]:
            if item["source"] != "pc-matcha":
                continue
            p = REPO / item["file"]
            w = wave.open(str(p)); d = array.array("h"); d.frombytes(w.readframes(w.getnframes())); sr = w.getframerate(); w.close()
            r = (sum(x * x for x in d) / max(1, len(d))) ** 0.5
            if r < 1:
                continue
            gain = min(target / r, 8.0)
            scaled = [max(-32767, min(32767, int(x * gain))) for x in d]
            with wave.open(str(p), "wb") as o:
                o.setnchannels(1); o.setsampwidth(2); o.setframerate(sr)
                o.writeframes(array.array("h", scaled).tobytes())
            import hashlib as h
            item["sha256"] = h.sha256(p.read_bytes()).hexdigest()
            item["gain"] = round(gain, 2)
    # 登记调用方 pull 下来的真实唤醒词录音
    for wav in sorted(OUT.glob("wake_*.wav")):
        with wave.open(str(wav), "rb") as w:
            sr, n = w.getframerate(), w.getnframes()
        manifest["items"].append({
            "file": str(wav.relative_to(REPO)), "id": wav.stem, "text": "小瑞小瑞(真实录音)",
            "category": "wake", "sample_rate": sr, "frames": n,
            "sha256": hashlib.sha256(wav.read_bytes()).hexdigest(),
            "source": "board-kws-corp"})
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    print("语料就绪: %d 项, manifest=%s" % (len(manifest["items"]), OUT / "manifest.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

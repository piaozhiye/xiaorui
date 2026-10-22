#!/usr/bin/env python3
"""语音音量控制的资产生成 (PC 上跑).

1. xr_volack24k.wav "好的" 确认音(24k/mono) → board-server/cues/(入库资产),
   matcha 22050 → audioop.ratecv 24k, 响度对齐现役 ack("我在")。
2. tests/audio/ 音量指令语料(16k): vol_up/down/set/max/min + 负例 vol_neg
   (必须放行给 LLM 的说法), 响度对齐既有语料标准(max wake RMS × 0.95,
   与 gen-test-audio.py 同款)。
3. wake2_vol* 组合语料: 放大唤醒段(RMS 6853, 同 gen-wake2-corpus.py)
   + 5s 静音 + 指令, 供板端声学验收直接播放。

manifest.json 一并登记(按文件名幂等覆盖)。前置: PC matcha 模型与 venv
(同 gen-test-audio.py)。用法: tools/gen-volume-assets.py [--venv ...]
"""
import argparse
import array
import audioop
import hashlib
import json
import subprocess
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
A = REPO / "tests" / "audio"
CUES = REPO / "board-server" / "cues"
MODEL = Path(os.path.expanduser("~/rk/xiaozhi-edge-client/tts-matcha/matcha-icefall-zh-baker"))
DEFAULT_VENV = os.path.expanduser("~/rk/xiaozhi-esp32-server/main/xiaozhi-server/.venv/bin/python")

# 与 board-server/pkg/core/providers/tts/sherpa_local.py 同一套构造(gen-test-audio.py 同款)
GEN_SNIPPET = r'''
import os
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

COMMANDS = [
    ("vol_up",   "声音大一点", "cmd"),
    ("vol_down", "声音小一点", "cmd"),
    ("vol_set",  "音量调到百分之六十", "cmd"),
    ("vol_max",  "音量调到最大", "cmd"),
    ("vol_min",  "静音", "cmd"),
    ("vol_neg",  "大声唱一首歌", "cmd"),   # 负例: 剥掉音量词剩实义词, 必须放行
]
COMPOSITES = {
    "wake2_volup.wav": "vol_up.wav",
    "wake2_voldown.wav": "vol_down.wav",
    "wake2_volset.wav": "vol_set.wav",
    "wake2_volmax.wav": "vol_max.wav",
    "wake2_volmin.wav": "vol_min.wav",
    "wake2_volneg.wav": "vol_neg.wav",
}
GAP_S = 5.0
WAKE_TARGET_RMS = 6853   # 同 gen-wake2-corpus.py: 单口气语料唤醒段响度


def synth(venv, text, out):
    r = subprocess.run([venv, "-c", GEN_SNIPPET, str(MODEL), text, str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("合成失败 %s: %s" % (out, r.stderr[-300:]))
    return int(r.stdout.strip())


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        data = w.readframes(w.getnframes())
    assert ch == 1 and sw == 2, "%s 非 mono/PCM16" % path
    return sr, array.array("h", data)


def write_wav(path, sr, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())


def scale_to_rms(samples, target):
    rms = audioop.rms(samples.tobytes(), 2)
    if rms < 1:
        return samples
    gain = min(target / rms, 32000 / max(1, audioop.max(samples.tobytes(), 2)))
    return array.array("h", audioop.mul(samples.tobytes(), 2, gain))


def manifest_upsert(path, item):
    mpath = A / "manifest.json"
    manifest = json.loads(mpath.read_text())
    name = Path(item["file"]).name
    manifest["items"] = [old for old in manifest["items"]
                         if Path(old["file"]).name != name] + [item]
    manifest["items"].sort(key=lambda it: it["file"])
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venv", default=DEFAULT_VENV)
    args = ap.parse_args()
    if not MODEL.is_dir():
        raise SystemExit("缺少 PC matcha 模型: %s" % MODEL)
    A.mkdir(parents=True, exist_ok=True)
    CUES.mkdir(parents=True, exist_ok=True)

    # 1. 确认音 "好的" @24k, 响度对齐现役 ack
    ack_sr, ack = read_wav(CUES / "xr_ack24k.wav")
    assert ack_sr == 24000
    tmp = A / "_volack_raw.wav"
    sr = synth(args.venv, "好的", tmp)
    _, cue = read_wav(tmp)
    cue24, _ = audioop.ratecv(cue.tobytes(), 2, 1, sr, 24000, None)
    cue24 = scale_to_rms(array.array("h", cue24), audioop.rms(ack.tobytes(), 2))
    write_wav(CUES / "xr_volack24k.wav", 24000, cue24)
    tmp.unlink()
    print("cue xr_volack24k.wav: %d samples @24k" % len(cue24))

    # 2. 指令语料(matcha 原生 22050, 与既有语料族同率), 响度=既有语料标准
    target = WAKE_TARGET_RMS * 0.95
    for name, text, cat in COMMANDS:
        wav = A / ("%s.wav" % name)
        sr = synth(args.venv, text, wav)
        _, d = read_wav(wav)
        d = scale_to_rms(d, target)
        write_wav(wav, sr, d)
        manifest_upsert(wav, dict(file="tests/audio/%s.wav" % name, id=name,
                                  text=text, category=cat, sample_rate=sr,
                                  frames=len(d),
                                  sha256=hashlib.sha256(wav.read_bytes()).hexdigest(),
                                  source="pc-matcha-volume"))
        print("%s ok (%d帧@%d)" % (name, len(d), sr))

    # 3. 组合语料: 放大唤醒段 + 5s 静音 + 指令(与唤醒段同率, 同 gen-wake2 口径)
    wsr, wake = read_wav(A / "wake_tts_pure.wav")
    wake = scale_to_rms(wake, WAKE_TARGET_RMS)
    gap = array.array("h", bytes(2 * int(wsr * GAP_S)))
    for out, cmdf in COMPOSITES.items():
        csr, cmd = read_wav(A / cmdf)
        assert csr == wsr, "%s 率 %d != 唤醒段 %d" % (cmdf, csr, wsr)
        comp = wake + gap + cmd
        path = A / out
        write_wav(path, wsr, comp)
        src = json.loads((A / "manifest.json").read_text())
        text = next(i["text"] for i in src["items"]
                    if Path(i["file"]).name == cmdf)
        manifest_upsert(path, dict(file="tests/audio/" + out, id=Path(out).stem,
                                   text=text, category="cmd", sample_rate=wsr,
                                   frames=len(comp),
                                   sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                   source="composite-wake-command", gap_s=GAP_S))
        print("%s ok (gap %.0fs)" % (out, GAP_S))


if __name__ == "__main__":
    main()

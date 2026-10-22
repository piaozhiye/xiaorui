#!/usr/bin/env python3
"""生成 wake2_* 间隙语料: 唤醒词(wake_tts_pure, 放大对齐单口气语料响度)
+ 5.0s 静音 + 指令(cmd_*/short_*/long_*)。

为什么5s: 当前ack有40534个24k样本(1.69s)，另有1.5s冷却、约0.5s端点等待和
0.57s精修；原3s间隙仍不足，报时指令被冷却吞掉。5s用于覆盖完整的当前链路。
这不是推荐用户等5秒的体验目标，而是给现有语音确认音做诚实验收。
为什么放大: wake_tts_pure RMS=1732, 单口气语料 RMS=6853, 差 11.9dB, 不放大
KWS 命中率 0/5(R2 实测)。峰值受限帽, 实际增益 ~8.4dB。

前置: tests/audio/ 下已有 wake_tts_pure.wav 与 cmd_time/cmd_weather/
short_stop/long_story/cmd_name.wav(gen-test-audio.py 产物)。
"""
import wave, audioop, sys, json, hashlib
from pathlib import Path

A = Path(__file__).resolve().parent.parent / "tests" / "audio"
manifest_path = A / "manifest.json"
manifest = json.loads(manifest_path.read_text())
source_items = {Path(item["file"]).name: item for item in manifest["items"]}
TARGET_RMS = 6853          # 对齐 wake_tts_* 单口气语料的唤醒段响度
GAP_S = 5.0
PAIRS = {
    "wake2_time.wav": "cmd_time.wav",
    "wake2_weather.wav": "cmd_weather.wav",
    "wake2_stop.wav": "short_stop.wav",
    "wake2_story.wav": "long_story.wav",
    "wake2_name.wav": "cmd_name.wav",
}

w = wave.open(str(A / "wake_tts_pure.wav"))
sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
if (ch, sw) != (1, 2):
    raise ValueError("wake fixture must be mono PCM16")
wake = w.readframes(w.getnframes())
w.close()
gain = min(TARGET_RMS / audioop.rms(wake, 2), 32000 / max(1, audioop.max(wake, 2)))
scaled = audioop.mul(wake, 2, gain)
gap = b"\x00" * int(sr * GAP_S) * ch * sw
print(f"wake 增益={gain:.2f} 峰值={audioop.max(scaled, 2)}")
for out, cmdf in PAIRS.items():
    c = wave.open(str(A / cmdf))
    if (c.getframerate(), c.getnchannels(), c.getsampwidth()) != (sr, ch, sw):
        raise ValueError(f"fixture format mismatch: {cmdf}")
    cd = c.readframes(c.getnframes())
    c.close()
    o = wave.open(str(A / out), "w")
    o.setnchannels(ch)
    o.setsampwidth(sw)
    o.setframerate(sr)
    o.writeframes(scaled + gap + cd)
    o.close()
    item = dict(file="tests/audio/" + out, id=Path(out).stem,
                text=source_items[cmdf]["text"], category="cmd", sample_rate=sr,
                frames=(len(scaled) + len(gap) + len(cd)) // (ch * sw),
                sha256=hashlib.sha256((A / out).read_bytes()).hexdigest(),
                source="composite-wake-command", gap_s=GAP_S)
    manifest["items"] = [old for old in manifest["items"]
                         if Path(old["file"]).name != out] + [item]
    print(f"{out} ok (gap {GAP_S}s)")
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")

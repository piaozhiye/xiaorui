#!/usr/bin/env python3
"""用板上 RockTTS 重新生成前端提示音 wav(24k/mono/S16_LE)。

在**板子上**跑(需要 RockTTS 的 .so、模型、license):
    /data/gwvenv/bin/python tools/gen-rock-cues.py --out /tmp/rock-cues

生成后把 wav 拷进 board-server/cues/(仓库)与
/data/xiaorui-server/board-scripts/(板端生产目录), 重启前端生效。

★ 不要和网关的 tts_helper 并发: 两个 RockTTS 实例会抢 NPU(若真并发,
  先 sh /etc/init.d/S98xiaorui stop, 生成完再 start)。脚本内也用
  TtsEngine 自带的 npu_exclusive() flock 做兜底。

旧版是 2026-10-15 用开源 TTS 预录的("我在"/"没听清"/"好的"), 听感偏
细、音色旧。这里改用现役 RockTTS 合成, 并把峰值归一到 ~26000 让声音
饱满(避免"细微")。
"""
import argparse
import os
import struct
import sys

LIB_DIR = "/data/xiaorui-server/rocktts/lib"
DATA_DIR = "/data/xiaorui-server/rocktts/data"
AUTH = "/data/xiaorui-server/rocktts/rockx_auth_config.json"

# 与旧 cue 一一对应; hmm 代码里没用, 但一并重生成保持资产风格统一
CUES = {
    "xr_ack24k.wav": "我在",
    "xr_fail24k.wav": "没听清",
    "xr_volack24k.wav": "好的",
    "xr_hmm24k.wav": "嗯",
}

ROCK_DIR = "/data/xiaorui-gateway/server/core/providers/rock"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="输出目录")
    args = parser.parse_args()

    # tts_helper 依赖 rockx(需要 LD_LIBRARY_PATH 指向 RockTTS 的 .so)
    os.environ["LD_LIBRARY_PATH"] = LIB_DIR + os.pathsep + \
        os.environ.get("LD_LIBRARY_PATH", "")
    sys.path.insert(0, ROCK_DIR)
    from tts_helper import TtsEngine  # noqa: E402

    os.makedirs(args.out, exist_ok=True)
    engine = TtsEngine(LIB_DIR, DATA_DIR, AUTH)
    for name, text in CUES.items():
        pcm, elapsed = engine.synthesize(text, 1.0, 30.0)
        samples = list(struct.unpack("<%dh" % (len(pcm) // 2), pcm))
        peak = max((abs(s) for s in samples), default=0)
        scale = min(2.5, 26000.0 / peak) if peak else 1.0
        if scale != 1.0:
            samples = [max(-32768, min(32767, int(s * scale))) for s in samples]
        path = os.path.join(args.out, name)
        with wave_open(path) as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(struct.pack("<%dh" % len(samples), *samples))
        print(f"{name}: '{text}' {len(samples) / 24000:.3f}s "
              f"peak_in={peak} gain={scale:.2f} ({elapsed:.2f}s 合成)")


def wave_open(path):
    import wave
    return wave.open(path, "wb")


if __name__ == "__main__":
    main()

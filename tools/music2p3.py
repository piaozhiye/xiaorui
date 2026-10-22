#!/usr/bin/env python3
"""任意音源 -> .p3 -> 推板 (2026-10-15).

板上 play_music 插件播 .p3 走纯 python 解码(零 ffmpeg 依赖; 板上 ffbin 的
ffmpeg 缺 libgnutls 起不来, mp3 路线作废)。本工具在 PC 上完成:
  1. 取音频: 本地文件或 --url 在线下载(mp3/任何 ffmpeg 能解的格式)
  2. ffmpeg -> 16k/mono/s16le PCM
  3. libopus(ctypes) 按 60ms 帧(960 样本)编码
  4. 封装 .p3: [type:1][保留:1][len:2 BE] + opus 包 (core/utils/p3.py 同格式)
  5. --push: adb 推到板 /data/xiaorui-gateway/server/music/

用法:
  tools/music2p3.py INPUT --name 演示曲一 [--trim 60] [--push]
  tools/music2p3.py --url https://.../song.mp3 --name 我的歌 [--push]
"""
import os
import argparse
import ctypes
import struct
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BOARD_MUSIC = "/data/xiaorui-gateway/server/music"
OPUS_APPLICATION_AUDIO = 2049
FRAME_MS = 60


def load_opus():
    lib = ctypes.CDLL("libopus.so.0")
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_int)]
    lib.opus_encode.restype = ctypes.c_int
    lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_char_p, ctypes.c_int]
    lib.opus_encoder_ctl.restype = ctypes.c_int
    lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_encoder_destroy.restype = None
    return lib


def to_pcm16k(src: Path, trim_s: int) -> bytes:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if trim_s > 0:
        cmd += ["-t", str(trim_s)]
    cmd += ["-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def pcm_to_p3(pcm: bytes, out: Path) -> int:
    lib = load_opus()
    err = ctypes.c_int(0)
    enc = lib.opus_encoder_create(16000, 1, OPUS_APPLICATION_AUDIO, ctypes.byref(err))
    if not enc:
        raise SystemExit("opus_encoder_create 失败 err=%d" % err.value)
    lib.opus_encoder_ctl(enc, 4002, 24000)   # OPUS_SET_BITRATE 24k
    frame = 960                               # 60ms @16k
    packets = 0
    with open(out, "wb") as f:
        for off in range(0, len(pcm) - frame * 2 + 1, frame * 2):
            chunk = pcm[off:off + frame * 2]
            buf = ctypes.create_string_buffer(4000)
            n = lib.opus_encode(enc, chunk, frame, buf, len(buf))
            if n <= 0:
                raise SystemExit("opus_encode 失败 rc=%d" % n)
            f.write(struct.pack(">BBH", 4, 0, n))
            f.write(buf.raw[:n])
            packets += 1
    lib.opus_encoder_destroy(enc)
    return packets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", help="本地音频文件(可省, 用 --url)")
    ap.add_argument("--url", help="在线直链(mp3 等), PC 下载后转换")
    ap.add_argument("--name", required=True, help="歌名(板上按名匹配, 建议纯中文)")
    ap.add_argument("--trim", type=int, default=0, help="只取前 N 秒(演示曲用)")
    ap.add_argument("--push", action="store_true", help="adb 推到板 music 目录")
    ap.add_argument("--serial", default=os.environ.get("XZ_BOARD_SERIAL"))
    args = ap.parse_args()
    if not args.serial:
        ap.error("缺少板卡序列号: 传 --serial 或 export XZ_BOARD_SERIAL")

    tmpdir = Path(tempfile.mkdtemp())
    src = None
    if args.url:
        src = tmpdir / "online.bin"
        print(f"下载 {args.url}")
        req = urllib.request.Request(args.url, headers={"User-Agent": "xr-music/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(src, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    elif args.input:
        src = Path(args.input)
    else:
        ap.error("需要 input 或 --url")
    if not src or not src.exists():
        raise SystemExit("输入不存在")

    out = Path(f"{args.name}.p3")
    pcm = to_pcm16k(src, args.trim)
    n = pcm_to_p3(pcm, out)
    dur = n * FRAME_MS / 1000
    print(f"{out} 就绪: {n} 包, {dur:.1f}s, {out.stat().st_size} 字节")
    if args.push:
        subprocess.run(["adb", "-s", args.serial, "push", str(out),
                        f"{BOARD_MUSIC}/{out.name}"], check=True)
        print(f"已推板: {BOARD_MUSIC}/{out.name}")


if __name__ == "__main__":
    main()

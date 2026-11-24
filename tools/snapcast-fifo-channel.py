#!/usr/bin/env python3
"""Convert 48 kHz stereo Snapcast PCM into the 24 kHz mono UI FIFO.

This is a measurement bridge for testing the existing FIFO playback path. It
does not add buffering: the frontend's own preroll and ALSA queue remain under
test. Input is raw S16_LE stereo; output is raw S16_LE mono, selecting every
other frame. Modes 0/1 isolate a channel for sync probes; ``mix`` averages L/R
for music playback so every speaker receives the complete song.
"""
import argparse
import array
import os
import sys
import time


def convert(input_path: str, output_path: str, channel: int | str,
            stats_fd: int | None = None) -> None:
    pending = b""
    sample_phase = 0
    in_bytes = out_bytes = 0
    report_at = time.monotonic() + 1.0
    with open(input_path, "rb", buffering=0) as source:
        with open(output_path, "wb", buffering=0) as sink:
            while True:
                chunk = source.read(8192)
                if not chunk:
                    return
                in_bytes += len(chunk)
                chunk = pending + chunk
                frame_bytes = 4
                complete = len(chunk) - len(chunk) % frame_bytes
                pending = chunk[complete:]
                if not complete:
                    continue
                samples = array.array("h")
                samples.frombytes(chunk[:complete])
                if sys.byteorder != "little":
                    samples.byteswap()
                # One 48 kHz frame out of every two makes 24 kHz mono. FIFO
                # reads may end on any frame boundary, so carry the decimation
                # phase across reads instead of restarting at each chunk.
                frame_count = complete // frame_bytes
                if channel == "mix":
                    mono = array.array("h")
                    for frame in range(sample_phase, frame_count, 2):
                        total = samples[frame * 2] + samples[frame * 2 + 1]
                        # Round toward zero to avoid a negative DC bias.
                        mono.append(total // 2 if total >= 0 else -((-total) // 2))
                else:
                    channel_index = int(channel)
                    channel_samples = samples[channel_index::2]
                    mono = channel_samples[sample_phase::2]
                sample_phase = (sample_phase + frame_count) & 1
                if sys.byteorder != "little":
                    mono.byteswap()
                output = mono.tobytes()
                sink.write(output)
                out_bytes += len(output)
                now = time.monotonic()
                if now >= report_at:
                    print(f"bridge {now:.3f} input={in_bytes} output={out_bytes}",
                          file=sys.stderr, flush=True)
                    if stats_fd is not None:
                        # A tiny atomic heartbeat for external health checks.
                        # Keep it separate from the audio stream and tolerate
                        # the observer disappearing during cleanup.
                        try:
                            os.write(stats_fd, f"{now:.3f},{in_bytes},{out_bytes}\n".encode())
                        except OSError:
                            pass
                    report_at = now + 1.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_fifo")
    parser.add_argument("output_fifo")
    parser.add_argument("channel", choices=("0", "1", "mix"))
    parser.add_argument("--stats-file", help="optional heartbeat CSV path")
    args = parser.parse_args()
    stats_fd = None
    if args.stats_file:
        stats_fd = os.open(args.stats_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    # 常驻: snapclient 断开时 music.raw 没有写端 -> read 返回 0(EOF);
    # 重开两个 FIFO 等写端回来, 否则客户端断一次 bridge 就没了(实测踩到).
    while True:
        try:
                channel = args.channel if args.channel == "mix" else int(args.channel)
                convert(args.input_fifo, args.output_fifo, channel, stats_fd)
        except (BrokenPipeError, OSError) as exc:
            print(f"fifo bridge: {exc} (重开)", file=sys.stderr, flush=True)
        else:
            print("fifo bridge: EOF (重开, 等写端回来)", file=sys.stderr, flush=True)
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

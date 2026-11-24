#!/usr/bin/env python3
"""Real-time feeder for XR_MIC=fifo (16 kHz, mono, signed 16-bit PCM)."""
import hashlib
import sys
import time


def main():
    if len(sys.argv) not in (4, 5, 6):
        raise SystemExit("usage: mic-feed.py <raw-s16le> <fifo> <loops> [startup-silence-ms] [tail-silence-ms]")
    path, fifo, loops_s = sys.argv[1:4]
    try:
        loops = int(loops_s)
    except ValueError:
        raise SystemExit("loops must be a positive integer")
    if loops <= 0:
        raise SystemExit("loops must be a positive integer")
    silence_ms = int(sys.argv[4]) if len(sys.argv) >= 5 else 0
    if silence_ms < 0 or silence_ms % 100:
        raise SystemExit("startup-silence-ms must be a non-negative multiple of 100")
    tail_ms = int(sys.argv[5]) if len(sys.argv) == 6 else 0
    if tail_ms < 0 or tail_ms % 100:
        raise SystemExit("tail-silence-ms must be a non-negative multiple of 100")

    with open(path, "rb") as source:
        data = source.read()
    if not data:
        raise SystemExit("input PCM is empty")
    if len(data) % 2:
        raise SystemExit("input PCM byte count must be even (S16LE frames)")

    chunk = 3200  # 100 ms @ 16 kHz, mono, S16LE
    silence_bytes = 32 * silence_ms  # 16 kHz * 2 bytes/sample * milliseconds / 1000
    tail_align_bytes = (-(len(data) * loops)) % chunk
    tail_bytes = tail_align_bytes + 32 * tail_ms
    expected = len(data) * loops + silence_bytes + tail_bytes
    written = 0
    started = time.monotonic()
    print(
        f"feed_start loops={loops} bytes_per_loop={len(data)} "
        f"total_bytes={expected} sha256={hashlib.sha256(data).hexdigest()} "
        f"preroll_bytes={silence_bytes} tail_bytes={tail_bytes}",
        flush=True,
    )
    try:
        with open(fifo, "wb") as output:
            for offset in range(0, silence_bytes, chunk):
                block = bytes(min(chunk, silence_bytes - offset))
                output.write(block)
                output.flush()
                written += len(block)
                time.sleep(len(block) / 32000.0)
            for loop_index in range(loops):
                loop_written = 0
                for offset in range(0, len(data), chunk):
                    block = data[offset : offset + chunk]
                    output.write(block)
                    output.flush()
                    loop_written += len(block)
                    written += len(block)
                    # Pace by actual frames; don't sleep 100 ms for a short tail.
                    time.sleep(len(block) / 32000.0)
                print(
                    f"feed_loop_done index={loop_index + 1}/{loops} bytes={loop_written}",
                    flush=True,
                )
            for offset in range(0, tail_bytes, chunk):
                block = bytes(min(chunk, tail_bytes - offset))
                output.write(block)
                output.flush()
                written += len(block)
                time.sleep(len(block) / 32000.0)
    except (BrokenPipeError, OSError) as exc:
        print(f"feed_error written={written}/{expected}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)

    elapsed = time.monotonic() - started
    print(
        f"feed_done loops={loops} bytes={written}/{expected} "
        f"frames={written // 2} preroll_bytes={silence_bytes} "
        f"tail_bytes={tail_bytes} elapsed_s={elapsed:.3f}",
        flush=True,
    )


if __name__ == "__main__":
    main()

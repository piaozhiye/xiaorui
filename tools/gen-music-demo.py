#!/usr/bin/env python3
"""Generate a small, original instrumental test fixture (not a downloaded song)."""
import argparse
import array
import math
import wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", help="output WAV path")
    args = parser.parse_args()
    rate = 16000
    notes = [60, 64, 67, 72, 69, 65, 62, 67, 64, 69, 71, 67, 65, 62, 64, 60]
    samples = array.array("h")
    for note in notes:
        frequency = 440 * 2 ** ((note - 69) / 12)
        for index in range(rate // 2):
            t = index / rate
            envelope = min(1, t / .02) * min(1, (.5 - t) / .08)
            value = math.sin(2 * math.pi * frequency * t)
            value += .25 * math.sin(4 * math.pi * frequency * t)
            samples.append(int(10000 * envelope * value))
    import sys
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(args.output, "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(samples.tobytes())


if __name__ == "__main__":
    main()

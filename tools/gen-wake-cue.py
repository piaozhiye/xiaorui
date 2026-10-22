#!/usr/bin/env python3
"""Generate a short 24k/mono/PCM16 wake cue; hardware output remains 48k/stereo."""
import array
import math
from pathlib import Path
import sys
import wave

def write_cue(path):
    rate, count, ramp = 24000, 2880, 192
    pcm = array.array("h")
    for i in range(count):
        envelope = min(1.0, i / ramp, (count - 1 - i) / ramp)
        pcm.append(round(6000 * envelope * math.sin(2 * math.pi * 880 * i / rate)))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(pcm.tobytes())

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: gen-wake-cue.py OUTPUT.wav")
    write_cue(Path(sys.argv[1]))

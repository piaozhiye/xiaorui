#!/usr/bin/env python3
"""Rebuild timer acoustic fixtures and merge canonical manifest entries.

Requires the private PC Matcha model, not board state or untracked WAV inputs.
python3 tools/gen-timer-corpus.py [--force]
python3 tools/gen-timer-corpus.py --verify-only
"""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import runpy
import subprocess
import sys
import wave

REPO = Path(__file__).resolve().parent.parent
COMMANDS = {
    "set1": "明早八点叫我",
    "set2": "明晚八点叫我",
    "cancel1": "取消明早八点闹钟",
    "cancel2": "把明晚八点的闹钟删掉",
    "list": "有什么提醒",
    "all": "取消提醒",
}


def read_pcm(path):
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"not mono PCM16: {path}")
        pcm = array.array("h", source.readframes(source.getnframes()))
        if sys.byteorder != "little":
            pcm.byteswap()
        if not pcm or not any(pcm):
            raise ValueError(f"empty/silent fixture: {path}")
        return source.getframerate(), pcm


def write_pcm(path, rate, pcm):
    pcm = array.array("h", pcm)
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(pcm.tobytes())


def entry(path, text, composite=False):
    rate, pcm = read_pcm(path)
    result = dict(file="tests/audio/" + path.name, id=path.stem, text=text,
                  category="cmd", sample_rate=rate, frames=len(pcm),
                  sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  source="composite-wake-command" if composite else "pc-matcha")
    if composite:
        result.update(gap_s=5.0, wake_text="小瑞小瑞", generator="tools/gen-timer-corpus.py")
    return result


def verify(root):
    manifest = json.loads((root / "tests/audio/manifest.json").read_text())
    entries = {item["file"]: item for item in manifest["items"]}
    for key in COMMANDS:
        for name in (f"xr_ck_{key}.wav", f"wake2_ck_{key}.wav"):
            relative = "tests/audio/" + name
            item = entries[relative]
            path = root / relative
            rate, pcm = read_pcm(path)
            if (item["id"] != path.stem or item["sample_rate"] != rate or
                    item["frames"] != len(pcm) or
                    item["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()):
                raise ValueError(f"manifest mismatch: {relative}")
    print("timer corpus: 12/12 valid")


def main():
    defaults = runpy.run_path(str(REPO / "tools/gen-test-audio.py"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO)
    parser.add_argument("--model-dir", type=Path, default=defaults["MODEL"])
    parser.add_argument("--python", default=defaults["DEFAULT_VENV"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.verify_only:
        verify(root)
        return
    if not args.model_dir.is_dir():
        parser.error("private Matcha model missing; use --model-dir")
    out = root / "tests/audio"
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"items": []}

    def synth(name, text):
        path = out / name
        if args.force or not path.exists():
            subprocess.run([args.python, "-c", defaults["GEN_SNIPPET"],
                            str(args.model_dir), text, str(path)], check=True,
                           stdout=subprocess.DEVNULL)
        read_pcm(path)
        return path

    wake_path = synth("wake_tts_pure.wav", "小瑞小瑞")
    rate, wake = read_pcm(wake_path)
    rms = math.sqrt(sum(x * x for x in wake) / len(wake))
    gain = min(6853 / rms, 32000 / max(abs(x) for x in wake))
    prefix = array.array("h", (int(x * gain) for x in wake))
    prefix.extend([0] * (rate * 5))
    generated = []
    for key, text in COMMANDS.items():
        command_path = synth(f"xr_ck_{key}.wav", text)
        command_rate, command = read_pcm(command_path)
        if command_rate != rate:
            raise ValueError("wake and command sample rates differ")
        composite = out / f"wake2_ck_{key}.wav"
        write_pcm(composite, rate, prefix + command)
        generated.extend([entry(command_path, text), entry(composite, text, True)])
    # Replace only this family, including old bare filenames. Other evidence stays intact.
    names = {Path(item["file"]).name for item in generated}
    manifest["items"] = [item for item in manifest["items"]
                         if Path(item["file"]).name not in names] + generated
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    verify(root)


if __name__ == "__main__":
    main()

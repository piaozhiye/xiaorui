#!/usr/bin/env python3
"""Set PulseAudio's idle suspend module to immediate idle release.

This patches a local copy of /etc/pulse/default.pa. It never contacts or
modifies a board; use --write only on the intended config file.
"""
import argparse
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


MODULE = "load-module module-suspend-on-idle timeout=0"
ACTIVE_MODULE = re.compile(r"^\s*load-module\s+module-suspend-on-idle(?:\s+.*)?(?:\r?\n)?$")


def patched_text(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        if ACTIVE_MODULE.match(line):
            if not inserted:
                out.append(MODULE + ("\r\n" if line.endswith("\r\n") else "\n"))
                inserted = True
            continue
        out.append(line)
    if not inserted:
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += "\n"
        out.append(MODULE + "\n")
    return "".join(out)


def atomic_write(path: Path, contents: str) -> None:
    original = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, stat.S_IMODE(original.st_mode))
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="PulseAudio default.pa file or local copy")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="fail unless already configured")
    action.add_argument("--write", action="store_true", help="atomically update this file")
    args = parser.parse_args()

    original = args.config.read_text(encoding="utf-8")
    updated = patched_text(original)
    if args.check:
        if original != updated:
            print("PulseAudio idle-suspend config needs updating")
            return 1
        print("PulseAudio idle-suspend config is already set to timeout=0")
        return 0
    if args.write:
        if original != updated:
            backup = args.config.with_name(args.config.name + ".pre-xiaorui")
            if not backup.exists():
                shutil.copy2(args.config, backup)
            atomic_write(args.config, updated)
        print(f"updated {args.config}: {MODULE} (backup: {args.config.name}.pre-xiaorui)")
        return 0
    print(updated, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

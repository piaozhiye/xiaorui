#!/usr/bin/env python3
"""Require runtime, model, NPU-load, and response evidence for RKLLM smoke."""

import argparse
from pathlib import Path
import re
import sys


def validate(runtime_log: str, npu_load_log: str, response: str) -> list[str]:
    errors = []
    if not re.search(r"(?:target_platform|platform):\s*RK3562\b", runtime_log):
        errors.append("RKLLM runtime log does not identify RK3562")
    if not re.search(r"model_dtype:\s*W8A8\b", runtime_log):
        errors.append("RKLLM runtime log does not identify the expected W8A8 model")
    loads = [
        int(value)
        for value in re.findall(r"NPU load:\s*(\d+)\s*%", npu_load_log)
    ]
    if not any(value > 0 for value in loads):
        errors.append("no nonzero RKNPU load observed during inference")
    if not response.strip():
        errors.append("RKLLM inference returned no text response")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-log", required=True, type=Path)
    parser.add_argument("--npu-load-log", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args()
    try:
        runtime = args.runtime_log.read_text(encoding="utf-8", errors="replace")
        loads = args.npu_load_log.read_text(encoding="utf-8", errors="replace")
        response = args.response.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"evidence file unavailable: {exc}", file=sys.stderr)
        return 2
    errors = validate(runtime, loads, response)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    peak = max(
        int(value)
        for value in re.findall(r"NPU load:\s*(\d+)\s*%", loads)
    )
    print(f"PASS: RK3562 W8A8; nonzero NPU load observed (peak {peak}%); response present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

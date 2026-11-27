#!/usr/bin/env python3
"""Prepare the user-supplied Rockchip 2mic+2ref VQE config for this board.

Usage: patch-vqe-config.py <source-json> <destination-json>
"""
import json
import sys
from pathlib import Path


def patch(source: str, destination: str) -> dict:
    with open(source, encoding="utf-8") as stream:
        data = json.load(stream)
    skv = data["skv_configs"]

    # Product processing requested for this board: both reference-based AEC
    # and two-microphone beamforming must be active.
    aec = skv["aec"]
    aec["status"] = "enable"
    bf = skv["bf"]
    bf["status"] = "enable"
    # The 20261110 2mic_2ref package uses num_channel for the BF microphone
    # array width. Do not add the legacy num_array_mic key: this schema ignores it.
    bf["num_channel"] = 2
    anr = skv["anr"]
    anr["status"] = "enable"

    # Sherpa KWS is the product wake-word source; avoid running duplicate
    # Rockchip wake/keyword detectors in the capture plugin.
    for name in ("wakeup", "unkws", "command", "doa"):
        if name in skv:
            skv[name]["status"] = "disable"

    # Keep the weight file shipped with this exact 2mic_2ref config and make
    # its runtime location deterministic.
    weight = Path(bf["param_path"]).name
    if not weight.startswith("wopt_2mic_") or not weight.endswith(".bin"):
        raise ValueError(f"2mic BF weight path is unexpected: {bf['param_path']}")
    bf["param_path"] = f"/usr/share/rkaudio/{weight}"

    errors = []
    if aec.get("status") != "enable":
        errors.append("AEC must be enabled")
    if bf.get("status") != "enable" or bf.get("num_channel") != 2:
        errors.append("BF must be enabled for 2 microphones")
    if anr.get("status") != "enable":
        errors.append("ANR must be enabled")
    for name in ("wakeup", "unkws", "command", "doa"):
        if name in skv and skv[name].get("status") != "disable":
            errors.append(f"{name} must be disabled; Sherpa KWS owns wake detection")
    if errors:
        raise ValueError("invalid VQE config: " + "; ".join(errors))

    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return data


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        patch(sys.argv[1], sys.argv[2])
    except (OSError, ValueError, KeyError) as error:
        print(f"patch-vqe-config: {error}", file=sys.stderr)
        sys.exit(1)
    print("VQE config: 2mic+2ref, AEC/BF/ANR enabled; duplicate wake detectors disabled")

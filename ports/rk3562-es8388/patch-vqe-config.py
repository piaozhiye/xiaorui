#!/usr/bin/env python3
"""把 Rockchip VQE 包里的配置改成本板 1 mic + 1 ref 形态, 并校验关键参数。

用法: patch-vqe-config.py <源 JSON> <目标 JSON>

改写: wakeup/unkws/command 全部 disable —— 唤醒由前端 sherpa KWS 负责,
VQE 自带的 KWS 词表不是同一真源, 两套并行会重复识别、白占 CPU。

校验: 不信任用户给的插件包内容。aec/bf/anr 的关键参数必须是实测过的
1mic1ref 值, 否则 AEC 静默失效但部署脚本仍报告成功。
"""
import json
import sys


def patch(source: str, destination: str) -> dict:
    with open(source, encoding="utf-8") as stream:
        data = json.load(stream)
    skv = data["skv_configs"]

    for key in ("wakeup", "unkws", "command"):
        if key in skv:
            skv[key]["status"] = "disable"

    errors = []
    if skv.get("aec", {}).get("status") != "enable":
        errors.append("aec.status 必须是 enable")
    if skv.get("bf", {}).get("num_array_mic") != 1:
        errors.append("bf.num_array_mic 必须是 1 (1mic1ref)")
    if skv.get("anr", {}).get("status") != "enable":
        errors.append("anr.status 必须是 enable")
    for key in ("wakeup", "unkws", "command"):
        if key in skv and skv[key].get("status") != "disable":
            errors.append(f"{key}.status 必须是 disable")
    if errors:
        raise ValueError("VQE 配置不合格: " + "; ".join(errors))

    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
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
    print("VQE JSON: aec=enable bf.mics=1 anr=enable, wakeup/unkws/command=disable")

"""Pure scoring for wake/silent acoustic evidence; no board or audio access."""
import re


def quiet_score(frontend):
    counts = dict(vad=frontend.count("[xr] 说完 ("),
                  noise=frontend.count("回执(噪声)"),
                  wakes=frontend.count("回执(唤醒)") + frontend.count("(纯唤醒, 等指令)"),
                  answers=frontend.count("回答完毕"))
    counts["ok"] = counts["wakes"] == 0 and counts["answers"] == 0
    return counts


def wake_score(item, frontend, gateway):
    prefixes = item.get("accepted_prefixes", [item.get("prefix")])
    stripped = "一句式唤醒剥前缀:" in gateway
    matched_prefix = next((prefix for prefix in prefixes if prefix and re.search(
        r"一句式唤醒剥前缀: " + re.escape(prefix) + r"小", gateway)), None)
    prefix_seen = matched_prefix is not None
    rejected = "云端转写未含唤醒词, 不答话:" in gateway
    answered = "回答完毕" in frontend
    local_wake = "(纯唤醒, 等指令)" in frontend or "回执(唤醒)" in frontend
    ok = stripped and prefix_seen and answered if item["positive"] else rejected and not answered and not local_wake
    return dict(id=item["id"], ok=ok, stripped=stripped, prefix_seen=prefix_seen,
                matched_prefix=matched_prefix,
                rejected=rejected, answered=answered, local_wake=local_wake)


if __name__ == "__main__":
    # Independently re-score saved logs, without repeating board operations.
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Re-score saved wake evidence; no board writes")
    ap.add_argument("evidence", type=Path)
    ap.add_argument("manifest", type=Path)
    args = ap.parse_args()
    original = json.loads((args.evidence / "summary.json").read_text())
    quiet = quiet_score((args.evidence / "quiet-0.log").read_text())
    cases = []
    for item in json.loads(args.manifest.read_text())["cases"]:
        front = (args.evidence / (item["id"] + "-0.log")).read_text()
        gateways = "\n".join((args.evidence / (item["id"] + f"-{i}.log")).read_text()
                             for i in (1, 2))
        cases.append(wake_score(item, front, gateways))
    result = dict(quiet=quiet, wake_cases=cases, restored=original["restored"],
                  ha_exit=original.get("ha_exit"), evidence=str(args.evidence))
    result["ok"] = (quiet["ok"] and all(c["ok"] for c in cases)
                    and result["ha_exit"] == 0 and result["restored"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)

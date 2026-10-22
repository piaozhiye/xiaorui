#!/usr/bin/env python3
"""Conservative log scoring: expected command, then completion, not completion alone."""
import argparse
import json
from pathlib import Path
import re
import sys
import unicodedata


def normalize(text):
    # Cloud JSON strings may still be escaped by the lightweight C parser.
    if "\\u" in text:
        try:
            text = json.loads('"' + text + '"')
        except ValueError:
            pass
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if not c.isspace()
                   and not unicodedata.category(c).startswith("P"))
    if text.startswith("小瑞小瑞"):
        text = text[len("小瑞小瑞"):]
    return text


def edit_distance(left, right):
    row = list(range(len(right) + 1))
    for i, char in enumerate(left, 1):
        nxt = [i]
        for j, other in enumerate(right, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j - 1] + (char != other)))
        row = nxt
    return row[-1]


def score(log, expected, variants=None, patterns=None):
    declared_variants = variants is not None
    variants = variants or []
    # Explicit measured variants take precedence. Never accept arbitrary prefixes
    # or substrings: e.g. "不要播放" must not match "播放".
    references = variants or [expected]
    limit = 0 if variants or len(normalize(expected)) <= 2 else (
        1 if len(normalize(expected)) <= 4 else 2)
    result = dict(ok=False, reason="waiting-for-command-and-answer",
                  expected_text=expected, expected_variants=variants,
                  match_policy="measured-variants" if variants else "bounded-edit-distance",
                  max_edit_distance=limit, edit_distance=None, matched_reference="",
                  accepted_by="", unseen_transcript=False,
                  asr_text="", command_text="", path="")
    if declared_variants and (not isinstance(variants, list) or not variants
            or any(not isinstance(ref, str) or not normalize(ref) for ref in variants)):
        result["reason"] = "invalid-expected-variants"
        return result
    if not normalize(expected):
        result["reason"] = "missing-expected-text"
        return result
    observed = ""
    for line in log.splitlines():
        cmd = re.search(r"\[xr\] \((唤醒|跟说中)\) 指令: (.*)", line)
        stt = re.search(r"\[xr\] ASR 回执: (.*)", line)
        if cmd:
            observed = cmd[2]
            result.update(command_text=observed, path=cmd[1])
        elif "[xr] 跟说音频交服务端 ASR" in line:
            observed = ""
            result["path"] = "cloud-asr-followup"
        elif stt:
            observed = stt[1]
            result["asr_text"] = observed
            if not result["path"]:
                result["path"] = "asr-receipt"
        elif "疑余音), 忽略:" in line:
            observed = ""
        elif "放弃本轮" in line:
            result["reason"] = "turn-abandoned"
            return result
        elif "[xr] 回答完毕" in line:
            distance, reference = min(
                (edit_distance(normalize(observed), normalize(ref)), ref)
                for ref in references)
            result.update(edit_distance=distance, matched_reference=reference)
            literal_match = distance <= limit
            pattern_match = next((pattern for pattern in (patterns or [])
                                  if re.fullmatch(pattern, normalize(observed))), "")
            result["ok"] = bool(normalize(observed)) and (literal_match or bool(pattern_match))
            result["accepted_by"] = "reference" if literal_match else (
                "fixture-pattern" if pattern_match else "")
            result["unseen_transcript"] = bool(normalize(observed)) and bool(variants) and distance != 0
            result["matched_pattern"] = pattern_match
            result["reason"] = "matched-command-and-answer" if result["ok"] else "unrelated-answer"
            return result
    return result


def expected_text(manifest, wav):
    for item in manifest["items"]:
        if Path(item["file"]).name == wav:
            # Pure wake recordings are NOT commands. A fixture with a command
            # tail must declare that tail explicitly, rather than match any STT.
            return item.get("command_text", "") if item["category"] == "wake" else item["text"]
    return ""


def expected_variants(manifest, wav):
    for item in manifest["items"]:
        if Path(item["file"]).name == wav:
            return item.get("expected_variants")
    return None


def expected_patterns(manifest, wav):
    for item in manifest["items"]:
        if Path(item["file"]).name == wav:
            return item.get("expected_patterns", [])
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--id", default="")
    parser.add_argument("--cat", default="")
    parser.add_argument("--phase", default="command")
    parser.add_argument("--failure", default="")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence:
        result = json.loads(args.evidence.read_text())
    else:
        manifest = json.loads(args.manifest.read_text())
        result = score(sys.stdin.read(), expected_text(manifest, args.wav),
                       expected_variants(manifest, args.wav),
                       expected_patterns(manifest, args.wav))
    result.update(id=args.id, cat=args.cat, wav=args.wav, phase=args.phase)
    if args.failure:
        result["scoring_reason"] = result["reason"]
        result.update(ok=False, reason=args.failure)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if result["ok"]:
        return 0
    return 1 if result["reason"] == "waiting-for-command-and-answer" else 2


if __name__ == "__main__":
    sys.exit(main())

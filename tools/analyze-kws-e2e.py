#!/usr/bin/env python3
"""Validate aggregate counts for the fixed repeated KWS/ASR test fixture.

This checks a corpus run, not general speech-recognition accuracy. The frontend
currently does not emit a fixture/trial ID, so the report deliberately avoids
claiming per-trial attribution.
"""
import argparse
import re
import sys
import unicodedata
from pathlib import Path


def normalize(text):
    return "".join(
        char for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frontend_log", type=Path)
    parser.add_argument("expected_trials", type=int)
    parser.add_argument("--startup-discarded-trials", type=int, default=0)
    parser.add_argument("--expected-asr", default="现在几点了")
    parser.add_argument("--feed-log", type=Path, required=True)
    parser.add_argument("--expected-feed-sha256")
    parser.add_argument("--expected-feed-bytes", type=int)
    parser.add_argument("--expected-feed-loops", type=int, default=1)
    parser.add_argument("--expected-feed-preroll-bytes", type=int, default=0)
    parser.add_argument("--expected-feed-tail-bytes", type=int, default=0)
    args = parser.parse_args()
    if args.expected_trials <= 0:
        parser.error("expected_trials must be positive")
    if not 0 <= args.startup_discarded_trials < args.expected_trials:
        parser.error("startup-discarded-trials must be between zero and expected-trials - 1")
    effective_trials = args.expected_trials - args.startup_discarded_trials

    log = args.frontend_log.read_text(encoding="utf-8", errors="replace")
    counts = {
        "kws_hits": len(re.findall(r"KWS 命中", log)),
        "utterances": len(re.findall(r"\] 说完 \(", log)),
        "submitted": len(re.findall(r"跟说音频交服务端 ASR", log)),
        "silent": len(re.findall(r"服务端静默回执", log)),
        "answers": len(re.findall(r"\] 回答完毕", log)),
        "empty_turns": len(re.findall(r"\] 空轮", log)),
        "crashes": len(re.findall(r"\bCRASH\b", log)),
        "errors": len(re.findall(r"\bERROR\b", log)),
    }
    asr_texts = re.findall(r"\] ASR 回执: ?(.*)", log)
    expected = normalize(args.expected_asr)
    matched = [text for text in asr_texts if normalize(text) == expected]
    mismatched = [text for text in asr_texts if normalize(text) != expected]

    print(f"固定夹具轮数: {args.expected_trials}")
    print(f"启动校准排除轮数: {args.startup_discarded_trials}; 业务有效轮数: {effective_trials}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"ASR回执: {len(asr_texts)}; 精确匹配预期文本: {len(matched)}; 不匹配: {len(mismatched)}")
    for text in mismatched[:10]:
        print(f"  ASR不匹配: {text!r}")

    feed = args.feed_log.read_text(encoding="utf-8", errors="replace")
    start = re.search(
        r"^feed_start loops=(\d+) bytes_per_loop=(\d+) total_bytes=(\d+) sha256=([0-9a-f]{64})(?: preroll_bytes=(\d+))?(?: tail_bytes=(\d+))?$",
        feed,
        re.M,
    )
    done = re.search(r"^feed_done loops=(\d+) bytes=(\d+)/(\d+) frames=(\d+)(?: preroll_bytes=(\d+))?(?: tail_bytes=(\d+))?", feed, re.M)
    errors = re.findall(r"^feed_error .*", feed, re.M)
    loop_lines = re.findall(r"^feed_loop_done index=", feed, re.M)
    feed_ok = bool(start and done) and not errors
    if start:
        start_loops, bytes_per_loop, total_bytes = map(int, start.groups()[:3])
        start_sha = start.group(4)
        preroll_bytes = int(start.group(5) or 0)
        tail_bytes = int(start.group(6) or 0)
        feed_ok &= (
            start_loops == args.expected_feed_loops
            and total_bytes == bytes_per_loop * start_loops + preroll_bytes + tail_bytes
            and (args.expected_feed_bytes is None or bytes_per_loop == args.expected_feed_bytes)
            and (args.expected_feed_sha256 is None or start_sha == args.expected_feed_sha256)
            and preroll_bytes == args.expected_feed_preroll_bytes
            and tail_bytes == args.expected_feed_tail_bytes
        )
        print(
            f"喂流输入: loops={start_loops}, bytes/loop={bytes_per_loop}, "
            f"sha256={start_sha}, preroll_bytes={preroll_bytes}, tail_bytes={tail_bytes}, source_hash_match="
            f"{start_sha == args.expected_feed_sha256 if args.expected_feed_sha256 else 'not_checked'}"
        )
    else:
        print("喂流: 缺少 feed_start 元数据")
    if done:
        loops, written, expected_bytes, frames = map(int, done.groups()[:4])
        preroll_bytes = int(done.group(5) or 0)
        tail_bytes = int(done.group(6) or 0)
        feed_ok &= (
            loops == args.expected_feed_loops
            and written == expected_bytes
            and expected_bytes == (bytes_per_loop * loops + preroll_bytes + tail_bytes if start else expected_bytes + 1)
            and (args.expected_feed_bytes is None or (expected_bytes - preroll_bytes - tail_bytes) == args.expected_feed_bytes * args.expected_feed_loops)
            and preroll_bytes == args.expected_feed_preroll_bytes
            and tail_bytes == args.expected_feed_tail_bytes
            and len(loop_lines) == loops
        )
        print(f"喂流: loops={loops}, bytes={written}/{expected_bytes}, frames={frames}, preroll_bytes={preroll_bytes}, tail_bytes={tail_bytes}, 完成循环日志={len(loop_lines)}")
    else:
        print("喂流: 缺少完整 feed_done 记录")
    for line in errors:
        print(line)

    exact = (
        counts["utterances"] == effective_trials
        and counts["kws_hits"] > 0
        and counts["submitted"] == effective_trials
        and counts["silent"] == 0
        and len(asr_texts) == effective_trials
        and len(matched) == effective_trials
        and counts["answers"] == effective_trials
        and counts["empty_turns"] == 0
        and counts["crashes"] == 0
        and counts["errors"] == 0
        and args.expected_feed_loops > 0
        and feed_ok
    )
    print("结果: " + ("PASS (仅限此固定夹具的汇总门禁)" if exact else "FAIL / INCOMPLETE"))
    if not exact:
        print("说明: 任一总数不符、ASR文本不符、喂流不完整或存在错误，均不得判通过。")
    print("限制: 当前日志没有输入轮次ID；本结果是固定语料的汇总门禁，不等同于逐轮归因或泛化准确率。")
    return 0 if exact else 1


if __name__ == "__main__":
    sys.exit(main())

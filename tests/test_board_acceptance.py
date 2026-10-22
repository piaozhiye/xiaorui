"""Acceptance script safety regressions. All board/audio operations are mocked."""
import json
import importlib.util
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/run-board-acceptance.sh"
spec = importlib.util.spec_from_file_location("turn_score", SCRIPT.with_name("score-acceptance-turn.py"))
scorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorer)


class AcceptanceTests(unittest.TestCase):
    def test_dialog_counts_json_fields_independent_of_key_order(self):
        proc, files = self.run_shell("""
restart_frontend() { :; }
warmup() { :; }
sleep() { :; }
board() { echo 3; }
round_one_phase() {
    printf '{"ok":true,"id":"%s","cat":"%s","phase":"command"}\\n' "$1" "$2" >> "$OUT/turns.jsonl"
}
round_two_phase() { round_one_phase "$@"; }
stage_dialog
""")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("7/7", files["summary.txt"])
        self.assertIn("long=2, short=1", files["summary.txt"])

    def test_duplicate_dialog_round_cannot_replace_missing_round(self):
        proc, _ = self.run_shell("""
restart_frontend() { :; }
warmup() { :; }
sleep() { :; }
board() { echo 3; }
round_one_phase() {
    printf '{"ok":true,"id":"D1","cat":"%s","phase":"command"}\\n' "$2" >> "$OUT/turns.jsonl"
}
round_two_phase() { round_one_phase "$@"; }
stage_dialog; test $? -eq 1
""")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def run_shell(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XR_ACCEPT_LIBRARY_ONLY="1", XR_ACCEPT_OUT=tmp)
            proc = subprocess.run(
                ["bash", "-c", 'script="$1"; shift; source "$script"; ' + body, "test", str(SCRIPT)],
                env=env, capture_output=True, text=True, timeout=15)
            files = {p.name: p.read_text() for p in Path(tmp).glob("*") if p.is_file()}
            return proc, files

    def test_missing_gates_never_promote(self):
        proc, files = self.run_shell('''
board() { echo "$*" >> "$OUT/ops"; }
stage_reboot; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ops", files)

    def test_missing_snapshot_blocks_promotion(self):
        proc, files = self.run_shell('''
board() { echo "$*" >> "$OUT/ops"; }
RES[dialog]=PASS; RES[fault]=PASS; RES[soak]=PASS
stage_reboot; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ops", files)

    def test_gateway_kill_pattern_does_not_match_own_shell(self):
        pattern = r"[/]data/xiaorui-gateway/releases/[^/]*/server/app[.]py"
        self.assertIsNotNone(re.search(pattern, "/data/miniforge/bin/python /data/xiaorui-gateway/releases/4110fb09ea49/server/app.py"))
        self.assertIsNone(re.search(pattern, f'/bin/bash -c PID=$(pgrep -f "{pattern}"); kill $PID'))
        script = SCRIPT.read_text()
        self.assertEqual(script.count('pgrep -f "' + pattern + '"'), 4)

    def test_same_candidate_fails_snapshot_before_mutations(self):
        proc, files = self.run_shell('''
board() {
    echo "$*" >> "$OUT/ops"
    printf '%064d  prod\n%064d  cand\n' 1 1
}
stage_snapshot; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(files["ops"].splitlines()), 1)
        self.assertIn("候选与生产相同", files["summary.txt"])
        self.assertNotIn("snapshot.log", files)

    def test_different_but_wrong_build_is_rejected(self):
        proc, files = self.run_shell('''
board() { printf '%064d  prod\n%064d  cand\n' 1 2; }
require_candidate; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("板上候选与本机构建不一致", files["summary.txt"])

    def test_expected_candidate_is_accepted(self):
        proc, _ = self.run_shell('''
board() {
    printf '%064d  prod\n' 1
    sha256sum "$REPO/board/xiaorui_live"
}
require_candidate
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_failed_dialog_round_is_not_replayed(self):
        proc, files = self.run_shell('''
logpos() { echo 0; }
play() { echo "$1" >> "$OUT/played"; }
wait_turn() { return 1; }
warmup() { echo unexpected-retry >> "$OUT/played"; }
round_one_phase D1 cold-wake; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files["played"].splitlines(), ["wake2_time.wav"])
        self.assertFalse(json.loads(files["turns.jsonl"])["ok"])

    def test_selfcheck_wrong_variants_stops_before_expensive_stages(self):
        proc, files = self.run_shell('''
restart_frontend() { return 0; }
logpos() { echo 0; }
play() { return 0; }
board() { printf '[xr] (唤醒) 指令: 好的\n[xr] 回答完毕\n'; }
sleep() { return 0; }
fixture_selfcheck; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(files["selfcheck.jsonl"])
        self.assertFalse(row["ok"])
        self.assertIn("未进入长跑/故障/晋升", files["summary.txt"])

    def test_selfcheck_measured_variant_passes(self):
        proc, files = self.run_shell('''
WAKE1=wake_pos049.wav
restart_frontend() { return 0; }
logpos() { echo 0; }
play() { return 0; }
board() { printf '[xr] (唤醒) 指令: 晚安全\n[xr] 回答完毕\n'; }
sleep() { return 0; }
settle() { return 0; }
fixture_selfcheck
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(files["selfcheck.jsonl"])
        self.assertTrue(row["ok"])
        self.assertEqual(row["matched_reference"], "晚安全")

    def test_current_selfcheck_uses_declared_time_command(self):
        proc, files = self.run_shell('''
restart_frontend() { return 0; }
logpos() { echo 0; }
play() { return 0; }
board() { printf '[xr] ASR 回执: 现在几点了\n[xr] 回答完毕\n'; }
sleep() { return 0; }
settle() { return 0; }
fixture_selfcheck
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(files["selfcheck.jsonl"])
        self.assertTrue(row["ok"])
        self.assertEqual(row["expected_text"], "现在几点了")

    def test_failure_after_start_restores_frontend(self):
        proc, files = self.run_shell('''
board() { echo "$*" >> "$OUT/ops"; }
FRONTEND_TOUCHED=1
exit 7
''')
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertIn("sh /data/xiaorui-gateway/restore-frontend.sh", files["ops"])

    def test_expired_selfcheck_budget_stops_without_read_or_sleep(self):
        proc, files = self.run_shell('''
SELFCHECK_DEADLINE=$((SECONDS-1))
board() { echo unexpected-read >> "$OUT/ops"; }
sleep() { echo unexpected-sleep >> "$OUT/ops"; }
wait_turn wake_pos049.wav 90 0; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ops", files)

    def test_wait_and_record_capture_matching_cloud_text(self):
        proc, files = self.run_shell('''
board() { printf '[xr] 跟说音频交服务端 ASR\n[xr] ASR 回执: 现在几点了\n[xr] 回答完毕\n'; }
wait_turn cmd_time.wav 1 0 || exit 1
record_turn D2 in-window-cmd cmd_time.wav command
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(files["turns.jsonl"])
        self.assertTrue(row["ok"])
        self.assertEqual(row["asr_text"], "现在几点了")
        self.assertEqual(row["wav"], "cmd_time.wav")
        self.assertEqual(row["path"], "cloud-asr-followup")

    def test_echo_wait_is_terminal_failure(self):
        proc, files = self.run_shell('''
board() { printf '[xr] (跟说中) 指令: 好的\n[xr] 回答完毕\n'; }
sleep() { exit 99; }
wait_turn cmd_time.wav 300 0; test $? -eq 2 || exit 1
record_turn D2 in-window-cmd cmd_time.wav command unmatched
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(files["turns.jsonl"])
        self.assertFalse(row["ok"])
        self.assertEqual(row["command_text"], "好的")
        self.assertEqual(row["scoring_reason"], "unrelated-answer")

    def test_explicit_play_device_is_preserved(self):
        proc, files = self.run_shell('''
PLAY_DEV=plughw:Audio,0
aplay() { printf '%s\n' "$@" > "$OUT/play-args"; }
play wake_pos049.wav
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files["play-args"].splitlines()[:3], ["-D", "plughw:Audio,0", "-q"])

    def test_selfcheck_only_no_activity_timeout_retries_once(self):
        proc, files = self.run_shell('''
restart_frontend() { return 0; }
sleep() { echo "$1" >> "$OUT/sleeps"; }
logpos() { echo 0; }
play() { echo "$1" >> "$OUT/played"; }
wait_turn() { WAIT_FAILURE=no-mic-activity; return 1; }
fixture_selfcheck; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(files["played"].splitlines()), 2)
        self.assertEqual(files["sleeps"].splitlines(), ["8", "8"])
        self.assertEqual(len(files["selfcheck.jsonl"].splitlines()), 2)

    def test_selfcheck_active_but_incomplete_answer_does_not_retry(self):
        proc, files = self.run_shell('''
restart_frontend() { return 0; }
sleep() { return 0; }
logpos() { echo 0; }
play() { echo "$1" >> "$OUT/played"; }
wait_turn() { WAIT_FAILURE=timeout-incomplete-turn; return 1; }
fixture_selfcheck; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(files["played"].splitlines()), 1)

    def test_one_failed_dialog_case_fails_stage(self):
        proc, files = self.run_shell('''
restart_frontend() { return 0; }
warmup() { return 0; }
sleep() { return 0; }
board() { echo 3; }
round_one_phase() {
    if [ "$1" = "D1" ]; then
        printf '{"id":"D1","ok":false}\n' >> "$OUT/turns.jsonl"
        return 1
    fi
    printf '{"id":"%s","cat":"%s","ok":true,"phase":"command"}\n' "$1" "$2" >> "$OUT/turns.jsonl"
}
round_two_phase() { round_one_phase "$@"; }
stage_dialog; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("6/7", files["summary.txt"])
        self.assertIn("retries=0", files["summary.txt"])

    def test_signal_cleans_owned_route_and_rolls_back(self):
        proc, files = self.run_shell('''
board() { echo "$*" >> "$OUT/ops"; }
ROUTE_ADDED=1
PROMOTION_PENDING=1
PRODUCTION_BACKUP=/data/xiaorui-server/board-scripts/xiaorui_live.bak-test
kill -TERM $$
''')
        self.assertEqual(proc.returncode, 143, proc.stderr)
        self.assertIn("ip route del blackhole default", files["ops"])
        self.assertIn("cp '/data/xiaorui-server/board-scripts/xiaorui_live.bak-test'", files["ops"])
        self.assertIn("sh /data/xiaorui-server/start-all.sh", files["ops"])

    def test_unowned_route_is_untouched(self):
        proc, files = self.run_shell('board() { echo "$*" >> "$OUT/ops"; }; exit 0')
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("ops", files)

    def test_cron_restores_exact_snapshot(self):
        proc, files = self.run_shell('''
board() {
    if [[ "$*" == *'crontab -l 2>/dev/null || true'* ]]; then
        printf '1 * * * * unrelated-job\n*/5 * * * * /data/xiaorui-gateway/start-gateway.sh\n'
    fi
}
board_stdin() { cat > "$OUT/restored-cron"; }
cron_off || exit 1
cron_on || exit 1
test "$CRON_SAVED" -eq 0
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(files["restored-cron"],
                         "1 * * * * unrelated-job\n"
                         "*/5 * * * * /data/xiaorui-gateway/start-gateway.sh\n")

    def test_reboot_failure_rolls_back_before_report(self):
        proc, files = self.run_shell('''
board() { echo "$*" >> "$OUT/ops"; }
stage_reboot_impl() {
    PROMOTION_PENDING=1
    PRODUCTION_BACKUP=/data/xiaorui-server/board-scripts/xiaorui_live.bak-test
    return 1
}
stage_reboot; test $? -eq 1 || exit 1
test "$PROMOTION_PENDING" -eq 0
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("xiaorui_live.bak-test", files["ops"])

    def test_cloud_submission_is_not_cloud_answer(self):
        proc, _ = self.run_shell('''
board() { printf 'audio submitted\n'; }
cloud_answer_since 0; test $? -eq 1 || exit 1
board() { printf 'audio submitted\n转本地续答\n回答完毕\n'; }
cloud_answer_since 0; test $? -eq 1 || exit 1
board() { printf 'audio submitted\n回答完毕\n'; }
cloud_answer_since 0
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_soak_plays_commands_and_one_miss_fails(self):
        proc, files = self.run_shell('''
ADB=true
SOAK_MIN=0
board() { return 0; }
wait_device() { return 0; }
sleep() { /bin/sleep 0.01; }
restart_frontend() { return 0; }
warmup() { return 0; }
logpos() { echo 0; }
round_two_phase() {
    echo "$3" >> "$OUT/played"
    [ "$1" != "soak-2" ]
}
stage_soak; test $? -eq 1
''')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        played = files["played"].splitlines()
        self.assertEqual(len(played), 30)
        self.assertIn("cmd_weather.wav", played)
        self.assertIn("cmd_music.wav", played)
        self.assertIn("long_story.wav", played)
        rows = [json.loads(line) for line in files["soak-turns.jsonl"].splitlines()]
        self.assertEqual(sum(row["ok"] for row in rows), 29)


class TurnScoringTests(unittest.TestCase):
    def test_new_wake_prefix_is_bounded_and_counted(self):
        manifest = json.loads((SCRIPT.parent.parent / "tests/audio/manifest.json").read_text())
        wav = "wake_pos049.wav"
        result = scorer.score("[xr] (唤醒) 指令: 天天小月小月晚安\n[xr] 回答完毕",
                              scorer.expected_text(manifest, wav),
                              scorer.expected_variants(manifest, wav),
                              scorer.expected_patterns(manifest, wav))
        self.assertTrue(result["ok"])
        self.assertTrue(result["unseen_transcript"])
        self.assertEqual(result["accepted_by"], "fixture-pattern")

    def test_unknown_arbitrary_instructions_are_not_accepted(self):
        pattern = r"^(?:天天)?(?:小[一-鿿]){2}晚安$"
        for text in ["好的", "请别说晚安", "不要晚安", "晚安然后播放音乐", "今天天气晚安"]:
            with self.subTest(text=text):
                result = scorer.score(f"[xr] (跟说中) 指令: {text}\n[xr] 回答完毕",
                                      "晚安", ["晚安"], [pattern])
                self.assertFalse(result["ok"])
    def test_measured_wake_variants_all_pass(self):
        variants = ["晚安", "晚安全", "天天小绿小路晚安"]
        for text in variants:
            with self.subTest(text=text):
                result = scorer.score(f"[xr] (唤醒) 指令: {text}\n[xr] 回答完毕", "晚安", variants)
                self.assertTrue(result["ok"])
                self.assertEqual(result["edit_distance"], 0)

    def test_declared_variants_are_a_real_gate(self):
        result = scorer.score("[xr] (唤醒) 指令: 晚安\n[xr] 回答完毕", "晚安", ["完全不同"])
        self.assertFalse(result["ok"])

    def test_empty_declared_variants_do_not_fall_back_to_canonical(self):
        result = scorer.score("[xr] (唤醒) 指令: 晚安\n[xr] 回答完毕", "晚安", [])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-expected-variants")

    def test_bounded_asr_error_is_recorded(self):
        result = scorer.score("[xr] (跟说中) 指令: 现在几电了\n[xr] 回答完毕", "现在几点了")
        self.assertTrue(result["ok"])
        self.assertEqual(result["edit_distance"], 1)
        self.assertEqual(result["max_edit_distance"], 2)

    def test_short_commands_and_negation_are_not_prefix_matches(self):
        for observed, expected in [("屏上", "停下"), ("不要播放", "播放"), ("好的", "停下")]:
            self.assertFalse(scorer.score(f"[xr] (跟说中) 指令: {observed}\n[xr] 回答完毕", expected)["ok"])

    def test_completion_alone_is_not_success(self):
        self.assertFalse(scorer.score("[xr] 回答完毕", "现在几点了")["ok"])

    def test_echo_command_is_not_expected_command(self):
        result = scorer.score("[xr] (跟说中) 指令: 好的\n[xr] 回答完毕", "现在几点了")
        self.assertFalse(result["ok"])
        self.assertEqual(result["command_text"], "好的")

    def test_expected_local_command_with_punctuation(self):
        result = scorer.score("[xr] (唤醒) 指令: 现在几点了？\n[xr] 回答完毕", "现在几点了")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "唤醒")

    def test_expected_cloud_receipt_is_recorded(self):
        result = scorer.score("[xr] 跟说音频交服务端 ASR\n[xr] ASR 回执: 今天天气怎么样\n[xr] 回答完毕", "今天天气怎么样")
        self.assertTrue(result["ok"])
        self.assertEqual(result["asr_text"], "今天天气怎么样")
        self.assertEqual(result["path"], "cloud-asr-followup")

    def test_mismatched_receipt_overrides_local_guess(self):
        result = scorer.score("[xr] (唤醒) 指令: 现在几点了\n[xr] ASR 回执: 好的\n[xr] 回答完毕", "现在几点了")
        self.assertFalse(result["ok"])

    def test_done_before_command_cannot_be_reused(self):
        result = scorer.score("[xr] 回答完毕\n[xr] (跟说中) 指令: 现在几点了", "现在几点了")
        self.assertFalse(result["ok"])

    def test_ignored_echo_is_not_a_submitted_command(self):
        result = scorer.score("[xr] (跟说中) 指令: 好的\n[xr] 窗内超短指令(疑余音), 忽略: 好的\n[xr] 回答完毕", "好的")
        self.assertFalse(result["ok"])

    def test_abandoned_turn_is_failure(self):
        result = scorer.score("[xr] (跟说中) 指令: 现在几点了\n[xr] 等待仍无音频，放弃本轮\n[xr] 回答完毕", "现在几点了")
        self.assertFalse(result["ok"])

    def test_missing_fixture_expectation_cannot_match(self):
        self.assertFalse(scorer.score("[xr] 回答完毕", "")["ok"])


if __name__ == "__main__":
    unittest.main()

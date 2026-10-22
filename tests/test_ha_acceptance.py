import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ha_acceptance import (score, exit_code, playback_ready, parse_follow_window,
                           retryable, pre_state, CASES, SETTLED)
from core.ha_audit import accepted, completed


class AcceptanceTests(unittest.TestCase):
    def test_muted_playback_preflight(self):
        self.assertTrue(playback_ready("Playback 78 [90%] [-6.75dB] [on]"))
        self.assertFalse(playback_ready("Playback 0 [0%] [-65.25dB] [on]"))
        self.assertFalse(playback_ready("Playback 78 [90%] [-6.75dB] [off]"))
        self.assertFalse(playback_ready("no control"))
        # 音量 90% 但 -65dB = 等于静音, 放行只会让三例都 no-wake
        self.assertFalse(playback_ready("Playback 78 [90%] [-65.25dB] [on]"))
    def evidence(self, **changes):
        event = dict(sentence_id="sid", session_id="session", text="帮我关灯",
                     op="off", domain="light", entities=["light.test"], result="acted")
        event.update(changes)
        return "\n".join("HA_AUDIT " + json.dumps(e) for e in (
            dict(event, phase="accepted"), dict(event, phase="completed", packets=4, aborted=False)))

    WAKE = "[xr] KWS 命中: 小瑞\n"

    def check(self, gateway=None, frontend=None, before="on", after="off"):
        return score(self.WAKE + (frontend or '[xr] ASR 回执: 帮我关灯\n{"session_id":"session"}\n[xr] 回答完毕'),
                     self.evidence() if gateway is None else gateway,
                     "off", before, after, "light.test")

    def test_action(self):
        self.assertEqual(self.check(), (True, "acted"))

    def test_missing_wake_word_is_reported_as_no_wake(self):
        ok, reason = score('[xr] ASR 回执: 帮我关灯\n{"session_id":"session"}\n[xr] 回答完毕',
                           self.evidence(), "off", "on", "off", "light.test")
        self.assertFalse(ok)
        self.assertEqual(reason, "no-voice-evidence")

    def test_noop_never_counts_as_a_pass(self):
        # 目标态已等于期望态时"已经是开的"不构成任何翻转, 不许算通过
        self.assertFalse(self.check(self.evidence(result="noop"), before="off")[0])
        self.assertFalse(self.check(self.evidence(result="noop"))[0])

    def test_transition_must_be_real(self):
        # before == want 时没有翻转可言
        self.assertFalse(self.check(before="off", after="off")[0])

    def test_extra_entity_fails(self):
        self.assertFalse(self.check(self.evidence(entities=["light.test", "light.other"]))[0])

    def test_wrong_operation_entity_or_sentence(self):
        for changes in (dict(op="on"), dict(domain="switch"),
                        dict(entities=["light.other"]), dict(text="好好"), dict(sentence_id="")):
            with self.subTest(changes=changes):
                self.assertFalse(self.check(self.evidence(**changes))[0])

    def test_no_legacy_pass(self):
        self.assertFalse(self.check("HA light/turn_off: ['light.test'] 灯已经是关的")[0])

    def test_completion_must_match(self):
        lines = self.evidence().splitlines()
        done = json.loads(lines[1].split("HA_AUDIT ")[1])
        for changes in (dict(sentence_id="other"), dict(session_id="other"),
                        dict(packets=0), dict(packets="4"), dict(packets=True),
                        dict(aborted=True), dict(op="on")):
            gateway = lines[0] + "\nHA_AUDIT " + json.dumps(dict(done, **changes))
            self.assertFalse(self.check(gateway)[0])

    def test_unrelated_answer_and_multiple_commands(self):
        for frontend in ("回答完毕\nASR 回执: 帮我关灯",
                         "ASR 回执: 好好\n回答完毕",
                         "ASR 回执: 帮我关灯\nASR 回执: 开灯\n回答完毕"):
            self.assertFalse(self.check(frontend=frontend)[0])

    def test_state_and_duplicate_audit(self):
        self.assertFalse(self.check(after="on")[0])
        self.assertFalse(self.check(self.evidence() + "\n" + self.evidence())[0])

    def test_malformed_audit(self):
        for text in ("HA_AUDIT null", "HA_AUDIT broken",
                     self.evidence(entities="light.test2"), self.evidence(text=None)):
            self.assertFalse(self.check(text)[0])

    def test_unicode_and_local_command(self):
        for text in (r"ASR 回执: \u5e2e\u6211\u5173\u706f", "(跟说中) 指令: 帮我关灯"):
            self.assertTrue(self.check(frontend=text + '\n{"session_id":"session"}\n回答完毕')[0])

    def test_follow_window_comes_from_the_board_log(self):
        self.assertEqual(parse_follow_window("[xr] 回答完毕, 30s 内免唤醒"), 30)
        self.assertEqual(parse_follow_window("[xr] 回答完毕, 5s 内免唤醒"), 5)
        self.assertEqual(parse_follow_window("没有这一行"), 30)

    def test_retryable_reasons(self):
        # 声学抖动类: 唤醒漏检/识别错/灯没翻 -> 重放
        for reason in ("no-voice-evidence", "missing-command", "wrong-command",
                       "ambiguous-commands", "state-mismatch", "missing-player-finish"):
            self.assertTrue(retryable(reason), reason)
        # 审计契约被破坏: 立刻判失败, 不靠重放掩盖
        for reason in ("malformed-audit", "invalid-audit", "mismatched-completion"):
            self.assertFalse(retryable(reason), reason)
        # 未分类的理由(比如基础设施故障抛出的异常名)不能当成声学抖动重放
        for reason in ("RuntimeError", "TimeoutExpired", "some-future-reason"):
            self.assertFalse(retryable(reason), reason)

    def test_each_case_forces_the_opposite_pre_state(self):
        for label, wav, want, pre in CASES:
            self.assertEqual(pre, pre_state(want), label)
            self.assertNotEqual(pre, want, label)

    def test_dangling_accepted_record_fails(self):
        # 上一轮的"已受理"没等到完成就会永远悬在网关日志里, 不能当没看见
        other = json.loads(self.evidence().splitlines()[0].split("HA_AUDIT ")[1])
        other.update(sentence_id="s0", text="关灯", op="off")
        gateway = "HA_AUDIT " + json.dumps(other) + "\n" + self.evidence()
        self.assertFalse(self.check(gateway)[0])

    def test_two_accepted_records_fail(self):
        self.assertFalse(self.check(self.evidence() + "\n" + self.evidence())[0])

    def test_wake_evidence_must_precede_the_command(self):
        # 语音入口证据在指令之后 = 是别的轮次的, 不能给这一条指令背书
        frontend = ('[xr] ASR 回执: 帮我关灯\n{"session_id":"session"}\n'
                    '[xr] 回答完毕\n[xr] KWS 命中: 小瑞\n')
        ok, reason = score(frontend, self.evidence(), "off", "on", "off", "light.test")
        self.assertFalse(ok)
        self.assertEqual(reason, "no-voice-evidence")

    def test_rejected_wake_line_is_not_wake_evidence(self):
        frontend = ('[xr] KWS 命中但文本无唤醒单元, 忽略\n'
                    '[xr] ASR 回执: 帮我关灯\n{"session_id":"session"}\n[xr] 回答完毕\n')
        ok, reason = score(frontend, self.evidence(), "off", "on", "off", "light.test")
        self.assertFalse(ok)
        self.assertEqual(reason, "no-voice-evidence")

    def test_cloud_asr_whole_sentence_is_enough_evidence(self):
        # 冷唤醒那轮若声学 KWS 没命中, 整句会被送上云, 而云 ASR 更准
        # (实测整句"小瑞小瑞帮我关灯"被云一次认成"帮我关灯")。那种形态链路是通的,
        # 不该被判 no-wake。
        frontend = ('[xr] 检测到说话\n[xr] 说完 (2.0s), 识别中\n'
                    '[be-cloud] turn=15 audio submitted: 32000 samples\n'
                    '[xr] 跟说音频交服务端 ASR\n'
                    '[xr] ASR 回执: 帮我关灯\n{"session_id":"session"}\n[xr] 回答完毕\n')
        self.assertTrue(self.check(frontend=frontend)[0])

    def test_local_asr_is_voice_evidence(self):
        frontend = ('[xr] 检测到说话\n[xr] 说完 (2.0s), 识别中\n'
                    '[xr] ASR 精修(20s RKNN): 帮我关灯 (0.55s)\n'
                    '[xr] (跟说中) 指令: 帮我关灯\n'
                    '{"session_id":"session"}\n[xr] 回答完毕\n')
        self.assertTrue(self.check(frontend=frontend)[0])

    def test_command_with_internal_space_is_accepted(self):
        # 归一化后再判指令, 免得 "帮我 关灯" 这种被误判成错指令
        frontend = ('[xr] KWS 命中: 小瑞\n[xr] ASR 回执: 帮我 关灯\n'
                    '{"session_id":"session"}\n[xr] 回答完毕\n')
        self.assertTrue(self.check(frontend=frontend)[0])

    def test_settled_set_excludes_not_yet_reasons(self):
        # 这些是"还没到"而不是"坏了": 早收工会把再等两秒就好的轮次判成失败
        for reason in ("state-mismatch", "missing-or-ambiguous-audit",
                       "missing-complete-audio", "wrong-frontend-session"):
            self.assertNotIn(reason, SETTLED, reason)
        for reason in ("no-voice-evidence", "wrong-command", "ambiguous-commands",
                       "no-transition"):
            self.assertIn(reason, SETTLED, reason)

    def test_wrong_frontend_session(self):
        self.assertFalse(self.check(frontend='ASR 回执: 帮我关灯\n{"session_id":"other"}\n回答完毕')[0])

    def test_suite_exit_status(self):
        self.assertEqual(exit_code(3, 0, True), 0)
        for counts in ((2, 1, True), (0, 3, True), (3, 0, False), (0, 0, True)):
            self.assertNotEqual(exit_code(*counts), 0)

    def test_audit_completion_lifecycle(self):
        messages = []
        class Logger:
            def bind(self, **kwargs):
                return self
            def info(self, message):
                messages.append(json.loads(message.split("HA_AUDIT ")[1]))
        conn = SimpleNamespace(sentence_id="sid", session_id="session", logger=Logger(),
                               audio_flow_control={"sentence_id": "sid", "packet_count": 4},
                               client_abort=False)
        accepted(conn, "帮我关灯", "off", "light", ["light.test"], "acted")
        completed(conn, "wrong-sid")
        self.assertEqual(len(messages), 1)
        completed(conn, "sid")
        completed(conn, "sid")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["packets"], 4)
        conn.audio_flow_control["sentence_id"] = "stale"
        accepted(conn, "帮我关灯", "off", "light", ["light.test"], "acted")
        completed(conn, "sid")
        self.assertEqual(messages[-1]["packets"], 0)

    def test_audit_round_trips_through_scorer(self):
        """生产端产出的记录必须能被消费端判过。

        之前的测试都是手写 JSON 喂给 score(), 生产端和消费端从没对过账:
        把 ha_audit.py 里的 entities 改名成 entity_ids, 上面 27 个测试照样全绿,
        板子上却会一直判 malformed-audit。
        """
        lines = []
        class Logger:
            def bind(self, **kwargs):
                return self
            def info(self, message):
                lines.append(message)
        conn = SimpleNamespace(sentence_id="sid", session_id="session", logger=Logger(),
                               audio_flow_control={"sentence_id": "sid", "packet_count": 4},
                               client_abort=False)
        accepted(conn, "帮我关灯", "off", "light", ["light.test"], "acted")
        completed(conn, "sid")
        gateway = "\n".join(lines)
        self.assertEqual(gateway.count("HA_AUDIT "), 2)
        frontend = ('[xr] KWS 命中: 小瑞\n[xr] ASR 回执: 帮我关灯\n'
                    '{"session_id":"session"}\n[xr] 回答完毕\n')
        self.assertEqual(score(frontend, gateway, "off", "on", "off", "light.test"),
                         (True, "acted"))

    def test_audit_round_trip_with_abort_fails_the_scorer(self):
        lines = []
        class Logger:
            def bind(self, **kwargs):
                return self
            def info(self, message):
                lines.append(message)
        conn = SimpleNamespace(sentence_id="sid", session_id="session", logger=Logger(),
                               audio_flow_control={"sentence_id": "sid", "packet_count": 4},
                               client_abort=True)
        accepted(conn, "帮我关灯", "off", "light", ["light.test"], "acted")
        completed(conn, "sid")
        gateway = "\n".join(lines)
        frontend = ('[xr] KWS 命中: 小瑞\n[xr] ASR 回执: 帮我关灯\n'
                    '{"session_id":"session"}\n[xr] 回答完毕\n')
        ok, reason = score(frontend, gateway, "off", "on", "off", "light.test")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing-complete-audio")

    def test_audit_phase_names_are_exact(self):
        lines = []
        class Logger:
            def bind(self, **kwargs):
                return self
            def info(self, message):
                lines.append(json.loads(message.split("HA_AUDIT ")[1]))
        conn = SimpleNamespace(sentence_id="sid", session_id="session", logger=Logger(),
                               audio_flow_control={"sentence_id": "sid", "packet_count": 4},
                               client_abort=False)
        accepted(conn, "帮我关灯", "off", "light", ["light.test"], "acted")
        completed(conn, "sid")
        self.assertEqual([r["phase"] for r in lines], ["accepted", "completed"])

    def test_same_text_in_two_lines_is_one_command(self):
        # 前端本地 ASR 的决定与后端回执是同一轮, 不是"混入多指令"
        frontend = ('[xr] KWS 命中: 小瑞\n[xr] (跟说中) 指令: 帮我关灯\n'
                    '[be] turn=2 submitted\n[xr] ASR 回执: 帮我关灯\n'
                    '{"session_id":"session"}\n[xr] 回答完毕\n')
        self.assertTrue(self.check(frontend=frontend)[0])

    def test_two_different_commands_fail(self):
        frontend = ('[xr] KWS 命中: 小瑞\n[xr] ASR 回执: 帮我关灯\n'
                    '{"session_id":"session"}\n[xr] ASR 回执: 开灯\n[xr] 回答完毕\n')
        ok, reason = score(frontend, self.evidence(), "off", "on", "off", "light.test")
        self.assertFalse(ok)
        self.assertEqual(reason, "ambiguous-commands")

    def test_shell_entrypoint_fails_on_preflight_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            run = subprocess.run(["bash", str(ROOT / "tools/run-ha-acoustic.sh"),
                    "--token-file", str(Path(directory) / "missing-token"),
                    "--output", str(output)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            report = json.loads((output / "summary.json").read_text())
            self.assertGreater(report["failed"], 0)

    def test_mixer_fails_fast_and_uses_card_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stub = path / "amixer"
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$AMIXER_TRACE"\n'
                            'case "$*" in *numid=22*) exit 4;; esac\n')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=directory + ":" + os.environ["PATH"],
                       AMIXER_TRACE=str(path / "calls"))
            run = subprocess.run(["sh", str(ROOT / "board-server/es8388-aec-mixer.sh")],
                                 env=env, capture_output=True, timeout=5)
            self.assertNotEqual(run.returncode, 0)
            calls = (path / "calls").read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertTrue(all("-c rockchipes8388" in c for c in calls))


if __name__ == "__main__":
    unittest.main()

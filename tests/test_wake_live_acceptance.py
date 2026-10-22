import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from wake_live_acceptance import quiet_score, wake_score


class WakeLiveAcceptanceTests(unittest.TestCase):
    def test_quiet_counts_actual_frontend_messages(self):
        result = quiet_score("[xr] 说完 (1.0s), 识别中\n[xr] 服务端静默回执(噪声): 收轮, 不搭话")
        self.assertEqual(result["vad"], 1)
        self.assertEqual(result["noise"], 1)
        self.assertTrue(result["ok"])

    def test_local_and_server_false_wakes_fail(self):
        for log in ("[xr] (纯唤醒, 等指令)", "服务端静默回执(唤醒)", "回答完毕"):
            self.assertFalse(quiet_score(log)["ok"])

    def test_asr_dropping_new_prefix_does_not_prove_prefix_fix(self):
        item = dict(id="thanks", positive=True, prefix="谢谢")
        self.assertFalse(wake_score(item, "回答完毕", "一句式唤醒剥前缀: 小瑞小瑞 + 现在几点了")["ok"])
        self.assertTrue(wake_score(item, "回答完毕", "一句式唤醒剥前缀: 谢谢小瑞小瑞 + 现在几点了")["ok"])

    def test_no_rejection_evidence_is_not_a_negative_pass(self):
        item = dict(id="negative", positive=False)
        self.assertFalse(wake_score(item, "", "")["ok"])
        rejected = "云端转写未含唤醒词, 不答话: 不是小度小度"
        self.assertTrue(wake_score(item, "", rejected)["ok"])
        self.assertFalse(wake_score(item, "回答完毕", rejected)["ok"])
        self.assertFalse(wake_score(item, "(纯唤醒, 等指令)", rejected)["ok"])

    def test_wei_homophones_are_explicit_equivalents(self):
        item = dict(id="wei", positive=True, prefix="喂", accepted_prefixes=["喂", "为"])
        for prefix in ("喂", "为"):
            result = wake_score(item, "回答完毕", "一句式唤醒剥前缀: " + prefix + "小月小月 + 现在几点了")
            self.assertTrue(result["ok"])
            self.assertEqual(result["matched_prefix"], prefix)
        self.assertFalse(wake_score(item, "回答完毕", "一句式唤醒剥前缀: 问问小月小月 + 现在几点了")["ok"])


if __name__ == "__main__":
    unittest.main()

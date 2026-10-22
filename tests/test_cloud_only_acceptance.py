import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from cloud_only_acceptance import startup_settled, window_events, capture_idle


class CloudOnlyReadinessTests(unittest.TestCase):
    def test_mic_ready_is_not_permission_to_play(self):
        self.assertFalse(startup_settled("mic=foo 聆听中\nambient=-34", 5, 5, 30))

    def test_welcome_must_finish_then_expire_window(self):
        log = "mic=foo 聆听中\n主动播报已受理: abc\n"
        self.assertFalse(startup_settled(log, 50, 50, 30))
        log += "播放线程收尾 ok\n回答完毕, 30s 内免唤醒\n"
        self.assertFalse(startup_settled(log, 50, 3, 30))
        self.assertTrue(startup_settled(log, 85, 33, 30))

    def test_silent_turn_does_not_extend_real_window(self):
        log = "回答完毕\naudio submitted: 100\n服务端静默回执(噪声): 收轮\n"
        self.assertEqual(window_events(log), 1)
        self.assertTrue(startup_settled(log, 90, 40, 30))
        self.assertFalse(startup_settled(log + "audio submitted: 200", 90, 40, 30))

    def test_idle_sample_does_not_override_pending_asr_or_capture(self):
        idle = "[diag] mic n=1600 in_utt=0\n"
        self.assertTrue(capture_idle(idle))
        self.assertFalse(capture_idle(idle + "检测到说话"))
        self.assertFalse(capture_idle(idle + "audio submitted: 100\n" + idle))
        self.assertTrue(capture_idle(idle + "audio submitted: 100\n回执(噪声)\n" + idle))
        self.assertFalse(capture_idle("[diag] mic in_utt=1"))


if __name__ == "__main__":
    unittest.main()

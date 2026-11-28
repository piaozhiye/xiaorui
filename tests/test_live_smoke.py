import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "live_smoke", Path(__file__).resolve().parents[1] / "tools/run-live-smoke.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def test_unicode_log(self):
        with patch.object(smoke, "offset", return_value=100), patch.object(
                smoke, "board", return_value=r"[xr] ASR 回执: \u73b0\u5728几点了"):
            self.assertEqual(smoke.segment(0), "[xr] ASR 回执: 现在几点了")

    def test_log_truncation(self):
        with patch.object(smoke, "offset", return_value=10):
            with self.assertRaises(RuntimeError):
                smoke.segment(20)

    def test_acoustic_wake_requires_local_kws_and_command_asr(self):
        evidence = smoke.acoustic_wake_evidence(
            "[xr] KWS 命中: 小瑞\n[xr] ASR 回执: 现在几点了",
            "识别文本: 现在几点了？\n语音生成成功: 现在是九点")
        self.assertEqual(evidence, {"kws_hit": "小瑞", "recognized_command": "现在几点了",
                                    "gateway_asr": "现在几点了？", "tts": "现在是九点"})
        with self.assertRaisesRegex(RuntimeError, "missing local KWS"):
            smoke.acoustic_wake_evidence("[xr] ASR 回执: 现在几点了",
                                         "识别文本: 现在几点了？\n语音生成成功: 九点")
        with self.assertRaisesRegex(RuntimeError, "missing local KWS"):
            smoke.acoustic_wake_evidence("[xr] KWS 命中: 小瑞",
                                         "识别文本: 现在几点了？\n语音生成成功: 九点")
        with self.assertRaisesRegex(RuntimeError, "TTS evidence"):
            smoke.acoustic_wake_evidence("[xr] KWS 命中: 小瑞\n[xr] ASR 回执: 现在几点了",
                                         "识别文本: 现在几点了？")

    def test_pipewire_player_uses_selected_sink(self):
        with patch.object(smoke.subprocess, "run") as run:
            smoke.play("plughw:1,0", "wake2_time.wav", backend="pipewire", target="42")
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["pw-play", "--target", "42"])
        self.assertTrue(args[-1].endswith("tests/audio/wake2_time.wav"))


if __name__ == "__main__":
    unittest.main()

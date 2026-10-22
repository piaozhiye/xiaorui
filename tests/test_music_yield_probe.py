"""Probe scoring must fail on missing/incorrect speech or missing resumed audio."""
import importlib.util
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "music_yield_probe", Path(__file__).resolve().parents[1] / "tools/probe_music_yield.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(score, state, **kwargs):
    score.observe(json.dumps(dict(type="tts", state=state, **kwargs)))


class ProbeTests(unittest.TestCase):
    def begin(self):
        score = module.YieldScore("现在")
        event(score, "sentence_start", media="music")
        score.observe(b"music")
        score.asked = True
        return score

    def answer(self, score, text="现在十二点", audio=True):
        event(score, "sentence_start", text=text)
        if audio:
            score.observe(b"speech")
        event(score, "stop")

    def test_ordered_real_audio_passes(self):
        score = self.begin()
        self.answer(score)
        event(score, "sentence_start", media="music")
        score.observe(b"resumed music")
        self.assertTrue(score.ok)

    def test_answer_segment_can_continue_same_turn(self):
        score = self.begin()
        event(score, "sentence_start", text="现在十二点")
        score.observe(b"speech")
        event(score, "sentence_end", continuation="music")
        event(score, "sentence_start", media="music")
        score.observe(b"music")
        self.assertTrue(score.ok)

    def test_markers_without_resumed_packets_fail(self):
        score = self.begin()
        self.answer(score)
        event(score, "sentence_start", media="music")
        self.assertFalse(score.ok)

    def test_continuous_music_is_not_an_answer(self):
        score = self.begin()
        score.observe(b"more music")
        self.assertFalse(score.ok)

    def test_wrong_or_silent_answer_fails(self):
        for text, audio in [("欢迎问候", True), ("现在十二点", False)]:
            score = self.begin()
            self.answer(score, text, audio)
            event(score, "sentence_start", media="music")
            score.observe(b"music")
            self.assertFalse(score.ok)

    def test_resume_before_answer_stop_does_not_pass(self):
        score = self.begin()
        event(score, "sentence_start", text="现在十二点")
        score.observe(b"speech")
        event(score, "sentence_start", media="music")
        score.observe(b"music")
        event(score, "stop")  # A music stop cannot stand in for the answer's stop.
        event(score, "sentence_start", media="music")
        score.observe(b"music again")
        self.assertFalse(score.ok)

    def run_cli(self, resumed_audio):
        def msg(**kwargs):
            return json.dumps(kwargs)
        messages = [msg(type="hello"), msg(type="tts", state="stop"),
            msg(type="tts", state="sentence_start", media="music"), b"music",
            msg(type="tts", state="sentence_start", text="现在十二点"), b"answer",
            msg(type="tts", state="sentence_end", continuation="music"),
            msg(type="tts", state="sentence_start", media="music")]
        if resumed_audio:
            messages.append(b"resumed")
        class Socket:
            def __enter__(self):
                self.messages = iter(messages)
                return self
            def __exit__(self, *args):
                pass
            def send(self, data):
                pass
            def recv(self, timeout):
                try:
                    return next(self.messages)
                except StopIteration:
                    raise TimeoutError
        fake_client = SimpleNamespace(connect=lambda *a, **kw: Socket())
        with patch.dict("sys.modules", {"websockets.sync.client": fake_client}), \
             patch("sys.argv", ["probe", "--ask-after", "1", "--seconds", "0.05"]), \
             patch.object(module.time, "sleep"), redirect_stdout(io.StringIO()):
            return module.main()

    def test_cli_passes_only_after_resumed_packets(self):
        self.assertEqual(self.run_cli(True), 0)

    def test_cli_missing_resumed_audio_exits_nonzero(self):
        self.assertEqual(self.run_cli(False), 2)


if __name__ == "__main__":
    unittest.main()

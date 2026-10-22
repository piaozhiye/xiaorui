"""Guard a hot-loop regression: rejecting noise must not stop microphone reads."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CaptureCadenceTests(unittest.TestCase):
    def test_local_asr_off_covers_main_and_playback_paths(self):
        source = (ROOT / "board/xiaorui_live.c").read_text()
        self.assertIn("xr_speech_route(config->local_asr, be_wants_audio(be)", source)
        self.assertIn("config->music_controls && config->local_asr", source)
        self.assertIn(".transcribe = config->local_asr ? transcribe_followup : NULL", source)
        before = source.index("xr_runtime_config_validate(config")
        self.assertLess(before, source.index("fifo_open_nb(dev + 5)"))
        self.assertLess(before, source.index("xr_speech_init_config(NULL"))

    def test_no_audio_exit_paths_use_silent_session_end(self):
        source = (ROOT / "board/xiaorui_live.c").read_text()
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        timeout = source.split("if (ev == EV_TIMEOUT) {", 1)[1].split("if (ev == EV_SILENT)", 1)[0]
        noise = source.split("if (ev == EV_SILENT) {", 1)[1].split("if (ev == EV_TTS_STOP)", 1)[0]
        for path in (timeout, noise):
            self.assertIn("xr_session_end_turn_silent(&ses", path)
            self.assertNotIn("xr_session_end_turn(&ses", path)
        stop = source.split("if (ev == EV_TTS_STOP) {", 1)[1].split("else if (ev == EV_STT)", 1)[0]
        self.assertRegex(stop, r"int answered = ses\.pkt_n > 0;")
        self.assertRegex(stop, r"if \(answered\)\s+xr_session_end_turn\([^;]+;\s+else\s+xr_session_end_turn_silent\(")
        self.assertIn("xr_session_awake(&ses", noise)  # pure wake still opens a window

    def test_short_vad_rejection_does_not_sleep(self):
        source = (ROOT / "board/xiaorui_live.c").read_text()
        self.assertIn("if (vr == XR_VAD_TOO_SHORT) {", source)
        begin = source.index("if (vr == XR_VAD_TOO_SHORT) {")
        end = source.index("if (vr != XR_VAD_UTT_DONE)", begin)
        code = re.sub(r"/\*.*?\*/", "", source[begin:end], flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertIn("continue;", code)
        self.assertIsNone(re.search(r"\b(?:sleep|usleep|nanosleep|clock_nanosleep)\s*\(", code))


if __name__ == "__main__":
    unittest.main()

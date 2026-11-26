#!/usr/bin/env python3
"""Narrow parsing tests for voice commands that control multiroom music."""
import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.musicControlIntent import match_music_control  # noqa: E402
from core.handle.intentHandler import _local_intents  # noqa: E402
from core.utils.snapcast_music import control, enabled  # noqa: E402


class MusicControlIntentTests(unittest.TestCase):
    def test_transport_controls_require_music_context(self):
        self.assertEqual(match_music_control("暂停音乐"), {"action": "pause"})
        self.assertEqual(match_music_control("继续播放音乐"), {"action": "resume"})
        self.assertEqual(match_music_control("停止播放歌曲"), {"action": "stop"})
        self.assertEqual(match_music_control("播放上一首歌"), {"action": "previous"})
        self.assertEqual(match_music_control("下一首歌"), {"action": "next"})
        self.assertIsNone(match_music_control("暂停"))
        self.assertIsNone(match_music_control("继续"))

    def test_short_controls_are_scoped_to_an_active_music_session(self):
        self.assertEqual(match_music_control("暂停", music_active=True), {"action": "pause"})
        self.assertEqual(match_music_control("继续播放", music_active=True), {"action": "resume"})
        self.assertEqual(match_music_control("停止播放", music_active=True), {"action": "stop"})
        self.assertEqual(match_music_control("下一首", music_active=True), {"action": "next"})
        self.assertEqual(match_music_control("上一首", music_active=True), {"action": "previous"})
        self.assertEqual(match_music_control("声音小一点", music_active=True),
                         {"action": "volume_down"})
        self.assertEqual(match_music_control("声音调到百分之三十", music_active=True),
                         {"action": "volume_set", "volume": 30})
        self.assertIsNone(match_music_control("暂停", music_active=False))
        self.assertIsNone(match_music_control("声音小一点", music_active=False))
        self.assertIsNone(match_music_control("暂停灯光", music_active=True))

    def test_intent_matcher_queries_active_queue_only_for_short_commands(self):
        conn = SimpleNamespace(config={"plugins": {"snapcast_music": {"enabled": True}}})
        matcher = next(row[2] for row in _local_intents(conn) if row[0] == "music-control")
        with patch("core.utils.snapcast_music.music_active", return_value=True) as active:
            self.assertEqual(matcher("暂停"), {"action": "pause"})
            active.assert_called_once_with(conn)
        with patch("core.utils.snapcast_music.music_active", return_value=False):
            self.assertIsNone(matcher("暂停"))
        with patch("core.utils.snapcast_music.music_active") as active:
            self.assertEqual(matcher("暂停音乐"), {"action": "pause"})
            active.assert_not_called()
            self.assertIsNone(matcher("明天暂停路灯计划"))
            active.assert_not_called()

    def test_music_volume_commands(self):
        self.assertEqual(match_music_control("把音乐音量调到30"),
                         {"action": "volume_set", "volume": 30})
        self.assertEqual(match_music_control("音乐声音小一点"), {"action": "volume_down"})
        self.assertEqual(match_music_control("音乐声音大一点"), {"action": "volume_up"})
        self.assertEqual(match_music_control("把音乐调小声"), {"action": "volume_down"})
        self.assertEqual(match_music_control("把音乐音量调到200"),
                         {"action": "invalid_volume"})
        names = [name for name, *_ in _local_intents()]
        self.assertLess(names.index("music-control"), names.index("volume"))

    def test_status_queries(self):
        self.assertEqual(match_music_control("现在放的什么歌"), {"action": "status"})
        self.assertIsNone(match_music_control("介绍一下播放器"))

    def test_voice_client_uses_only_loopback_controller(self):
        conn = SimpleNamespace(config={"plugins": {"snapcast_music": {
            "enabled": True, "base_url": "http://192.168.1.40:8765"}}})
        self.assertTrue(enabled(conn))
        with patch("core.utils.snapcast_music.urllib.request.urlopen") as urlopen:
            result = control(conn, "pause")
        self.assertFalse(result["ok"])
        urlopen.assert_not_called()

    def test_voice_client_posts_whitelisted_action_to_local_controller(self):
        conn = SimpleNamespace(config={"plugins": {"snapcast_music": {
            "enabled": True, "base_url": "http://127.0.0.1:8766/"}}})
        response = BytesIO('{"ok":true,"response":"音乐已暂停"}'.encode())
        with patch("core.utils.snapcast_music.urllib.request.urlopen", return_value=response) as urlopen:
            result = control(conn, "pause")
        self.assertEqual(result["response"], "音乐已暂停")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8766/api/voice")
        self.assertEqual(request.data, b'{"action": "pause"}')
        with patch("core.utils.snapcast_music.urllib.request.urlopen") as urlopen:
            result = control(conn, "delete_everything")
        self.assertFalse(result["ok"])
        urlopen.assert_not_called()

    def test_music_active_uses_status_state_not_response_wording(self):
        conn = SimpleNamespace(config={"plugins": {"snapcast_music": {
            "enabled": True, "base_url": "http://127.0.0.1:8766"}}})
        with patch("core.utils.snapcast_music.control", return_value={
            "ok": True, "response": "sample.wav，已暂停"}) as call:
            from core.utils.snapcast_music import music_active
            self.assertTrue(music_active(conn))
            call.assert_called_once_with(conn, "status")
        with patch("core.utils.snapcast_music.control", return_value={
            "ok": True, "response": "当前没有正在播放的音乐"}):
            self.assertFalse(music_active(conn))


if __name__ == "__main__":
    unittest.main()

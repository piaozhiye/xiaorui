#!/usr/bin/env python3
"""Narrow parsing tests for voice commands that control multiroom music."""
import sys
import queue
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.handle.musicControlIntent import match_music_control  # noqa: E402
from core.handle.intentHandler import _h_music, _h_music_control, _local_intents  # noqa: E402
from core.handle.nextIntent import handle_next_intent  # noqa: E402
from core.handle.sendAudioHandle import send_tts_message  # noqa: E402
from core.utils.snapcast_music import control, enabled  # noqa: E402
from core.providers.tts.dto.dto import ContentType, SentenceType  # noqa: E402
from plugins_func.functions.control_music import control_music  # noqa: E402
from plugins_func.functions.play_music import play_music  # noqa: E402
from plugins_func.register import Action  # noqa: E402


class MusicControlIntentTests(unittest.TestCase):
    def test_transport_controls_require_music_context(self):
        self.assertEqual(match_music_control("暂停音乐"), {"action": "pause"})
        self.assertEqual(match_music_control("停一下音乐"), {"action": "pause"})
        self.assertEqual(match_music_control("停下音乐"), {"action": "stop"})
        self.assertEqual(match_music_control("停播这首歌"), {"action": "stop"})
        self.assertEqual(match_music_control("继续播放音乐"), {"action": "resume"})
        self.assertEqual(match_music_control("停止播放歌曲"), {"action": "stop"})
        self.assertEqual(match_music_control("播放上一首歌"), {"action": "previous"})
        self.assertEqual(match_music_control("下一首歌"), {"action": "next"})
        self.assertIsNone(match_music_control("暂停"))
        self.assertIsNone(match_music_control("继续"))

    def test_short_controls_are_scoped_to_an_active_music_session(self):
        self.assertEqual(match_music_control("暂停", music_active=True), {"action": "pause"})
        self.assertEqual(match_music_control("停一下", music_active=True), {"action": "pause"})
        self.assertEqual(match_music_control("暂停一下", music_active=True), {"action": "pause"})
        self.assertEqual(match_music_control("停下", music_active=True), {"action": "stop"})
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
        with patch("core.utils.snapcast_music.music_active", return_value=None):
            self.assertEqual(matcher("停止播放"), {"action": "unavailable"})
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
            "enabled": True, "base_url": "http://192.0.2.40:8765"}}})
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

    def test_music_active_is_unknown_when_controller_status_fails(self):
        conn = SimpleNamespace(config={"plugins": {"snapcast_music": {
            "enabled": True, "base_url": "http://127.0.0.1:8766"}}})
        with patch("core.utils.snapcast_music.control", return_value={
                "ok": False, "error": "音乐服务暂时不可用"}):
            from core.utils.snapcast_music import music_active
            self.assertIsNone(music_active(conn))


class MusicControlReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_stop_tts_state_is_still_sent(self):
        conn = SimpleNamespace(
            sentence_id="sentence", session_id="session", config={},
            websocket=SimpleNamespace(send=AsyncMock()),
        )
        await send_tts_message(conn, "sentence_start", "测试")
        conn.websocket.send.assert_awaited_once()

    async def test_successful_controls_end_turn_without_tts_speech(self):
        actions = ("play", "pause", "resume", "stop", "next", "previous",
                   "volume_set", "volume_up", "volume_down")
        for action in actions:
            with self.subTest(action=action):
                conn = SimpleNamespace(
                    config={}, sentence_id=None,
                    tts=SimpleNamespace(tts_text_queue=queue.Queue()),
                )
                with patch("core.handle.sendAudioHandle.send_stt_message",
                           new_callable=AsyncMock), \
                     patch("core.utils.snapcast_music.enabled", return_value=True), \
                     patch("core.utils.snapcast_music.control_async", new_callable=AsyncMock,
                           return_value={"ok": True, "response": "音乐已暂停"}), \
                     patch("core.handle.intentHandler.speak_txt") as speak:
                    handled = await _h_music_control(conn, "控制音乐", {"action": action})

                self.assertTrue(handled)
                speak.assert_not_called()
                messages = [conn.tts.tts_text_queue.get_nowait()]
                self.assertEqual([m.sentence_type for m in messages], [SentenceType.LAST])
                self.assertTrue(all(m.content_type == ContentType.ACTION for m in messages))

    async def test_failed_control_still_speaks_error(self):
        conn = SimpleNamespace(config={}, sentence_id=None)
        with patch("core.handle.sendAudioHandle.send_stt_message", new_callable=AsyncMock), \
             patch("core.utils.snapcast_music.enabled", return_value=True), \
             patch("core.utils.snapcast_music.control_async", new_callable=AsyncMock,
                   return_value={"ok": False, "error": "音乐服务暂时不可用"}), \
             patch("core.handle.intentHandler.speak_txt") as speak:
            handled = await _h_music_control(conn, "暂停音乐", {"action": "pause"})

        self.assertTrue(handled)
        speak.assert_called_once_with(conn, "音乐服务暂时不可用")

    async def test_snapcast_play_intent_ends_without_success_speech(self):
        conn = SimpleNamespace(
            config={}, sentence_id=None,
            tts=SimpleNamespace(tts_text_queue=queue.Queue()),
        )
        with patch("core.handle.sendAudioHandle.send_stt_message", new_callable=AsyncMock), \
             patch("core.utils.snapcast_music.enabled", return_value=True), \
             patch("core.utils.snapcast_music.control_async", new_callable=AsyncMock,
                   return_value={"ok": True, "response": "正在播放歌曲"}), \
             patch("core.handle.intentHandler.speak_txt") as speak:
            handled = await _h_music(conn, "播放歌曲", "歌曲")

        self.assertTrue(handled)
        speak.assert_not_called()
        messages = [conn.tts.tts_text_queue.get_nowait()]
        self.assertEqual([m.sentence_type for m in messages], [SentenceType.LAST])
        self.assertTrue(all(m.content_type == ContentType.ACTION for m in messages))

    async def test_next_intent_routes_snapcast_next_through_silent_control_handler(self):
        conn = SimpleNamespace()
        with patch("core.utils.snapcast_music.enabled", return_value=True), \
             patch("core.handle.intentHandler._h_music_control", new_callable=AsyncMock,
                   return_value=True) as control_handler:
            handled = await handle_next_intent(conn, "下一首")

        self.assertTrue(handled)
        control_handler.assert_awaited_once_with(conn, "下一首", {"action": "next"})

    async def test_music_control_tool_records_success_without_tts_response(self):
        conn = SimpleNamespace(config={})
        with patch("plugins_func.functions.control_music.enabled", return_value=True), \
             patch("plugins_func.functions.control_music.control_async", new_callable=AsyncMock,
                   return_value={"ok": True, "response": "音乐已暂停"}):
            result = await control_music(conn, "pause")

        self.assertEqual(result.action, Action.RECORD)
        self.assertIsNone(result.response)

    async def test_play_music_tool_records_success_without_tts_response(self):
        conn = SimpleNamespace(config={})
        with patch("core.utils.snapcast_music.enabled", return_value=True), \
             patch("core.utils.snapcast_music.control_async", new_callable=AsyncMock,
                   return_value={"ok": True, "response": "正在播放歌曲"}):
            result = await play_music(conn, "歌曲")

        self.assertEqual(result.action, Action.RECORD)
        self.assertIsNone(result.response)

    async def test_silent_music_turn_suppresses_configured_stop_audio(self):
        conn = SimpleNamespace(
            sentence_id="music-turn", session_id="session",
            _silent_music_turn_sid="music-turn",
            config={"enable_stop_tts_notify": True},
            audio_rate_controller=None, clearSpeakStatus=lambda: None,
            client_abort=False,
            websocket=SimpleNamespace(send=AsyncMock()),
            session_coordinator=None,
        )
        with patch("core.handle.sendAudioHandle._wait_for_audio_completion",
                   new_callable=AsyncMock), \
             patch("core.handle.sendAudioHandle.audio_to_data",
                   new_callable=AsyncMock) as audio_to_data, \
             patch("core.ha_audit.completed"):
            await send_tts_message(conn, "stop", None)

        audio_to_data.assert_not_awaited()
        self.assertIsNone(conn._silent_music_turn_sid)
        conn.websocket.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

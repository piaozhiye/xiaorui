#!/usr/bin/env python3
"""Behavior tests for routing voice playback commands to Music Assistant."""
import json
import sys
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board-server" / "pkg"))

from core.utils.music_assistant import control  # noqa: E402
from core.utils.snapcast_music import control as route_control, control_async  # noqa: E402


class FakeMAWebSocket:
    def __init__(self, *, many_players=False):
        self.sent = []
        self.responses = []
        self.many_players = many_players

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send(self, raw):
        command = json.loads(raw)
        self.sent.append(command)
        name = command["command"]
        args = command.get("args", {})
        if name == "auth/login":
            result = {"access_token": "ephemeral-test-token"}
        elif name == "players/all":
            result = [{"player_id": "snap-1", "provider": "snapcast", "name": "小瑞", "available": True}]
            if self.many_players:
                result.append({"player_id": "snap-2", "provider": "snapcast", "name": "客厅", "available": True})
        elif name == "player_queues/all":
            result = [{"queue_id": "snap-1", "player_id": "snap-1", "state": "playing",
                       "current_item": {"name": "晴天"}}]
        elif name == "providers":
            result = [{"instance_id": "qqmusic--account", "domain": "qqmusic", "type": "music", "available": True}]
        elif name == "music/search":
            result = {
                "artists": [], "albums": [], "genres": [],
                "tracks": [
                    {"media_type": "track", "name": "晴天", "uri": "qqmusic://track/1"},
                    {"media_type": "track", "name": "晴天 (现场版)", "uri": "qqmusic://track/2"},
                ],
                "playlists": [], "radio": [], "audiobooks": [],
                "podcasts": [], "sound_effects": [],
            }
        elif name == "music/tracks/library_items":
            result = [{"media_type": "track", "name": "随机曲目", "uri": "qqmusic://track/random"}]
        else:
            result = None
        self.responses.append(json.dumps({"message_id": command["message_id"], "result": result}))

    def recv(self, timeout=None):
        if not self.responses:
            raise TimeoutError("no response")
        return self.responses.pop(0)


class MusicAssistantVoiceTests(unittest.TestCase):
    def setUp(self):
        self.conn = SimpleNamespace(config={"plugins": {"music_assistant": {
            "enabled": True, "base_url": "http://ma.local:8095", "player_name": "小瑞"}}})
        self.env = patch.dict("os.environ", {
            "XR_MA_URL": "", "XR_MA_USERNAME": "voice-user", "XR_MA_PASSWORD": "secret",
            "XR_MA_ACCESS_TOKEN": "", "XR_MA_PLAYER_ID": "", "XR_MA_PLAYER_NAME": "",
            "XR_MA_PROVIDER_DOMAIN": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ws = FakeMAWebSocket()
        self.connect = patch("core.utils.music_assistant.websocket_connect", return_value=self.ws)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def commands(self):
        return [item["command"] for item in self.ws.sent]

    def test_named_song_searches_exact_title_and_replaces_ma_queue(self):
        result = control(self.conn, "play", query="晴天")
        self.assertEqual(result, {"ok": True, "response": "正在播放晴天"})
        search = next(c for c in self.ws.sent if c["command"] == "music/search")
        play = next(c for c in self.ws.sent if c["command"] == "player_queues/play_media")
        self.assertEqual(search["args"]["search_query"], "晴天")
        self.assertEqual(play["args"]["queue_id"], "snap-1")
        self.assertEqual(play["args"]["option"], "replace")
        self.assertEqual(play["args"]["media"]["uri"], "qqmusic://track/1")

    def test_named_song_accepts_flat_search_results_from_older_api(self):
        self.ws.responses.clear()
        original_send = self.ws.send

        def send_with_flat_search(raw):
            command = json.loads(raw)
            if command["command"] == "music/search":
                self.ws.sent.append(command)
                self.ws.responses.append(json.dumps({
                    "message_id": command["message_id"],
                    "result": [{"media_type": "track", "name": "晴天",
                                "uri": "qqmusic://track/legacy"}],
                }))
                return
            original_send(raw)

        self.ws.send = send_with_flat_search
        result = control(self.conn, "play", query="晴天")
        self.assertTrue(result["ok"])
        play = next(c for c in self.ws.sent if c["command"] == "player_queues/play_media")
        self.assertEqual(play["args"]["media"]["uri"], "qqmusic://track/legacy")

    def test_explicit_qq_source_limits_search_to_qq_provider(self):
        result = control(self.conn, "play", query="QQ音乐 晴天")
        self.assertTrue(result["ok"])
        search = next(c for c in self.ws.sent if c["command"] == "music/search")
        self.assertEqual(search["args"]["search_query"], "晴天")
        self.assertEqual(search["args"]["providers"], ["qqmusic--account"])

    def test_play_without_title_uses_ma_library(self):
        result = control(self.conn, "play", query="")
        self.assertEqual(result["response"], "正在播放随机曲目")
        library = next(c for c in self.ws.sent
                       if c["command"] == "music/tracks/library_items")
        self.assertEqual(library["args"]["limit"], 100)

    def test_transport_and_volume_commands_target_the_selected_player(self):
        paused = control(self.conn, "pause")
        self.assertEqual(paused["response"], "好，暂停了")
        pause = next(c for c in self.ws.sent if c["command"] == "player_queues/pause")
        self.assertEqual(pause["args"], {"queue_id": "snap-1"})
        volume = control(self.conn, "volume_set", volume=34)
        self.assertEqual(volume["response"], "音量调到34%了")
        set_volume = next(c for c in self.ws.sent if c["command"] == "players/cmd/volume_set")
        self.assertEqual(set_volume["args"], {"player_id": "snap-1", "volume_level": 34})

    def test_transport_and_relative_volume_confirmations_are_short_and_specific(self):
        expected = {
            "resume": "好，继续播放",
            "stop": "好，音乐停了",
            "next": "好，切到下一首了",
            "previous": "好，切到上一首了",
            "volume_up": "音量调大了",
            "volume_down": "音量调小了",
        }
        for action, response in expected.items():
            with self.subTest(action=action):
                result = control(self.conn, action)
                self.assertTrue(result["ok"])
                self.assertEqual(result["response"], response)

    def test_async_control_runs_ma_request_off_the_calling_loop(self):
        result = asyncio.run(control_async(self.conn, "next"))
        self.assertEqual(result["response"], "好，切到下一首了")
        self.assertIn("player_queues/next", self.commands())

    def test_status_marks_a_current_track_active(self):
        result = control(self.conn, "status")
        self.assertTrue(result["active"])
        self.assertEqual(result["state"], "playing")

    def test_route_prefers_ma_only_when_enabled(self):
        with patch("core.utils.music_assistant.control", return_value={"ok": True}) as ma:
            result = route_control(self.conn, "next")
        self.assertEqual(result, {"ok": True})
        ma.assert_called_once()

    def test_missing_credentials_fails_without_connecting(self):
        with patch.dict("os.environ", {"XR_MA_USERNAME": "", "XR_MA_PASSWORD": ""}):
            self.connect.stop()
            with patch("core.utils.music_assistant.websocket_connect") as connect:
                result = control(self.conn, "pause")
        self.assertFalse(result["ok"])
        self.assertIn("登录信息", result["error"])
        connect.assert_not_called()

    def test_invalid_url_is_rejected(self):
        self.conn.config["plugins"]["music_assistant"]["base_url"] = "http://user:pass@ma.local:8095"
        result = control(self.conn, "pause")
        self.assertFalse(result["ok"])
        self.assertIn("地址无效", result["error"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Snapcast 控制适配器 + MusicSession 单测。

用假的 transport(不连真 server)覆盖:
  - Server.GetStatus -> 玩家列表解析
  - Client.SetVolume / Client.SetLatency / Client.SetName 的参数
  - Group.SetClients + Group.SetStream 的分派
  - JSON-RPC 错误与服务不可达时的异常
  - MusicSession 的 players.list / session.play / stop / volume
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "snapcast_center"))

from music_session import MusicSession  # noqa: E402
from snapcast_control import SnapcastClient, SnapcastError  # noqa: E402


STATUS = {
    # v0.35 的真实结构: result = {"server": {"groups": [...], "server": {...}, "streams": [...]}}
    "server": {
        "groups": [
            {
                "id": "grp-1",
                "name": "",
                "stream_id": "demo",
                "muted": False,
                "clients": [
                    {
                        "id": "aa:bb:cc:dd:ee:01",
                        "connected": True,
                        "host": {"name": "rk3562-a", "ip": "198.51.100.10", "arch": "aarch64", "os": "Linux"},
                        "config": {"name": "rk3562-a", "latency": 0, "instance": 1,
                                   "volume": {"muted": False, "percent": 100}},
                        "snapclient": {"name": "Snapclient", "version": "0.35.0", "protocolVersion": 2},
                    },
                    {
                        "id": "aa:bb:cc:dd:ee:ff",
                        "connected": False,
                        "host": {"name": "rk3562-b", "ip": "198.51.100.11", "arch": "aarch64", "os": "Linux"},
                        "config": {"name": "", "latency": 10, "instance": 1,
                                   "volume": {"muted": True, "percent": 40}},
                        "snapclient": {"name": "Snapclient", "version": "0.35.0", "protocolVersion": 2},
                    },
                ],
            }
        ],
        "server": {
            "host": {"name": "pc", "ip": "192.0.2.10", "arch": "x86_64", "os": "Linux"},
            "snapserver": {"name": "Snapserver", "version": "0.35.0", "protocolVersion": 1},
        },
        "streams": [
            {"id": "demo", "status": "playing",
             "uri": {"raw": "file:///tmp/demo.pcm?name=demo", "scheme": "file",
                     "query": {"codec": "pcm", "name": "demo", "sampleformat": "24000:16:1"}}},
        ],
    }
}

# 老版本/推送通知的结构(groups 在顶层), 适配器要认
STATUS_LEGACY = {
    "groups": STATUS["server"]["groups"],
    "streams": STATUS["server"]["streams"],
}


class FakeTransport:
    """假的 JSON-RPC transport: 记录调用, 按 method 返回预设结果。"""

    def __init__(self, results=None, error=None):
        self.calls = []
        self.results = results or {}
        self.error = error

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        if method in self.results:
            value = self.results[method]
            if isinstance(value, Exception):
                raise value
            return value
        if method == "Server.GetStatus":
            return STATUS
        return "ok"


class SnapcastClientTests(unittest.TestCase):
    def test_players_list_parses_groups(self):
        client = SnapcastClient(transport=FakeTransport())
        players = client.players()
        self.assertEqual(len(players), 2)
        first = players[0]
        self.assertEqual(first["id"], "aa:bb:cc:dd:ee:01")
        self.assertEqual(first["name"], "rk3562-a")
        self.assertEqual(first["host"], "198.51.100.10")
        self.assertTrue(first["connected"])
        self.assertEqual(first["volume"], 100)
        self.assertFalse(first["muted"])
        self.assertEqual(first["group_id"], "grp-1")
        self.assertEqual(first["stream_id"], "demo")
        self.assertEqual(first["group_clients"], ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:ff"])
        # 离线的那台也要列出来, 但 connected=False
        self.assertFalse(players[1]["connected"])
        self.assertTrue(players[1]["muted"])

    def test_players_list_uses_name_fallback(self):
        # 没配名字时用 host.name, 再不行用 id
        status = {"server": {"groups": [{"id": "g", "stream_id": "s", "clients": [
            {"id": "x1", "connected": True,
             "host": {"name": "laptop", "ip": "10.0.0.2"},
             "config": {"volume": {"muted": False, "percent": 80}}},
            {"id": "x2", "connected": True, "config": {}},
        ]}]}}
        client = SnapcastClient(transport=FakeTransport({"Server.GetStatus": status}))
        players = client.players()
        self.assertEqual(players[0]["name"], "laptop")
        self.assertEqual(players[1]["name"], "x2")
        self.assertEqual(players[1]["volume"], 100)

    def test_players_list_accepts_legacy_flat_status(self):
        # 老版本/通知的格式: groups 在顶层
        client = SnapcastClient(transport=FakeTransport({"Server.GetStatus": STATUS_LEGACY}))
        players = client.players()
        self.assertEqual(len(players), 2)
        self.assertEqual(players[0]["name"], "rk3562-a")
        self.assertEqual(len(client.streams()), 1)

    def test_set_volume_calls_client_set_volume(self):
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.set_volume("aa:bb:cc:dd:ee:01", 65)
        method, params = transport.calls[-1]
        self.assertEqual(method, "Client.SetVolume")
        self.assertEqual(params, {"id": "aa:bb:cc:dd:ee:01",
                                   "volume": {"muted": False, "percent": 65}})

    def test_set_volume_muted(self):
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.set_volume("abc", 30, muted=True)
        method, params = transport.calls[-1]
        self.assertEqual(method, "Client.SetVolume")
        self.assertTrue(params["volume"]["muted"])

    def test_set_latency_and_name(self):
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.set_latency("abc", 42)
        self.assertEqual(transport.calls[-1], ("Client.SetLatency", {"id": "abc", "latency": 42}))
        client.set_name("abc", "客厅")
        self.assertEqual(transport.calls[-1], ("Client.SetName", {"id": "abc", "name": "客厅"}))

    def test_assign_players_to_stream(self):
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.assign_players_to_stream(["p1", "p2"], "demo")
        methods = [c[0] for c in transport.calls]
        self.assertIn("Group.SetClients", methods)
        self.assertIn("Group.SetStream", methods)
        group_params = [p for m, p in transport.calls if m == "Group.SetClients"][0]
        self.assertEqual(group_params["clients"], ["p1", "p2"])
        stream_params = [p for m, p in transport.calls if m == "Group.SetStream"][0]
        self.assertEqual(stream_params["stream_id"], "demo")

    def test_group_set_mute_uses_mute_key(self):
        # 服务端 Group.SetMute 读的是 "mute", 写成 "muted" 会被 InvalidParams
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.group_set_mute("grp-1", True)
        self.assertEqual(transport.calls[-1], ("Group.SetMute", {"id": "grp-1", "mute": True}))

    def test_set_volume_keeps_current_mute(self):
        # 改音量不该顺手取消静音
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.set_volume("aa:bb:cc:dd:ee:ff", 70)   # 夹具里这台是 muted=True
        method, params = transport.calls[-1]
        self.assertEqual(method, "Client.SetVolume")
        self.assertTrue(params["volume"]["muted"])
        self.assertEqual(params["volume"]["percent"], 70)

    def test_players_list_reports_group_mute(self):
        status = {"server": {"groups": [{"id": "g", "stream_id": "s", "muted": True, "clients": [
            {"id": "x1", "connected": True, "config": {"volume": {"muted": False, "percent": 50}}},
        ]}]}}
        client = SnapcastClient(transport=FakeTransport({"Server.GetStatus": status}))
        p = client.players()[0]
        self.assertTrue(p["muted"])        # 生效静音 = group 静音
        self.assertFalse(p["client_muted"])
        self.assertTrue(p["group_muted"])

    def test_players_list_tolerates_null_fields(self):
        status = {"server": {"groups": [{"id": "g", "stream_id": "s", "muted": False, "clients": [
            {"id": "x1", "connected": True, "config": {"volume": None, "latency": None}},
        ]}]}}
        client = SnapcastClient(transport=FakeTransport({"Server.GetStatus": status}))
        p = client.players()[0]
        self.assertEqual(p["volume"], 100)
        self.assertEqual(p["latency"], 0)

    def test_stream_control_uses_params_object(self):
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.stream_control("demo", "seek", {"offset": 60000})
        self.assertEqual(transport.calls[-1],
                         ("Stream.Control", {"id": "demo", "command": "seek",
                                             "params": {"offset": 60000}}))

    def test_set_latency_range_check(self):
        client = SnapcastClient(transport=FakeTransport())
        with self.assertRaises(SnapcastError):
            client.set_latency("x", 999999)

    def test_add_and_remove_stream(self):
        transport = FakeTransport()
        client = SnapcastClient(transport=transport)
        client.add_stream("file:///tmp/x.pcm?name=x")
        self.assertEqual(transport.calls[-1][0], "Stream.AddStream")
        client.remove_stream("x")
        self.assertEqual(transport.calls[-1], ("Stream.RemoveStream", {"id": "x"}))

    def test_remove_stream_treats_lost_reply_as_success_only_when_absent(self):
        transport = FakeTransport({"Stream.RemoveStream": SnapcastError("connection closed")})
        client = SnapcastClient(transport=transport)
        result = client.remove_stream("already-gone")
        self.assertEqual(result, {"id": "already-gone", "removed": True})
        transport.results["Server.GetStatus"] = STATUS
        with self.assertRaises(SnapcastError):
            client.remove_stream("demo")

    def test_rpc_error_raises(self):
        transport = FakeTransport({"Client.SetVolume": SnapcastError("no such client")})
        client = SnapcastClient(transport=transport)
        with self.assertRaises(SnapcastError):
            client.set_volume("nope", 10)

    def test_transport_error_raises(self):
        client = SnapcastClient(transport=FakeTransport(error=OSError("connection refused")))
        with self.assertRaises(SnapcastError):
            client.players()


class MusicSessionTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.session = MusicSession(SnapcastClient(transport=self.transport))

    def test_players_list_op(self):
        result = self.session.handle({"op": "players.list"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["players"]), 2)
        self.assertEqual(result["players"][0]["name"], "rk3562-a")

    def test_unknown_op_is_error(self):
        result = self.session.handle({"op": "nope"})
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_session_play_assigns_selected_players(self):
        result = self.session.handle({
            "op": "session.play",
            "source": "file:///tmp/other.pcm?name=other",
            "players": ["aa:bb:cc:dd:ee:01"],
        })
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["session_id"])
        methods = [c[0] for c in self.transport.calls]
        self.assertIn("Stream.AddStream", methods)
        self.assertIn("Group.SetClients", methods)
        self.assertIn("Group.SetStream", methods)

    def test_session_play_reuses_existing_stream(self):
        # server 上已经有同名的流就别重复加, 直接复用
        result = self.session.handle({
            "op": "session.play",
            "source": "file:///tmp/demo.pcm?name=demo",
            "players": ["aa:bb:cc:dd:ee:01"],
        })
        self.assertTrue(result["ok"], result)
        methods = [c[0] for c in self.transport.calls]
        self.assertNotIn("Stream.AddStream", methods)
        self.assertIn("Group.SetStream", methods)

    def test_new_stream_replaces_overlapping_session_without_stale_reconcile(self):
        session = MusicSession(SnapcastClient(transport=self.transport),
                              session_id_factory=iter(("old", "new")).__next__)
        first = session.handle({"op": "session.play", "source": "pipe:///old?name=old-stream",
                                "players": ["aa:bb:cc:dd:ee:01"]})
        second = session.handle({"op": "session.play", "source": "pipe:///new?name=new-stream",
                                 "players": ["aa:bb:cc:dd:ee:01"]})
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        sessions = session.handle({"op": "sessions.list"})["sessions"]
        self.assertEqual([s["session_id"] for s in sessions], ["new"])
        self.assertIn(("Stream.RemoveStream", {"id": "old-stream"}), self.transport.calls)

    def test_switching_one_player_keeps_remaining_players_in_old_session(self):
        session = MusicSession(SnapcastClient(transport=self.transport),
                              session_id_factory=iter(("old", "new")).__next__)
        session.handle({"op": "session.play", "source": "pipe:///old?name=old-stream",
                        "players": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:ff"]})
        second = session.handle({"op": "session.play", "source": "pipe:///new?name=new-stream",
                                 "players": ["aa:bb:cc:dd:ee:01"]})
        self.assertTrue(second["ok"], second)
        sessions = {s["session_id"]: s for s in session.handle({"op": "sessions.list"})["sessions"]}
        self.assertEqual(sessions["old"]["players"], ["aa:bb:cc:dd:ee:ff"])
        self.assertEqual(sessions["old"]["state"], "playing")

    def test_same_stream_selection_merges_into_one_session(self):
        session = MusicSession(SnapcastClient(transport=self.transport),
                              session_id_factory=iter(("shared", "unused")).__next__)
        source = "pipe:///shared?name=shared"
        first = session.handle({"op": "session.play", "source": source,
                                "players": ["aa:bb:cc:dd:ee:01"]})
        second = session.handle({"op": "session.play", "source": source,
                                 "players": ["aa:bb:cc:dd:ee:ff"]})
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(session.handle({"op": "sessions.list"})["sessions"]), 1)

    def test_session_play_requires_players(self):
        result = self.session.handle({"op": "session.play", "source": "x", "players": []})
        self.assertFalse(result["ok"])

    def test_session_play_rejects_unknown_player(self):
        result = self.session.handle({
            "op": "session.play",
            "source": "file:///tmp/demo.pcm?name=demo",
            "players": ["not-a-player"],
        })
        self.assertFalse(result["ok"])
        self.assertIn("not-a-player", result["error"])

    def test_player_volume_op(self):
        result = self.session.handle({"op": "player.volume", "player_id": "aa:bb:cc:dd:ee:01", "volume": 42})
        self.assertTrue(result["ok"])
        method, params = self.transport.calls[-1]
        self.assertEqual(method, "Client.SetVolume")
        self.assertEqual(params["volume"]["percent"], 42)
        self.assertFalse(params["volume"]["muted"])

    def test_player_volume_preserves_existing_mute_without_explicit_choice(self):
        result = self.session.handle({"op": "player.volume", "player_id": "aa:bb:cc:dd:ee:ff",
                                      "volume": 42})
        self.assertTrue(result["ok"])
        method, params = self.transport.calls[-1]
        self.assertEqual(method, "Client.SetVolume")
        self.assertTrue(params["volume"]["muted"])

    def test_player_volume_requires_boolean_mute_choice(self):
        result = self.session.handle({"op": "player.volume", "player_id": "aa:bb:cc:dd:ee:01",
                                      "volume": 42, "muted": "false"})
        self.assertFalse(result["ok"])
        self.assertIn("布尔值", result["error"])

    def test_player_volume_out_of_range(self):
        result = self.session.handle({"op": "player.volume", "player_id": "x", "volume": 300})
        self.assertFalse(result["ok"])

    def test_source_without_name_gets_name_injected(self):
        # 服务端要求 URI 里有 ?name=, 少了会被拒
        result = self.session.handle({
            "op": "session.play",
            "source": "pipe:///tmp/snapfifo",
            "players": ["aa:bb:cc:dd:ee:01"],
        })
        self.assertTrue(result["ok"], result)
        add = [p for m, p in self.transport.calls if m == "Stream.AddStream"][0]
        self.assertIn("name=", add["streamUri"])
        self.assertTrue(add["streamUri"].startswith("pipe:///tmp/snapfifo?"))

    def test_session_play_uses_server_returned_stream_id(self):
        transport = FakeTransport({"Stream.AddStream": {"id": "srv-stream-7"}})
        session = MusicSession(SnapcastClient(transport=transport),
                              session_id_factory=iter(("first", "second")).__next__)
        request = {
            "op": "session.play",
            "source": "pipe:///tmp/snapfifo?name=local",
            "players": ["aa:bb:cc:dd:ee:01"],
        }
        result = session.handle(request)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stream_id"], "srv-stream-7")
        calls_before = len([1 for method, _ in transport.calls if method == "Stream.AddStream"])
        repeated = session.handle(request)
        calls_after = len([1 for method, _ in transport.calls if method == "Stream.AddStream"])
        self.assertTrue(repeated["ok"], repeated)
        self.assertEqual(repeated["session_id"], result["session_id"])
        self.assertEqual(repeated["stream_id"], "srv-stream-7")
        self.assertEqual(calls_after, calls_before)

    def test_session_stop_reports_failure_instead_of_success(self):
        transport = FakeTransport()
        session = MusicSession(SnapcastClient(transport=transport))
        r = session.handle({"op": "session.play", "source": "pipe:///x?name=demo",
                            "players": ["aa:bb:cc:dd:ee:01"]})
        self.assertTrue(r["ok"], r)
        # 摘流失败 -> 不能报成功(否则 UI 显示停了实际还在响)
        transport.results["Stream.RemoveStream"] = SnapcastError("no such stream")
        r2 = session.handle({"op": "session.stop", "session_id": r["session_id"]})
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["state"], "unknown")

    def test_session_stop_succeeds_when_stream_removal_succeeds(self):
        transport = FakeTransport()
        session = MusicSession(SnapcastClient(transport=transport))
        started = session.handle({"op": "session.play", "source": "pipe:///x?name=s1",
                                  "players": ["aa:bb:cc:dd:ee:01"]})
        transport.results["Stream.Control"] = SnapcastError("stop unsupported")
        stopped = session.handle({"op": "session.stop", "session_id": started["session_id"]})
        self.assertTrue(stopped["ok"], stopped)
        self.assertEqual(stopped["state"], "stopped")
        self.assertIn("stop unsupported", stopped["warnings"][0])

    def test_session_stop(self):
        result = self.session.handle({"op": "session.stop", "session_id": "nope"})
        # 未知的 session_id 也要给出明确答复, 不能抛异常
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_session_state_persists_and_reconciles_after_server_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "sessions.json"
            first = MusicSession(SnapcastClient(transport=self.transport),
                                 session_id_factory=lambda: "recover-me",
                                 state_file=state_file)
            started = first.handle({"op": "session.play",
                                    "source": "pipe:///tmp/music.pcm?name=music",
                                    "players": ["aa:bb:cc:dd:ee:01"]})
            self.assertTrue(started["ok"], started)
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

            # A fresh controller process loads the desired session. Fake server
            # still has only the default stream/group, like after restart.
            self.transport.calls.clear()
            recovered = MusicSession(SnapcastClient(transport=self.transport),
                                     state_file=state_file)
            result = recovered.handle({"op": "server.reconcile"})
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["sessions"][0]["session_id"], "recover-me")
            self.assertTrue(result["sessions"][0]["restored"])
            methods = [method for method, _ in self.transport.calls]
            self.assertIn("Stream.AddStream", methods)
            self.assertIn("Group.SetClients", methods)
            self.assertIn("Group.SetStream", methods)

    def test_paused_session_stays_paused_after_reconcile(self):
        session = MusicSession(SnapcastClient(transport=self.transport))
        started = session.handle({"op": "session.play",
                                  "source": "file:///tmp/demo.pcm?name=demo",
                                  "players": ["aa:bb:cc:dd:ee:01"]})
        self.assertTrue(started["ok"], started)
        self.transport.calls.clear()
        result = session.handle({"op": "session.pause", "session_id": started["session_id"]})
        self.assertTrue(result["ok"], result)
        self.transport.calls.clear()
        result = session.handle({"op": "server.reconcile"})
        self.assertTrue(result["ok"], result)
        self.assertIn(("Stream.Control", {"id": "demo", "command": "pause"}),
                      self.transport.calls)

    def test_music_library_pipe_pause_is_owned_by_source_controller(self):
        session = MusicSession(SnapcastClient(transport=self.transport))
        started = session.handle({"op": "session.play",
                                  "source": "pipe:///tmp/demo.pcm?name=demo",
                                  "players": ["aa:bb:cc:dd:ee:01"],
                                  "track_id": "a" * 20})
        self.assertTrue(started["ok"], started)
        self.transport.calls.clear()
        paused = session.handle({"op": "session.pause", "session_id": started["session_id"]})
        self.assertTrue(paused["ok"], paused)
        self.assertFalse(any(method == "Stream.Control" for method, _ in self.transport.calls))
        self.transport.calls.clear()
        session.handle({"op": "server.reconcile"})
        self.assertFalse(any(method == "Stream.Control" for method, _ in self.transport.calls))


class HttpTransportTests(unittest.TestCase):
    """真起一个本地 http.server, 覆盖 HTTP transport(唯一的生产代码路径)。"""

    def setUp(self):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received = []
        responses = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                received.append((self.path, dict(self.headers), _json.loads(body)))
                method = received[-1][2].get("method")
                payload = responses.get(method, {"ok": True})
                out = _json.dumps({"jsonrpc": "2.0", "id": 1, "result": payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.received = received
        self.responses = responses
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_posts_jsonrpc_envelope_to_jsonrpc_path(self):
        client = SnapcastClient(host="127.0.0.1", port=self.port)
        self.responses["Server.GetStatus"] = {"server": {"groups": []}}
        client.server_status()
        path, headers, body = self.received[-1]
        self.assertEqual(path, "/jsonrpc")
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["method"], "Server.GetStatus")
        self.assertIn("application/json", headers.get("Content-Type", "").lower())

    def test_decodes_error_response(self):
        # 服务端返回 error 时要抛 SnapcastError, 而不是把 error 当 result 返回
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class ErrHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                out = _json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "error": {"code": -32601, "message": "no such method"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = HTTPServer(("127.0.0.1", 0), ErrHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            client = SnapcastClient(host="127.0.0.1", port=srv.server_address[1])
            with self.assertRaises(SnapcastError):
                client.call("Nope.Nope")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()

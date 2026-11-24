#!/usr/bin/env python3
"""HTTP/UI smoke tests for the local Snapcast music controller."""
import json
import copy
import base64
import sys
import threading
import tempfile
import subprocess
import unittest
import urllib.request
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "snapcast_center"))

from music_session import MusicSession  # noqa: E402
from music_library import MusicLibraryError  # noqa: E402
from server import (Handler, _rotate_pairing_from_screen, _sync_pairing_display,
                    is_loopback_bind, normalize_host)  # noqa: E402
from pairing_auth import PairingAuth  # noqa: E402
from snapcast_control import SnapcastClient  # noqa: E402
from test_snapcast_control import FakeTransport, STATUS  # noqa: E402


class MutableVolumeTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.status = copy.deepcopy(STATUS)

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "Server.GetStatus":
            return self.status
        if method == "Client.SetVolume":
            player_id = params["id"]
            for group in self.status["server"]["groups"]:
                for player in group["clients"]:
                    if player["id"] == player_id:
                        player["config"]["volume"] = params["volume"]
            return "ok"
        return "ok"


class FakeLibrary:
    def __init__(self):
        self.track = {"id": "a" * 20, "title": "sample.wav", "relative_path": "private/path.wav",
                      "size_bytes": 4096, "duration_seconds": 1.0}
        self.started = []
        self.stopped = []
        self.paused = []
        self.resumed = []
        self.ended = []

    def list_tracks(self):
        return [self.track]

    def get_track(self, track_id):
        if track_id != self.track["id"]:
            raise MusicLibraryError("unknown track")
        return self.track, Path(self.track["relative_path"])

    def start(self, track_id):
        if track_id != self.track["id"]:
            raise MusicLibraryError("unknown track")
        self.started.append(track_id)
        return self.track

    def source_uri(self, track_id):
        return f"pipe:///tmp/{track_id}.pcm?name=music-{track_id}"

    def sync_sessions(self, _sessions):
        ended, self.ended = self.ended, []
        return ended

    def stop(self, track_id):
        self.stopped.append(track_id)

    def pause(self, track_id):
        self.paused.append(track_id)

    def resume(self, track_id):
        self.resumed.append(track_id)


class MusicUiHttpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.session_state_file = Path(self.tempdir.name) / "sessions.json"
        self.transport = MutableVolumeTransport()
        self.httpd.session = MusicSession(SnapcastClient(transport=self.transport),
                                         state_file=self.session_state_file)
        self.httpd.library = FakeLibrary()
        self.httpd.music_max_start_volume = 35
        self.httpd.music_queue = Handler._empty_queue()
        self.httpd.music_queue_lock = threading.RLock()
        self.httpd.queue_state_file = Path(self.tempdir.name) / "queue.json"
        self.httpd.music_active_file = Path(self.tempdir.name) / "music.active"
        self.httpd.pairing_auth = None
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def test_serves_player_ui(self):
        with urllib.request.urlopen(self.base + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("小瑞音乐", page)
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', page)
        self.assertIn('id="install-panel"', page)
        self.assertIn('id="install-app" class="primary">添加到手机主屏幕', page)
        self.assertIn("Safari：点底部", page)
        self.assertIn('id="pairing-qr"', page)
        self.assertIn("立即播放", page)
        self.assertIn("session.pause", page)
        self.assertIn("toggleOp=s.state", page)
        self.assertNotIn('id="queue-pause"', page)
        self.assertNotIn('id="queue-resume"', page)
        self.assertIn('id="select-all-tracks"', page)
        self.assertIn("播放勾选队列", page)
        self.assertIn("下一首", page)
        self.assertIn('id="queue-retry"', page)
        self.assertIn("queue.retry", page)

    def test_serves_installable_mobile_app_assets(self):
        cases = {
            "/manifest.webmanifest": ("application/manifest+json", '"display": "standalone"'),
            "/service-worker.js": ("text/javascript", 'xiaorui-music-shell-v2'),
            "/icon.svg": ("image/svg+xml", "<svg"),
            "/icon-192.png": ("image/png", ""),
            "/icon-512.png": ("image/png", ""),
        }
        for path, (content_type, expected) in cases.items():
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=2) as response:
                    raw = response.read()
                    body = raw.decode("utf-8") if not path.endswith(".png") else ""
                    self.assertIn(content_type, response.headers.get("Content-Type", ""))
                    if expected:
                        self.assertIn(expected, body)
                    if path.startswith("/icon-"):
                        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
                    if path == "/service-worker.js":
                        self.assertEqual(response.headers.get("Service-Worker-Allowed"), "/")

    def test_protects_music_ui_with_http_basic_auth_when_enabled(self):
        self.httpd.music_access_password = b"0123456789abcdef01234567"
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(self.base + "/", timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 401)
        self.assertIn("WWW-Authenticate", raised.exception.headers)

        token = base64.b64encode(b"xiaorui:0123456789abcdef01234567").decode("ascii")
        req = urllib.request.Request(self.base + "/",
                                     headers={"Authorization": "Basic " + token})
        with urllib.request.urlopen(req, timeout=2) as response:
            self.assertIn("小瑞音乐", response.read().decode("utf-8"))

    def test_auth_rejects_wrong_password(self):
        self.httpd.music_access_password = b"0123456789abcdef01234567"
        token = base64.b64encode(b"xiaorui:wrong-password").decode("ascii")
        req = urllib.request.Request(self.base + "/api/state",
                                     headers={"Authorization": "Basic " + token})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 401)

    def test_pairing_issues_cookie_and_paired_browser_can_revoke(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "pair-state.json",
                                              Path(self.tempdir.name) / "pair-code.json")
        self.httpd.music_public_url = "https://xiaorui-music.local:8765"
        with urllib.request.urlopen(self.base + "/api/pairing/status", timeout=2) as response:
            status = json.loads(response.read())
        self.assertTrue(status["enabled"])
        self.assertTrue(status["local"])
        self.assertEqual(len(status["code"]), 8)
        self.assertEqual(status["public_url"], self.httpd.music_public_url)
        code = status["code"]

        request = urllib.request.Request(self.base + "/api/pair", data=json.dumps({
            "code": code, "name": "Test phone"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            paired = json.loads(response.read())
            cookie = response.headers.get("Set-Cookie")
        cookie_header = cookie.split(";", 1)[0]
        self.assertTrue(paired["ok"])
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("token", json.dumps(paired))

        devices_request = urllib.request.Request(self.base + "/api/pairing/devices",
                                                 headers={"Cookie": cookie_header})
        with urllib.request.urlopen(devices_request, timeout=2) as response:
            devices = json.loads(response.read())
        self.assertEqual(devices["devices"][0]["name"], "Test phone")
        request = urllib.request.Request(self.base + "/api/pairing/revoke", data=json.dumps({
            "device_id": paired["device_id"]}).encode(), method="POST",
            headers={"Content-Type": "application/json", "Cookie": cookie_header})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertTrue(json.loads(response.read())["ok"])
        state_request = urllib.request.Request(self.base + "/api/state",
                                               headers={"Cookie": cookie_header})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(state_request, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 401)

    def test_unpaired_browser_can_load_pairing_shell_but_not_music_api(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "pair-state.json",
                                              Path(self.tempdir.name) / "pair-code.json")
        with urllib.request.urlopen(self.base + "/?pair=12345678", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn('id="pair-dialog"', page)
        self.assertIn('id="pair-code-input"', page)
        self.assertIn("new URLSearchParams(location.search).get('pair')", page)
        self.assertIn("已从二维码读取一次性配对码", page)

        request = urllib.request.Request(self.base + "/api/state")
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 401)

    def test_loopback_can_generate_pair_code_before_pairing(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "pair-state.json",
                                              Path(self.tempdir.name) / "pair-code.json")
        request = urllib.request.Request(self.base + "/api/pairing/rotate", data=b"{}",
                                         method="POST", headers={
                                             "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            rotated = json.loads(response.read())
        self.assertTrue(rotated["ok"])
        self.assertEqual(len(rotated["code"]), 8)

    def test_board_screen_pairing_info_and_rotation_request(self):
        state = Path(self.tempdir.name) / "pair-state.json"
        code_file = Path(self.tempdir.name) / "pair-code.json"
        info_file = Path(self.tempdir.name) / "pairing-display.txt"
        request_file = Path(self.tempdir.name) / "pairing-rotate.request"
        pairing = PairingAuth(state, code_file)
        server = SimpleNamespace(pairing_auth=pairing,
                                 pairing_display_file=info_file,
                                 pairing_rotate_request_file=request_file,
                                 music_public_url="https://speaker.local:8765",
                                 pairing_display_value=None)
        _sync_pairing_display(server)
        lines = info_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "https://speaker.local:8765")
        self.assertEqual(lines[1], pairing.code)
        self.assertGreater(float(lines[2]), 0)
        self.assertEqual(info_file.stat().st_mode & 0o777, 0o600)

        request_file.write_text("rotate\n", encoding="ascii")
        _rotate_pairing_from_screen(server)
        self.assertFalse(request_file.exists())
        rotated = info_file.read_text(encoding="utf-8").splitlines()
        self.assertNotEqual(rotated[1], lines[1])

    def test_pairing_qr_contains_public_url_and_one_time_code(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "pair-state.json",
                                              Path(self.tempdir.name) / "pair-code.json")
        self.httpd.music_public_url = "https://speaker.local:8765"
        code = self.httpd.pairing_auth.code
        with urllib.request.urlopen(self.base + "/api/pairing/qr?v=1", timeout=2) as response:
            svg = response.read().decode("utf-8")
        self.assertIn("image/svg+xml", response.headers.get("Content-Type", ""))
        self.assertIn("<svg", svg)
        self.assertIn(f"https://speaker.local:8765/?pair={code}", svg)

    def test_pairing_code_is_single_use_and_rotate_replaces_it(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "pair-state.json",
                                              Path(self.tempdir.name) / "pair-code.json")
        code = self.httpd.pairing_auth.code

        def post(path, body):
            request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
            try:
                return urllib.request.urlopen(request, timeout=2)
            except Exception as exc:
                return exc

        first = post("/api/pair", {"code": code, "name": "phone"})
        self.assertEqual(first.code, 200)
        cookie_header = first.headers["Set-Cookie"].split(";", 1)[0]
        first.close()
        second = post("/api/pair", {"code": code, "name": "other"})
        self.assertEqual(second.code, 401)
        second.close()
        rotate_request = urllib.request.Request(self.base + "/api/pairing/rotate", data=b"{}",
                                               method="POST", headers={
                                                   "Content-Type": "application/json",
                                                   "Cookie": cookie_header})
        rotated = urllib.request.urlopen(rotate_request, timeout=2)
        self.assertEqual(rotated.code, 200)
        new_code = json.loads(rotated.read())["code"]
        self.assertNotEqual(code, new_code)

    def test_allows_only_explicit_hosts_when_not_loopback_host(self):
        self.httpd.music_allowed_hosts = {"speaker.example"}
        req = urllib.request.Request(self.base + "/health",
                                     headers={"Host": "speaker.example:8765"})
        with urllib.request.urlopen(req, timeout=2) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})

        req = urllib.request.Request(self.base + "/health",
                                     headers={"Host": "other.example:8765"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 403)

    def test_state_endpoint_lists_devices_and_reconciles(self):
        with urllib.request.urlopen(self.base + "/api/state", timeout=2) as response:
            state = json.loads(response.read())
        self.assertTrue(state["ok"])
        self.assertEqual(len(state["players"]), 2)
        self.assertIn("reconcile", state)

    def test_library_endpoint_hides_absolute_and_relative_file_paths(self):
        with urllib.request.urlopen(self.base + "/api/library", timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"])
        self.assertEqual(result["tracks"][0]["title"], "sample.wav")
        self.assertNotIn("relative_path", result["tracks"][0])

    def test_library_play_uses_opaque_track_id(self):
        self.httpd.music_max_start_volume = 100
        body = json.dumps({"op": "library.play", "track_id": "a" * 20,
                           "players": ["aa:bb:cc:dd:ee:01"]}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.httpd.library.started, ["a" * 20])
        self.assertTrue(self.httpd.music_active_file.exists())
        sessions = self.httpd.session.handle({"op": "sessions.list"})["sessions"]
        self.assertEqual(sessions[0]["track_title"], "sample.wav")

    def test_music_play_is_blocked_above_safe_start_volume(self):
        body = json.dumps({"op": "library.play", "track_id": "a" * 20,
                           "players": ["aa:bb:cc:dd:ee:01"]}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 409)
        body = json.loads(raised.exception.read())
        self.assertEqual(body["code"], "volume_too_high")
        self.assertEqual(body["max_start_volume"], 35)
        self.assertEqual(body["players"][0]["volume"], 100)
        self.assertEqual(self.httpd.library.started, [])

    def test_confirmed_safe_volume_lowers_selected_players_and_starts(self):
        first = self.transport.status["server"]["groups"][0]["clients"][0]
        first["config"]["volume"] = {"muted": True, "percent": 100}
        second_id = "aa:bb:cc:dd:ee:01"
        body = json.dumps({"op": "library.play", "track_id": "a" * 20,
                           "players": [second_id], "startup_volume": 24}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        volume_call = next(params for method, params in self.transport.calls
                           if method == "Client.SetVolume")
        self.assertEqual(volume_call["volume"], {"muted": True, "percent": 24})
        self.assertEqual(self.httpd.library.started, ["a" * 20])
        self.assertTrue(self.httpd.music_active_file.exists())

    def test_confirmed_volume_rejects_values_above_safe_limit(self):
        body = json.dumps({"op": "library.play", "track_id": "a" * 20,
                           "players": ["aa:bb:cc:dd:ee:01"],
                           "startup_volume": 36}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 400)
        self.assertIn("0~35%", raised.exception.read().decode())
        self.assertEqual(self.httpd.library.started, [])

    def test_queue_play_can_confirm_safe_volume(self):
        track_id = "a" * 20
        body = json.dumps({"op": "queue.play", "track_ids": [track_id],
                           "players": ["aa:bb:cc:dd:ee:01"],
                           "startup_volume": 20}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.httpd.music_queue["state"], "playing")
        self.assertTrue(any(method == "Client.SetVolume"
                            and params["volume"]["percent"] == 20
                            for method, params in self.transport.calls))

    def test_music_play_rejects_offline_device_before_starting_writer(self):
        body = json.dumps({"op": "library.play", "track_id": "a" * 20,
                           "players": ["aa:bb:cc:dd:ee:ff"]}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 400)
        self.assertIn("离线", raised.exception.read().decode())
        self.assertEqual(self.httpd.library.started, [])

    def test_queue_play_next_and_clear(self):
        self.httpd.music_max_start_volume = 35
        players = ["aa:bb:cc:dd:ee:01"]
        track_id = "a" * 20
        client = self.transport.status["server"]["groups"][0]["clients"][0]
        client["config"]["volume"]["percent"] = 20

        def post(payload):
            req = urllib.request.Request(self.base + "/api", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.loads(response.read())

        started = post({"op": "queue.play", "track_ids": [track_id, track_id],
                        "players": players})
        self.assertTrue(started["ok"], started)
        self.assertTrue(self.httpd.music_active_file.exists())
        self.assertEqual(started["queue"]["index"], 0)
        self.assertEqual(len(self.httpd.library.started), 1)
        # A user may raise volume after safely starting the queue. Advancing
        # that already-authorized queue must not re-apply the new-start limit.
        client["config"]["volume"]["percent"] = 43
        post({"op": "session.pause", "session_id": started["queue"]["session_id"]})
        self.assertEqual(self.httpd.library.paused, [track_id])
        self.assertFalse(self.httpd.music_active_file.exists())
        with urllib.request.urlopen(self.base + "/api/state", timeout=2) as response:
            self.assertEqual(json.loads(response.read())["queue"]["state"], "paused")
        post({"op": "session.resume", "session_id": started["queue"]["session_id"]})
        self.assertEqual(self.httpd.library.resumed, [track_id])
        self.assertTrue(self.httpd.music_active_file.exists())

        advanced = post({"op": "queue.next"})
        self.assertTrue(advanced["ok"], advanced)
        self.assertEqual(advanced["queue"]["index"], 1)
        self.assertEqual(len(self.httpd.library.started), 2)
        self.assertEqual(self.httpd.library.stopped, [track_id])

        cleared = post({"op": "queue.clear"})
        self.assertTrue(cleared["ok"], cleared)
        self.assertEqual(cleared["queue"]["track_ids"], [])
        self.assertEqual(self.httpd.session.handle({"op": "sessions.list"})["sessions"], [])
        self.assertFalse(self.httpd.music_active_file.exists())

    def test_queue_automatically_advances_when_track_ends(self):
        self.httpd.music_max_start_volume = 35
        track_id = "a" * 20
        client = self.transport.status["server"]["groups"][0]["clients"][0]
        client["config"]["volume"]["percent"] = 20
        req = urllib.request.Request(self.base + "/api", data=json.dumps({
            "op": "queue.play", "track_ids": [track_id, track_id],
            "players": ["aa:bb:cc:dd:ee:01"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            started = json.loads(response.read())
        old_session = started["queue"]["session_id"]
        client["config"]["volume"]["percent"] = 43
        self.httpd.library.ended = [old_session]
        # Invoke the same maintenance entry point used by the background worker;
        # no HTTP/UI poll is involved in advancing the queue.
        helper = object.__new__(Handler)
        helper.server = self.httpd
        helper._sync_library_sessions()
        self.assertEqual(self.httpd.music_queue["index"], 1)
        self.assertNotEqual(self.httpd.music_queue["session_id"], old_session)
        self.assertEqual(len(self.httpd.library.started), 2)

    def test_failed_queue_is_restored_and_can_be_retried_explicitly(self):
        self.httpd.music_max_start_volume = 35
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20
        track_id = "a" * 20
        payload = {"track_ids": [track_id, track_id],
                   "players": ["aa:bb:cc:dd:ee:01"], "index": 1,
                   "session_id": None, "state": "error",
                   "error": "模拟切歌失败"}
        self.httpd.queue_state_file.write_text(json.dumps(payload), encoding="utf-8")
        self.httpd.music_queue = Handler._load_queue(
            self.httpd.queue_state_file, self.httpd.library, self.httpd.session)
        self.assertEqual(self.httpd.music_queue["state"], "error")
        self.assertEqual(self.httpd.music_queue["index"], 1)
        self.assertEqual(self.httpd.library.started, [])

        req = urllib.request.Request(self.base + "/api", data=json.dumps(
            {"op": "queue.retry", "startup_volume": 24}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["queue"]["state"], "playing")
        self.assertEqual(result["queue"]["index"], 1)
        self.assertEqual(self.httpd.library.started, [track_id])

    def test_queue_retry_enforces_safe_start_volume(self):
        track_id = "a" * 20
        self.httpd.music_max_start_volume = 35
        self.httpd.music_queue = {
            "track_ids": [track_id], "players": ["aa:bb:cc:dd:ee:01"],
            "index": 0, "session_id": None, "state": "error",
            "error": "之前启动失败",
        }
        client = self.transport.status["server"]["groups"][0]["clients"][0]
        client["config"]["volume"]["percent"] = 61

        req = urllib.request.Request(self.base + "/api", data=json.dumps(
            {"op": "queue.retry"}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(caught.exception.code, 409)
        result = json.loads(caught.exception.read())
        self.assertEqual(result["code"], "volume_too_high")
        self.assertEqual(self.httpd.library.started, [])

        req = urllib.request.Request(self.base + "/api", data=json.dumps(
            {"op": "queue.retry", "startup_volume": 24}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.httpd.library.started, [track_id])
        self.assertEqual(client["config"]["volume"]["percent"], 24)

    def test_queue_state_restores_only_when_matching_session_is_active(self):
        self.httpd.music_max_start_volume = 100
        track_id = "a" * 20
        payload = {"op": "queue.play", "track_ids": [track_id, track_id],
                   "players": ["aa:bb:cc:dd:ee:01"]}
        req = urllib.request.Request(self.base + "/api", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        restarted_session = MusicSession(SnapcastClient(transport=FakeTransport()),
                                         state_file=self.session_state_file)
        restored = Handler._load_queue(self.httpd.queue_state_file, self.httpd.library,
                                       restarted_session)
        self.assertEqual(restored["session_id"], result["queue"]["session_id"])
        self.assertEqual(restored["track_ids"], [track_id, track_id])
        restarted_session.handle({"op": "session.stop", "session_id": restored["session_id"]})
        stale = Handler._load_queue(self.httpd.queue_state_file, self.httpd.library,
                                    restarted_session)
        self.assertEqual(stale["state"], "stopped")

    def test_api_endpoint_applies_player_volume(self):
        body = json.dumps({"op": "player.volume", "player_id": "aa:bb:cc:dd:ee:01",
                           "volume": 12}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"])
        self.assertEqual(result["volume"], 12)

    def test_api_endpoint_renames_known_player(self):
        body = json.dumps({"op": "player.name", "player_id": "aa:bb:cc:dd:ee:01",
                           "name": "客厅音箱"}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "客厅音箱")
        self.assertIn(("Client.SetName", {"id": "aa:bb:cc:dd:ee:01", "name": "客厅音箱"}),
                      self.transport.calls)

    def test_player_name_rejects_empty_or_overlong_name(self):
        for name in ("  ", "音箱" * 17):
            body = json.dumps({"op": "player.name", "player_id": "aa:bb:cc:dd:ee:01",
                               "name": name}).encode()
            req = urllib.request.Request(self.base + "/api", data=body,
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(Exception) as raised:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(getattr(raised.exception, "code", None), 400)

    def test_rejects_oversized_or_invalid_json(self):
        req = urllib.request.Request(self.base + "/api", data=b"{bad",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 400)

    def test_rejects_dns_rebinding_host_header(self):
        req = urllib.request.Request(self.base + "/api/library",
                                     headers={"Host": "attacker.example"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 403)

    def test_rejects_cross_site_browser_write(self):
        body = json.dumps({"op": "player.volume", "player_id": "aa:bb:cc:dd:ee:01",
                           "volume": 0}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Origin": "http://attacker.example"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 403)

    def test_rejects_non_loopback_bind_address(self):
        self.assertTrue(is_loopback_bind("127.0.0.1"))
        self.assertTrue(is_loopback_bind("::1"))
        self.assertTrue(is_loopback_bind("localhost"))
        self.assertFalse(is_loopback_bind("0.0.0.0"))
        self.assertFalse(is_loopback_bind("192.168.1.20"))
        self.assertFalse(Handler._local_hostname("0.0.0.0"))
        self.assertFalse(Handler._local_hostname("music-room.example"))
        self.assertTrue(Handler._local_hostname("localhost:8765"))
        self.assertTrue(Handler._local_hostname("[::1]:8765"))
        self.assertEqual(normalize_host("Speaker.Example."), "speaker.example")
        self.assertEqual(normalize_host("[2001:db8::1]"), "2001:db8::1")
        with self.assertRaises(ValueError):
            normalize_host("speaker.example:8765")

    def test_server_refuses_lan_bind_without_tls_and_authentication(self):
        script = ROOT / "tools" / "snapcast_center" / "server.py"
        result = subprocess.run([sys.executable, str(script), "--bind", "0.0.0.0",
                                 "--port", "0"], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("HTTPS", result.stderr)
        self.assertIn("口令文件或配对认证", result.stderr)

    def test_server_rejects_public_url_with_non_root_path(self):
        script = ROOT / "tools" / "snapcast_center" / "server.py"
        result = subprocess.run([sys.executable, str(script), "--public-url",
                                 "https://music.example.local/controller"],
                               capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("根路径", result.stderr)

    def test_server_requires_explicit_allowed_host_for_lan_bind(self):
        script = ROOT / "tools" / "snapcast_center" / "server.py"
        with tempfile.TemporaryDirectory() as temp:
            password_path = Path(temp) / "password"
            password_path.write_text("0123456789abcdef01234567", encoding="ascii")
            password_path.chmod(0o600)
            cert_path = Path(temp) / "cert.pem"
            key_path = Path(temp) / "key.pem"
            cert_path.write_text("placeholder", encoding="ascii")
            key_path.write_text("placeholder", encoding="ascii")
            key_path.chmod(0o600)
            result = subprocess.run([sys.executable, str(script), "--bind", "0.0.0.0",
                                     "--port", "0", "--tls-cert", str(cert_path),
                                     "--tls-key", str(key_path), "--access-password-file",
                                     str(password_path)], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--allowed-hosts", result.stderr)

    def test_server_rejects_world_readable_password_file(self):
        script = ROOT / "tools" / "snapcast_center" / "server.py"
        with tempfile.TemporaryDirectory() as temp:
            password_path = Path(temp) / "password"
            password_path.write_text("0123456789abcdef01234567", encoding="ascii")
            password_path.chmod(0o644)
            cert_path = Path(temp) / "cert.pem"
            key_path = Path(temp) / "key.pem"
            cert_path.write_text("placeholder", encoding="ascii")
            key_path.write_text("placeholder", encoding="ascii")
            key_path.chmod(0o600)
            result = subprocess.run([sys.executable, str(script), "--bind", "0.0.0.0",
                                     "--port", "0", "--tls-cert", str(cert_path),
                                     "--tls-key", str(key_path), "--access-password-file",
                                     str(password_path), "--allowed-hosts", "speaker.local"],
                                    capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 2)
        self.assertIn("口令文件权限过宽", result.stderr)


if __name__ == "__main__":
    unittest.main()

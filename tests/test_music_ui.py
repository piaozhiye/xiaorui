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
import urllib.error
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "snapcast_center"))

from music_session import MusicSession  # noqa: E402
from music_library import MusicLibraryError  # noqa: E402
from server import (Handler, _rotate_pairing_from_screen, _sync_pairing_display,
                    create_loopback_voice_server, is_loopback_bind,
                    normalize_host)  # noqa: E402
import server as music_server_module  # noqa: E402
from pairing_auth import PairingAuth  # noqa: E402
from music_collections import MusicCollections  # noqa: E402
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
        self.position_seconds = 0.55
        self.uploaded = []
        self.deleted = []

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

    def start_radio(self, track_id, url, source=None):
        self.started.append((track_id, url))
        return source or f"pipe:///tmp/{track_id.replace(':', '-')}.pcm?name=radio-test"

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

    def position(self, _track_id):
        return self.position_seconds

    def duration(self, track_id):
        return self.get_track(track_id)[0]["duration_seconds"]

    def import_upload(self, filename, chunks, size_bytes):
        payload = b"".join(chunks)
        self.uploaded.append((filename, payload, size_bytes))
        track = {"id": "b" * 20, "title": filename, "size_bytes": size_bytes,
                 "duration_seconds": 2.0, "relative_path": "uploads/opaque.wav"}
        self.track = track
        return track

    def upload_usage(self):
        return {"used_bytes": sum(row[2] for row in self.uploaded),
                "max_bytes": 512 * 1024 * 1024, "available_bytes": 500 * 1024 * 1024}

    def delete_upload(self, track_id):
        if track_id != self.track["id"] or not self.track["relative_path"].startswith("uploads/"):
            raise MusicLibraryError("只能删除手机上传的曲目")
        self.deleted.append(track_id)
        return self.upload_usage()


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
        self.httpd.voice_ui_socket = "/tmp/fake-voice.sock"
        self.httpd.music_queue = Handler._empty_queue()
        self.httpd.music_queue_lock = threading.RLock()
        self.httpd.queue_state_file = Path(self.tempdir.name) / "queue.json"
        self.httpd.collections = MusicCollections(Path(self.tempdir.name) / "collections.json")
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
        self.assertIn('role="tablist" aria-label="主导航"', page)
        for page_id in ("play", "library", "devices", "radio", "settings"):
            self.assertIn(f'id="tab-{page_id}"', page)
            self.assertIn(f'id="page-{page_id}"', page)
        self.assertIn("localStorage.setItem('xiaorui-music-page',page)", page)
        self.assertIn("switchPage('play')", page)
        self.assertIn('id="install-app" class="primary">添加到手机主屏幕', page)
        self.assertIn("Safari：点底部", page)
        self.assertIn('id="pairing-qr"', page)
        self.assertIn("立即播放", page)
        self.assertIn("session.pause", page)
        self.assertIn("toggleOp=s.state", page)
        self.assertNotIn('id="queue-pause"', page)
        self.assertNotIn('id="queue-resume"', page)
        self.assertIn('id="select-all-tracks"', page)
        self.assertIn('id="library-pagination"', page)
        self.assertIn('id="library-prev"', page)
        self.assertIn('id="library-next"', page)
        self.assertIn("const LIBRARY_PAGE_SIZE=20,MAX_QUEUE_TRACKS=20", page)
        self.assertIn("function renderLibraryPage()", page)
        self.assertIn("队列最多选择 ${MAX_QUEUE_TRACKS} 首", page)
        self.assertIn("全选本页", page)
        self.assertIn("播放勾选队列", page)
        self.assertIn('id="select-all-players"', page)
        self.assertIn('id="clear-player-selection"', page)
        self.assertIn('id="player-selection-status"', page)
        self.assertIn("已选 ${selected}/${online.length} 台在线设备", page)
        self.assertIn("button:focus-visible", page)
        self.assertIn("下一首", page)
        self.assertIn('id="queue-retry"', page)
        self.assertIn("queue.retry", page)
        self.assertIn('id="queue-previous"', page)
        self.assertIn("queue.jump", page)
        self.assertIn("queue.remove", page)
        self.assertIn('id="connection-banner"', page)
        self.assertIn("navigator.mediaSession", page)
        self.assertIn("正在播放", page)
        self.assertIn("const visible=sessions.filter(s=>['playing','paused','starting','unknown'].includes(s.state)", page)
        self.assertIn("async function cleanupOrphanSessions(sessions,queue)", page)
        self.assertIn("session.state!=='unknown'||(session.players||[]).length", page)
        self.assertNotIn("清理旧会话", page)
        self.assertIn("安全音量上限", page)
        self.assertIn('id="upload-audio"', page)
        self.assertIn("/api/upload?filename=", page)
        self.assertIn("上传并播放", page)
        self.assertIn("删除上传", page)
        self.assertIn("512 MB", page)
        self.assertIn("library.delete_upload", page)
        self.assertIn('id="voice-volume"', page)
        self.assertIn("本机“我在”、回答和提示音", page)
        self.assertIn("统一音乐音量", page)
        self.assertEqual(page.count('class="volume-control"'), 2)
        self.assertIn("grid-template-columns:7em minmax(0,1fr) 3.5em", page)
        self.assertIn('for="bulk-volume"', page)
        self.assertIn('for="voice-volume"', page)
        self.assertIn('id="radio-presets"', page)
        self.assertIn("const CNR_PRESETS=", page)
        self.assertIn('id="radio-search"', page)
        self.assertIn('id="radio-now"', page)
        self.assertIn('id="radio-select-players"', page)
        self.assertIn("async function playRadioStation(station)", page)
        self.assertIn("添加并播放", page)
        self.assertIn("已收藏", page)
        self.assertIn("播放控制和音量在“播放”和“设备”页面统一管理", page)
        for station in ("中国之声", "经济之声", "音乐之声", "经典音乐广播", "文艺之声"):
            self.assertIn(station, page)
        self.assertIn("添加自定义电台", page)
        self.assertIn("text.textContent='音乐音量'", page)

    def test_voice_gain_endpoint_is_separate_and_validated(self):
        original_get, original_set = music_server_module.get_voice_gain, music_server_module.set_voice_gain
        self.addCleanup(setattr, music_server_module, "get_voice_gain", original_get)
        self.addCleanup(setattr, music_server_module, "set_voice_gain", original_set)
        saved = {"gain": 40}
        music_server_module.get_voice_gain = lambda path: saved["gain"]

        def set_gain(path, gain):
            if isinstance(gain, bool) or not isinstance(gain, int) or not 10 <= gain <= 100:
                raise music_server_module.VoiceVolumeError("语音音量必须是 10 到 100 的整数")
            saved["gain"] = gain
            return gain

        music_server_module.set_voice_gain = set_gain
        with urllib.request.urlopen(self.base + "/api/voice-volume", timeout=2) as response:
            self.assertEqual(json.loads(response.read())["gain"], 40)
        request = urllib.request.Request(self.base + "/api/voice-volume", data=b'{"gain":32}',
                                         method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(json.loads(response.read())["gain"], 32)
        request = urllib.request.Request(self.base + "/api/voice-volume", data=b'{"gain":101}',
                                         method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(saved["gain"], 32)

    def test_upload_endpoint_streams_file_and_returns_library_track(self):
        payload = b"fake audio body"
        request = urllib.request.Request(
            self.base + "/api/upload?filename=%E6%B5%8B%E8%AF%95.wav", data=payload,
            method="POST", headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
            self.assertEqual(response.status, 201)
        self.assertTrue(result["ok"])
        self.assertEqual(result["track"]["id"], "b" * 20)
        self.assertEqual(self.httpd.library.uploaded, [("测试.wav", payload, len(payload))])

    def test_upload_endpoint_rejects_non_audio_content_type(self):
        request = urllib.request.Request(self.base + "/api/upload?filename=x.wav", data=b"body",
                                         method="POST", headers={"Content-Type": "text/plain"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 415)

    def test_upload_endpoint_enforces_size_limit_before_reading_body(self):
        from music_library import MAX_UPLOAD_BYTES
        request = urllib.request.Request(self.base + "/api/upload?filename=x.wav", data=b"x",
                                         method="POST", headers={"Content-Type": "application/octet-stream"})
        request.add_header("Content-Length", str(MAX_UPLOAD_BYTES + 1))
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 413)
        self.assertEqual(self.httpd.library.uploaded, [])

    def test_serves_installable_mobile_app_assets(self):
        cases = {
            "/manifest.webmanifest": ("application/manifest+json", '"display": "standalone"'),
            "/service-worker.js": ("text/javascript", 'xiaorui-music-shell-v11'),
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

    def test_loopback_voice_endpoint_stays_separate_from_paired_browser_api(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "voice-pair-state.json",
                                              Path(self.tempdir.name) / "voice-pair-code.json")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base + "/api/state", timeout=2)
        self.assertEqual(raised.exception.code, 401)

        request = urllib.request.Request(self.base + "/api/voice",
            data=json.dumps({"action": "status"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        self.assertEqual(result, {"ok": True, "response": "当前没有正在播放的音乐"})

        request = urllib.request.Request(self.base + "/api/voice",
            data=json.dumps({"action": "arbitrary.snapcast.op"}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)
        self.assertIn("不支持", json.loads(raised.exception.read())["error"])

    def test_dedicated_voice_listener_is_loopback_only_and_shares_music_state(self):
        self.httpd.pairing_auth = PairingAuth(Path(self.tempdir.name) / "voice-pair-state.json",
                                              Path(self.tempdir.name) / "voice-pair-code.json")
        voice_server = create_loopback_voice_server(self.httpd, 0)
        voice_thread = threading.Thread(target=voice_server.serve_forever, daemon=True)
        voice_thread.start()
        self.addCleanup(voice_server.server_close)
        self.addCleanup(voice_server.shutdown)
        self.assertEqual(voice_server.server_address[0], "127.0.0.1")
        base = f"http://127.0.0.1:{voice_server.server_address[1]}"

        request = urllib.request.Request(base + "/api/voice",
            data=json.dumps({"action": "status"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(json.loads(response.read()),
                             {"ok": True, "response": "当前没有正在播放的音乐"})

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(base + "/api/state", timeout=2)
        self.assertEqual(raised.exception.code, 404)

    def test_voice_play_uses_shared_queue_and_only_online_players(self):
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20
        request = urllib.request.Request(self.base + "/api/voice",
            data=json.dumps({"action": "play", "query": "sample"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        self.assertIn("同步播放", result["response"])
        self.assertEqual(self.httpd.music_queue["state"], "playing")
        self.assertEqual(self.httpd.music_queue["track_ids"], [self.httpd.library.track["id"]])
        self.assertEqual(self.httpd.music_queue["players"], ["aa:bb:cc:dd:ee:01"])
        self.assertEqual(self.httpd.library.started, [self.httpd.library.track["id"]])

        def post(action):
            body = json.dumps({"action": action}).encode()
            req = urllib.request.Request(self.base + "/api/voice", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.loads(response.read())

        self.assertTrue(post("pause")["ok"])
        self.assertEqual(self.httpd.music_queue["state"], "paused")
        self.assertTrue(post("resume")["ok"])
        self.assertEqual(self.httpd.music_queue["state"], "playing")
        self.assertEqual(self.httpd.library.paused, [self.httpd.library.track["id"]])
        self.assertEqual(self.httpd.library.resumed, [self.httpd.library.track["id"]])

    def test_voice_radio_uses_pcm_fifo_and_can_pause_resume(self):
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20
        station = self.httpd.collections.station_create("测试电台", "https://radio.example/live.m3u8")

        def post(action, query=None):
            body = json.dumps({"action": action, "query": query}).encode()
            request = urllib.request.Request(self.base + "/api/voice", data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.loads(response.read())

        result = post("play", "测试电台")
        self.assertTrue(result["ok"], result)
        marker = "radio:" + station["id"]
        self.assertIn((marker, station["url"]), self.httpd.library.started)
        session = self.httpd.session.handle({"op": "sessions.list"})["sessions"][0]
        self.assertTrue(session["source"].startswith("pipe:///"))
        self.assertEqual(session["source_url"], station["url"])
        self.assertTrue(post("pause")["ok"])
        self.assertTrue(post("resume")["ok"])
        self.assertEqual(self.httpd.library.paused, [marker])
        self.assertEqual(self.httpd.library.resumed, [marker])

    def test_voice_start_obeys_safe_volume_limit(self):
        request = urllib.request.Request(self.base + "/api/voice",
            data=json.dumps({"action": "play", "query": "sample"}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 409)
        result = json.loads(raised.exception.read())
        self.assertEqual(result["code"], "volume_too_high")
        self.assertEqual(self.httpd.library.started, [])

    def test_library_endpoint_hides_absolute_and_relative_file_paths(self):
        with urllib.request.urlopen(self.base + "/api/library", timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"])
        self.assertEqual(result["tracks"][0]["title"], "sample.wav")
        self.assertNotIn("relative_path", result["tracks"][0])
        self.assertFalse(result["tracks"][0]["uploaded"])
        self.assertIn("upload_usage", result)

    def test_library_query_uses_server_side_search_and_pagination(self):
        with urllib.request.urlopen(self.base + "/api/library?q=sample&page=0&page_size=1&sort=artist", timeout=2) as response:
            result = json.loads(response.read())
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["page_size"], 1)
        self.assertEqual(len(result["tracks"]), 1)
        with urllib.request.urlopen(self.base + "/api/library?q=does-not-exist", timeout=2) as response:
            empty = json.loads(response.read())
        self.assertEqual(empty["total"], 0)
        with urllib.request.urlopen(self.base + "/api/library?folder=__root__", timeout=2) as response:
            root_folder = json.loads(response.read())
        self.assertEqual(root_folder["total"], 1)

    def test_collections_endpoint_and_favorite_toggle(self):
        track_id = "a" * 20
        body = json.dumps({"op": "favorite.toggle", "track_id": track_id}).encode()
        request = urllib.request.Request(self.base + "/api", data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertTrue(json.loads(response.read())["favorite"])
        with urllib.request.urlopen(self.base + "/api/collections", timeout=2) as response:
            self.assertIn(track_id, json.loads(response.read())["favorites"])

    def test_playlist_create_rejects_malformed_track_list_without_server_error(self):
        request = urllib.request.Request(
            self.base + "/api", data=json.dumps({"op": "playlist.create", "name": "bad",
                                                  "track_ids": None}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 400)
        result = json.loads(raised.exception.read())
        self.assertFalse(result["ok"])
        self.assertIn("曲目", result["error"])

    def test_radio_source_validation_and_collection_persistence(self):
        body = json.dumps({"op": "radio.add", "name": "测试电台",
                           "url": "file:///etc/passwd"}).encode()
        request = urllib.request.Request(self.base + "/api", data=body,
                                         headers={"Content-Type": "application/json"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 400)
        body = json.dumps({"op": "radio.add", "name": "局域网电台",
                           "url": "http://192.168.1.2:8000/stream"}).encode()
        request = urllib.request.Request(self.base + "/api", data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"])
        self.assertEqual(self.httpd.collections.list()["stations"][0]["url"],
                         "http://192.168.1.2:8000/stream")

    def test_radio_playback_is_separate_from_file_queue_and_pause_resume_works(self):
        player_id = STATUS["server"]["groups"][0]["clients"][0]["id"]
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20
        self.httpd.music_max_start_volume = 35

        def post(payload):
            req = urllib.request.Request(self.base + "/api", data=json.dumps(payload).encode(),
                                          headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.loads(response.read())

        station = post({"op": "radio.add", "name": "测试电台", "url": "http://192.168.1.2:8000/stream"})["station"]
        started = post({"op": "radio.play", "id": station["id"], "players": [player_id]})
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["queue"]["source_kind"], "radio")
        self.assertEqual(started["queue"]["tracks"], [])
        marker = "radio:" + station["id"]
        self.assertIn((marker, "http://192.168.1.2:8000/stream"), self.httpd.library.started)
        session_id = started["session_id"]
        session_row = next(row for row in post({"op": "sessions.list"})["sessions"]
                           if row["session_id"] == session_id)
        self.assertTrue(session_row["source"].startswith("pipe:///"))
        self.assertEqual(session_row["source_url"], station["url"])
        self.assertTrue(post({"op": "session.pause", "session_id": session_id})["ok"])
        self.assertTrue(post({"op": "session.resume", "session_id": session_id})["ok"])
        self.assertEqual(self.httpd.library.paused, [marker])
        self.assertEqual(self.httpd.library.resumed, [marker])

    def test_radio_queue_is_restored_only_for_matching_session(self):
        station_id = "c" * 32
        session_row = {"session_id": "radio-session", "state": "playing",
                       "track_id": "radio:" + station_id}
        fake_session = SimpleNamespace(handle=lambda request: {"sessions": [session_row]})
        payload = {"source_kind": "radio", "station_id": station_id,
                   "session_id": "radio-session", "players": ["player"],
                   "state": "playing", "current_title": "电台"}
        path = Path(self.tempdir.name) / "queue.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        restored = Handler._load_queue(path, self.httpd.library, fake_session)
        self.assertEqual(restored["state"], "playing")
        self.assertEqual(restored["source_kind"], "radio")
        payload["session_id"] = "stale-session"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(Handler._load_queue(path, self.httpd.library, fake_session)["state"], "stopped")

    def test_library_delete_upload_refuses_tracks_still_queued(self):
        self.httpd.library.track = {"id": "b" * 20, "title": "phone.wav",
                                    "relative_path": "uploads/phone.wav", "size_bytes": 32,
                                    "duration_seconds": 1.0}
        self.httpd.music_queue = {"track_ids": ["b" * 20], "players": [], "index": 0,
                                  "session_id": None, "state": "playing", "error": None}
        request = urllib.request.Request(self.base + "/api", data=json.dumps({
            "op": "library.delete_upload", "track_id": "b" * 20}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(Exception) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(getattr(raised.exception, "code", None), 400)
        self.assertEqual(self.httpd.library.deleted, [])

    def test_library_delete_upload_removes_unqueued_uploaded_track(self):
        self.httpd.library.track = {"id": "b" * 20, "title": "phone.wav",
                                    "relative_path": "uploads/phone.wav", "size_bytes": 32,
                                    "duration_seconds": 1.0}
        request = urllib.request.Request(self.base + "/api", data=json.dumps({
            "op": "library.delete_upload", "track_id": "b" * 20}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertTrue(json.loads(response.read())["ok"])
        self.assertEqual(self.httpd.library.deleted, ["b" * 20])

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

    def test_queue_previous_jump_and_remove_are_index_based(self):
        self.httpd.music_max_start_volume = 100
        player = "aa:bb:cc:dd:ee:01"
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20

        def post(payload):
            req = urllib.request.Request(self.base + "/api", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.loads(response.read())

        started = post({"op": "queue.play", "track_ids": ["a" * 20] * 3,
                        "players": [player]})
        at_two = post({"op": "queue.jump", "index": 2})
        self.assertEqual(at_two["queue"]["index"], 2)
        previous = post({"op": "queue.previous"})
        self.assertEqual(previous["queue"]["index"], 1)
        removed_before = post({"op": "queue.remove", "index": 0})
        self.assertEqual(removed_before["queue"]["track_ids"], ["a" * 20] * 2)
        self.assertEqual(removed_before["queue"]["index"], 0)
        removed_current = post({"op": "queue.remove", "index": 0})
        self.assertEqual(removed_current["queue"]["track_ids"], ["a" * 20])
        self.assertEqual(removed_current["queue"]["index"], 0)
        self.assertEqual(removed_current["queue"]["state"], "playing")
        self.assertNotEqual(started["queue"]["session_id"], removed_current["queue"]["session_id"])

    def test_queue_snapshot_reports_estimated_progress_and_duration(self):
        self.httpd.music_max_start_volume = 100
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20
        req = urllib.request.Request(self.base + "/api", data=json.dumps({
            "op": "queue.play", "track_ids": ["a" * 20],
            "players": ["aa:bb:cc:dd:ee:01"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2):
            pass
        with urllib.request.urlopen(self.base + "/api/state", timeout=2) as response:
            state = json.loads(response.read())
        queue = state["queue"]
        self.assertEqual(queue["position_seconds"], 0.55)
        self.assertEqual(queue["duration_seconds"], 1.0)
        self.assertEqual(queue["current_track_id"], "a" * 20)

    def test_queue_completion_is_kept_for_ui_and_can_be_replayed(self):
        self.httpd.music_max_start_volume = 100
        self.transport.status["server"]["groups"][0]["clients"][0]["config"]["volume"]["percent"] = 20
        track_id = "a" * 20
        req = urllib.request.Request(self.base + "/api", data=json.dumps({
            "op": "queue.play", "track_ids": [track_id],
            "players": ["aa:bb:cc:dd:ee:01"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2):
            pass
        self.httpd.library.ended = [self.httpd.music_queue["session_id"]]
        helper = Handler.__new__(Handler)
        helper.server = self.httpd
        helper._sync_library_sessions()
        self.assertEqual(self.httpd.music_queue["state"], "completed")
        self.assertEqual(self.httpd.music_queue["track_ids"], [track_id])

    def test_batch_volume_and_group_mute_operations(self):
        first, second = [p["id"] for p in STATUS["server"]["groups"][0]["clients"]]
        self.transport.status["server"]["groups"][0]["clients"][1]["connected"] = True
        group_id = STATUS["server"]["groups"][0]["id"]
        body = json.dumps({"op": "players.volume", "player_ids": [first, second],
                           "volume": 27}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        calls = [params for method, params in self.transport.calls if method == "Client.SetVolume"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row["volume"]["percent"] == 27 for row in calls))

        body = json.dumps({"op": "group.mute", "group_id": group_id, "muted": False}).encode()
        req = urllib.request.Request(self.base + "/api", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.loads(response.read())
        self.assertTrue(result["ok"], result)
        self.assertIn(("Group.SetMute", {"id": group_id, "mute": False}), self.transport.calls)

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

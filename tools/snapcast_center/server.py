#!/usr/bin/env python3
"""Local web controller for a Snapcast-backed Xiaorui music session."""
import argparse
import base64
import hmac
import ipaddress
import io
import json
import os
import pathlib
import re
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from music_library import MusicLibrary, MusicLibraryError
from music_session import MusicSession
from pairing_auth import PairingAuth
from snapcast_control import SnapcastClient, SnapcastError

_PAIRING_DISPLAY_LOCK = threading.Lock()

# PyQRCode is vendored so the embedded controller can create a pairing QR
# without reaching PyPI or a third-party QR service at runtime.
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import pyqrcode


class MusicVolumeError(MusicLibraryError):
    """A playback request needs an explicit safe-volume choice in the UI."""

    def __init__(self, players, maximum):
        self.players = players
        self.maximum = maximum
        details = "、".join(f"{row['name']} {row['volume']}%" for row in players)
        super().__init__(f"播放音量超过安全上限 {maximum}%；当前：{details}")


def _sync_pairing_display(server):
    """Publish the current one-time pairing data for the local screen UI."""
    pairing = getattr(server, "pairing_auth", None)
    path = getattr(server, "pairing_display_file", None)
    if pairing is None or path is None:
        return
    with _PAIRING_DISPLAY_LOCK:
        with pairing._lock:
            code = pairing.code or ""
            created = pairing.code_data.get("created_at", 0) if code else 0
        value = f"{server.music_public_url}\n{code}\n{created}\n"
        if value == getattr(server, "pairing_display_value", None):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        server.pairing_display_value = value


def _rotate_pairing_from_screen(server):
    request_file = getattr(server, "pairing_rotate_request_file", None)
    pairing = getattr(server, "pairing_auth", None)
    if pairing is None or request_file is None or not request_file.exists():
        return
    try:
        request_file.unlink()
    except FileNotFoundError:
        return
    pairing.rotate_code()
    _sync_pairing_display(server)


def is_loopback_bind(address):
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def normalize_host(value):
    host = str(value).strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        if len(host) > 253 or not host:
            raise ValueError("invalid hostname")
        labels = host.split(".")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in labels):
            raise ValueError("invalid hostname")
        return host


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaoruiMusic/0.1"
    protocol_version = "HTTP/1.1"

    def _send(self, status, content_type, body, headers=None):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _unauthorized(self):
        message = ("需要先用一次性配对码绑定此浏览器"
                   if getattr(self.server, "pairing_auth", None) else "需要音乐控制器访问口令")
        body = json.dumps({"ok": False, "error": message},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(401)
        if not getattr(self.server, "pairing_auth", None):
            self.send_header("WWW-Authenticate", 'Basic realm="Xiaorui Music", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        pairing = getattr(self.server, "pairing_auth", None)
        if pairing is not None:
            # The loopback watchdog needs a credential-free liveness probe;
            # browser control itself always requires a paired session.
            if (self.path == "/health"
                    and self.client_address[0] in ("127.0.0.1", "::1")):
                return True
            if (urllib.parse.urlsplit(self.path).path == "/api/pairing/qr"
                    and self.client_address[0] in ("127.0.0.1", "::1")):
                return True
            if pairing.authorize(self._session_token()):
                return True
            self._unauthorized()
            return False
        password = getattr(self.server, "music_access_password", None)
        if password is None:
            return True
        # Keep the local watchdog health probe usable without credentials. No
        # player, library, or control data is returned by this endpoint.
        if self.path == "/health" and self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            self._unauthorized()
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, separator, supplied = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            username, separator, supplied = "", "", ""
        valid = (separator == ":" and username == "xiaorui"
                 and hmac.compare_digest(supplied.encode("utf-8"), password))
        if not valid:
            self._unauthorized()
            return False
        return True

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("XiaoruiSession")
        return morsel.value if morsel else None

    def _pairing_public_route(self):
        # The static shell contains no library or control data. Unpaired
        # browsers need it to render the pairing-code dialog; all music APIs
        # remain protected by _authorized().
        path = urllib.parse.urlsplit(self.path).path
        return path in ("/", "/manifest.webmanifest", "/service-worker.js",
                             "/icon.svg", "/icon-192.png", "/icon-512.png",
                             "/api/pairing/status", "/api/pair")

    @staticmethod
    def _local_hostname(value):
        try:
            hostname = urllib.parse.urlsplit("//" + value).hostname
            return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        except (ValueError, TypeError):
            return False

    def _reject_nonlocal_request(self):
        host_header = self.headers.get("Host", "")
        try:
            parsed_host = urllib.parse.urlsplit("//" + host_header)
            request_host = parsed_host.hostname
            parsed_host.port  # Validate syntax/range even if this route has no Origin header.
        except ValueError:
            request_host = None
        local_host = self._local_hostname(host_header)
        allowed_hosts = getattr(self.server, "music_allowed_hosts", set())
        try:
            normalized_request_host = normalize_host(request_host) if request_host else None
        except ValueError:
            normalized_request_host = None
        if not local_host and (not normalized_request_host
                               or normalized_request_host not in allowed_hosts):
            self._send(403, "application/json; charset=utf-8",
                       '{"ok":false,"error":"Host 不在音乐控制器允许列表中"}'.encode("utf-8"))
            return True
        origin = self.headers.get("Origin")
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            expected_scheme = "https" if getattr(self.server, "music_tls_enabled", False) else "http"
            try:
                origin_host = normalize_host(parsed.hostname or "")
                origin_port = parsed.port
            except ValueError:
                origin_host, origin_port = "", None
            expected_host = normalized_request_host or ""
            if (parsed.scheme != expected_scheme or origin_host != expected_host
                    or origin_port != self._host_port(host_header, expected_scheme)):
                self._send(403, "application/json; charset=utf-8",
                           '{"ok":false,"error":"拒绝跨站请求"}'.encode("utf-8"))
                return True
        return False

    @staticmethod
    def _host_port(host_header, scheme):
        try:
            port = urllib.parse.urlsplit("//" + host_header).port
        except ValueError:
            return None
        return port if port is not None else (443 if scheme == "https" else 80)

    def _sync_library_sessions(self):
        # A finite track can finish between polls (or immediately after a
        # decoder error). Stop its Snapcast session before reconcile can start
        # its source again.
        with self.server.music_queue_lock:
            for _ in range(2):
                sessions = self.server.session.handle({"op": "sessions.list"})["sessions"]
                ended = self.server.library.sync_sessions(sessions)
                if not ended:
                    return
                for session_id in ended:
                    self.server.session.handle({"op": "session.stop", "session_id": session_id})
                    queue = self.server.music_queue
                    if queue.get("session_id") == session_id:
                        if queue["index"] + 1 < len(queue["track_ids"]):
                            queue["index"] += 1
                            self._start_queued_track(enforce_start_limit=False)
                        else:
                            queue["session_id"] = None
                            queue["state"] = "completed"
                            queue["error"] = None
                        self._save_queue()

    @staticmethod
    def _empty_queue():
        return {"track_ids": [], "players": [], "index": 0, "session_id": None,
                "state": "stopped", "error": None}

    def _save_queue(self):
        path = getattr(self.server, "queue_state_file", None)
        if path is None:
            return
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp, "w", encoding="utf-8") as stream:
                os.chmod(tmp, 0o600)
                json.dump(self.server.music_queue, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            print(f"[music-ui] cannot persist queue state: {exc}")
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass

    def _sync_music_activity(self):
        """Publish a short-lived local marker for the frontend FIFO reader.

        Snapclient's raw file player emits silence while a stream is idle, so the
        frontend cannot infer an active music session from FIFO bytes alone.
        The marker is refreshed by this controller and expires after a crash.
        """
        path = getattr(self.server, "music_active_file", None)
        if path is None:
            return
        queue = self.server.music_queue
        active = queue.get("state") == "playing" and bool(queue.get("session_id"))
        try:
            if active:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
                os.chmod(path, 0o600)
                os.utime(path, None)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[music-ui] cannot update music activity marker: {exc}")

    @classmethod
    def _load_queue(cls, path, library, session):
        if path is None:
            return cls._empty_queue()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return cls._empty_queue()
            track_ids, players, index = payload.get("track_ids"), payload.get("players"), payload.get("index")
            if (not isinstance(track_ids, list) or not track_ids or len(track_ids) > 20
                    or any(not isinstance(track_id, str) for track_id in track_ids)
                    or not isinstance(players, list)
                    or any(not isinstance(player, str) for player in players)
                    or not isinstance(index, int) or isinstance(index, bool)
                    or not 0 <= index < len(track_ids)):
                return cls._empty_queue()
            for track_id in track_ids:
                library.get_track(track_id)
            sessions = session.handle({"op": "sessions.list"}).get("sessions", [])
            active = next((row for row in sessions
                           if row.get("session_id") == payload.get("session_id")
                           and row.get("state") in ("playing", "paused")
                           and row.get("track_id") == track_ids[index]), None)
            if not active:
                if payload.get("state") == "completed":
                    return {"track_ids": track_ids, "players": players, "index": index,
                            "session_id": None, "state": "completed", "error": None}
                # A failed queue has no active Snapcast session, but retaining
                # it lets the UI offer an explicit retry after restart.
                if payload.get("state") == "error":
                    error = payload.get("error")
                    return {"track_ids": track_ids, "players": players, "index": index,
                            "session_id": None, "state": "error",
                            "error": error[:500] if isinstance(error, str) else "播放失败，可重试"}
                return cls._empty_queue()
            return {"track_ids": track_ids, "players": players, "index": index,
                    "session_id": active["session_id"], "state": active["state"],
                    "error": payload.get("error")}
        except (OSError, ValueError, TypeError, MusicLibraryError):
            return cls._empty_queue()

    def _authorize_music_players(self, players, startup_volume=None, enforce_start_limit=True):
        if (not isinstance(players, list) or not players
                or any(not isinstance(player_id, str) for player_id in players)):
            raise MusicLibraryError("先选择至少一台在线设备")
        current = self.server.session.handle({"op": "players.list"})
        by_id = {player["id"]: player for player in current.get("players", [])}
        unknown = [player_id for player_id in players if player_id not in by_id]
        if unknown:
            raise MusicLibraryError("设备已离线或不存在，请刷新设备列表")
        offline = [player_id for player_id in players if not by_id[player_id]["connected"]]
        if offline:
            raise MusicLibraryError("所选设备当前离线，请先恢复连接")
        loud = ([(player_id, by_id[player_id]["name"], by_id[player_id]["volume"])
                 for player_id in players
                 if by_id[player_id]["volume"] > self.server.music_max_start_volume]
                if enforce_start_limit else [])
        if loud:
            if startup_volume is None:
                details = [{"id": player_id, "name": name, "volume": volume}
                           for player_id, name, volume in loud]
                raise MusicVolumeError(details, self.server.music_max_start_volume)
            if (not isinstance(startup_volume, int) or isinstance(startup_volume, bool)
                    or not 0 <= startup_volume <= self.server.music_max_start_volume):
                raise MusicLibraryError(
                    f"启动音量必须是 0~{self.server.music_max_start_volume}% 的整数")
            # The user has explicitly confirmed a safe volume in the UI. Apply
            # it only to selected players and retain each player's mute state.
            for player_id, _name, _volume in loud:
                result = self.server.session.handle({
                    "op": "player.volume", "player_id": player_id,
                    "volume": startup_volume,
                    "muted": by_id[player_id].get("client_muted", False),
                })
                if not result.get("ok"):
                    raise MusicLibraryError(
                        f"{by_id[player_id]['name']} 调整音量失败，已取消播放："
                        f"{result.get('error', 'Snapcast 音量设置失败')}")
            # Re-read authoritative state before opening the stream.
            updated = self.server.session.handle({"op": "players.list"})
            after = {player["id"]: player for player in updated.get("players", [])}
            still_loud = [after.get(player_id, {}).get("name", player_id)
                          for player_id in players
                          if player_id not in after
                          or after[player_id]["volume"] > self.server.music_max_start_volume]
            if still_loud:
                raise MusicLibraryError("设备音量未能降到安全范围，已取消播放：" + "、".join(still_loud))
    def _start_queued_track(self, startup_volume=None, enforce_start_limit=True):
        queue = self.server.music_queue
        track_id = queue["track_ids"][queue["index"]]
        try:
            self._authorize_music_players(queue["players"], startup_volume,
                                          enforce_start_limit=enforce_start_limit)
            track = self.server.library.start(track_id)
            result = self.server.session.handle({
                "op": "session.play", "source": self.server.library.source_uri(track_id),
                "players": queue["players"], "track_id": track_id,
                "track_title": track["title"],
            })
            if not result.get("ok"):
                self.server.library.stop(track_id)
                queue["state"] = "error"
                queue["error"] = result.get("error", "无法开始曲目")
                queue["session_id"] = None
                return result
            queue["session_id"] = result["session_id"]
            queue["state"] = "playing"
            queue["error"] = None
            self._save_queue()
            return result
        except MusicVolumeError:
            # Preserve the typed error so the HTTP layer can return the same
            # confirmation response used by direct playback.
            self.server.library.stop(track_id)
            queue["state"] = "error"
            queue["error"] = "播放音量超过安全上限，需要确认安全音量后重试"
            queue["session_id"] = None
            raise
        except MusicLibraryError as exc:
            self.server.library.stop(track_id)
            queue["state"] = "error"
            queue["error"] = str(exc)
            queue["session_id"] = None
            return {"ok": False, "error": str(exc)}

    def _queue_snapshot(self):
        queue = self.server.music_queue
        tracks = {track["id"]: track for track in self.server.library.list_tracks()}
        current_id = (queue["track_ids"][queue["index"]]
                      if queue.get("track_ids") and 0 <= queue.get("index", -1) < len(queue["track_ids"])
                      else None)
        current = tracks.get(current_id, {}) if current_id else {}
        try:
            duration = self.server.library.duration(current_id) if current_id else None
        except MusicLibraryError:
            duration = None
        try:
            position = self.server.library.position(current_id) if current_id else None
        except MusicLibraryError:
            position = None
        if queue.get("state") == "completed" and duration is not None:
            position = duration
        if position is not None and duration is not None:
            position = min(position, duration)
        return {"track_ids": list(queue["track_ids"]),
                "tracks": [{"id": track_id,
                            "title": tracks.get(track_id, {}).get("title", "曲目已移除")}
                           for track_id in queue["track_ids"]],
                "players": list(queue["players"]), "index": queue["index"],
                "session_id": queue["session_id"], "state": queue["state"],
                "current_track_id": current_id,
                "position_seconds": round(position, 2) if position is not None else None,
                "duration_seconds": duration,
                "error": queue["error"]}

    def _start_queue_index(self, index, old_track=None):
        queue = self.server.music_queue
        old_session = queue.get("session_id")
        if old_track is None:
            old_track = (queue["track_ids"][queue["index"]]
                         if queue.get("track_ids") and 0 <= queue.get("index", -1) < len(queue["track_ids"])
                         else None)
        if old_session:
            self.server.session.handle({"op": "session.stop", "session_id": old_session})
        if old_track:
            self.server.library.stop(old_track)
        queue["index"] = index
        queue["session_id"] = None
        queue["state"] = "starting"
        queue["error"] = None
        result = self._start_queued_track(enforce_start_limit=False)
        self._save_queue()
        if result.get("ok"):
            result["queue"] = self._queue_snapshot()
        return result

    def do_GET(self):
        if self._reject_nonlocal_request():
            return
        request_path = urllib.parse.urlsplit(self.path).path
        pairing = getattr(self.server, "pairing_auth", None)
        if not (pairing and self._pairing_public_route()):
            if not self._authorized():
                return
        if request_path == "/api/pairing/status":
            is_local = self.client_address[0] in ("127.0.0.1", "::1")
            code = pairing.code if pairing is not None and is_local else None
            return self._send(200, "application/json; charset=utf-8", json.dumps({
                "ok": True, "enabled": pairing is not None,
                "paired": bool(pairing and pairing.authorize(self._session_token())),
                "local": is_local,
                "device_count": len(pairing.devices()) if pairing else 0,
                "code": code,
                "public_url": getattr(self.server, "music_public_url", ""),
            }))
        if urllib.parse.urlsplit(self.path).path == "/api/pairing/qr" and pairing:
            if not (self.client_address[0] in ("127.0.0.1", "::1")
                    or pairing.authorize(self._session_token())):
                return self._unauthorized()
            code = pairing.code
            public_url = getattr(self.server, "music_public_url", "")
            if not code or not public_url:
                return self._send(409, "application/json; charset=utf-8",
                                  '{"ok":false,"error":"请先生成有效配对码并配置手机访问地址"}')
            target = public_url.rstrip("/") + "/?pair=" + urllib.parse.quote(code)
            qr = pyqrcode.create(target, error="M")
            output = io.BytesIO()
            qr.svg(output, scale=5, quiet_zone=4, module_color="#10151d",
                   background="#ffffff", title=target)
            return self._send(200, "image/svg+xml; charset=utf-8", output.getvalue())
        if request_path == "/api/pairing/devices" and pairing:
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps({"ok": True, "devices": pairing.devices()}, ensure_ascii=False))
        if request_path == "/":
            page = pathlib.Path(__file__).with_name("music_ui.html").read_text(encoding="utf-8")
            return self._send(200, "text/html; charset=utf-8", page)
        static_files = {
            "/manifest.webmanifest": ("music_manifest.webmanifest", "application/manifest+json"),
            "/service-worker.js": ("music_service_worker.js", "text/javascript; charset=utf-8"),
            "/icon.svg": ("music_icon.svg", "image/svg+xml"),
            "/icon-192.png": ("music_icon_192.png", "image/png"),
            "/icon-512.png": ("music_icon_512.png", "image/png"),
        }
        if request_path in static_files:
            filename, content_type = static_files[request_path]
            body = pathlib.Path(__file__).with_name(filename).read_bytes()
            headers = {"Service-Worker-Allowed": "/"} if request_path == "/service-worker.js" else None
            return self._send(200, content_type, body, headers)
        if self.path == "/health":
            return self._send(200, "application/json", b'{"ok":true}')
        if self.path == "/api/library":
            try:
                tracks = self.server.library.list_tracks()
            except MusicLibraryError as exc:
                return self._send(503, "application/json; charset=utf-8",
                                  json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            public = [{key: track[key] for key in ("id", "title", "size_bytes", "duration_seconds")}
                      for track in tracks]
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps({"ok": True, "tracks": public}, ensure_ascii=False))
        if self.path != "/api/state":
            return self._send(404, "application/json", b'{"ok":false,"error":"not found"}')
        # Recreate realtime PCM writers after a controller restart, and remove
        # completed tracks before session reconciliation can resurrect them.
        try:
            self._sync_library_sessions()
        except MusicLibraryError as exc:
            return self._send(503, "application/json; charset=utf-8",
                              json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        reconciled = self.server.session.handle({"op": "server.reconcile"})
        if not reconciled.get("ok"):
            return self._send(503, "application/json; charset=utf-8",
                              json.dumps(reconciled, ensure_ascii=False))
        players = self.server.session.handle({"op": "players.list"})
        sessions = self.server.session.handle({"op": "sessions.list"})
        self._sync_music_activity()
        result = {"ok": True, "players": players["players"],
                  "sessions": sessions["sessions"], "reconcile": reconciled["sessions"],
                  "queue": self._queue_snapshot(),
                  "music_max_start_volume": self.server.music_max_start_volume}
        return self._send(200, "application/json; charset=utf-8",
                          json.dumps(result, ensure_ascii=False))

    def do_POST(self):
        if self._reject_nonlocal_request():
            return
        pairing = getattr(self.server, "pairing_auth", None)
        local_pairing_rotate = bool(
            pairing and self.path == "/api/pairing/rotate"
            and self.client_address[0] in ("127.0.0.1", "::1"))
        if pairing and self.path == "/api/pair":
            request = self._read_json_request()
            if isinstance(request, tuple):
                return
            if not isinstance(request, dict):
                return self._send(400, "application/json; charset=utf-8",
                                  '{"ok":false,"error":"请求必须是 JSON 对象"}')
            result = pairing.pair(request.get("code"), request.get("name"),
                                  self.client_address[0])
            if not result:
                return self._send(401, "application/json; charset=utf-8",
                                  '{"ok":false,"error":"配对码无效、已过期或尝试次数过多"}')
            _sync_pairing_display(self.server)
            secure = "; Secure" if getattr(self.server, "music_tls_enabled", False) else ""
            return self._send(200, "application/json; charset=utf-8", json.dumps({
                "ok": True, "device_id": result["device_id"]
            }), {"Set-Cookie": (f"XiaoruiSession={result['token']}; Path=/; HttpOnly; "
                                f"SameSite=Strict; Max-Age=31536000{secure}")})
        if not local_pairing_rotate and not self._authorized():
            return
        if pairing and self.path == "/api/pairing/revoke":
            request = self._read_json_request()
            device_id = request.get("device_id") if isinstance(request, dict) else None
            revoked = pairing.revoke(device_id) if isinstance(device_id, str) else False
            return self._send(200 if revoked else 404, "application/json; charset=utf-8",
                              json.dumps({"ok": revoked,
                                          "error": None if revoked else "设备不存在"}, ensure_ascii=False))
        if pairing and self.path == "/api/pairing/rotate":
            request = self._read_json_request()
            if isinstance(request, tuple):
                return
            if not isinstance(request, dict):
                return self._send(400, "application/json; charset=utf-8",
                                  '{"ok":false,"error":"请求必须是 JSON 对象"}')
            code = pairing.rotate_code()
            _sync_pairing_display(self.server)
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps({"ok": True, "code": code}))
        if self.path != "/api":
            return self._send(404, "application/json", b'{"ok":false,"error":"not found"}')
        request = self._read_json_request()
        if isinstance(request, tuple):
            return request[0]
        if not isinstance(request, dict):
            result = {"ok": False, "error": "请求必须是 JSON 对象"}
        elif request.get("op") == "library.play":
            try:
                with self.server.music_queue_lock:
                    track_id = request.get("track_id")
                    players = request.get("players")
                    self._authorize_music_players(players, request.get("startup_volume"))
                    track = self.server.library.start(track_id)
                    source = self.server.library.source_uri(track_id)
                    result = self.server.session.handle({
                        "op": "session.play", "source": source,
                        "players": players, "track_id": track_id,
                        "track_title": track["title"],
                    })
                    if result.get("ok"):
                        self.server.music_queue = {
                            "track_ids": [track_id], "players": list(players), "index": 0,
                            "session_id": result["session_id"], "state": "playing", "error": None}
                        self._save_queue()
                    else:
                        self.server.library.stop(track_id)
            except MusicVolumeError as exc:
                result = {"ok": False, "code": "volume_too_high", "error": str(exc),
                          "players": exc.players, "max_start_volume": exc.maximum}
            except MusicLibraryError as exc:
                result = {"ok": False, "error": str(exc)}
        elif request.get("op") == "queue.play":
            track_ids = request.get("track_ids")
            players = request.get("players")
            try:
                if (not isinstance(track_ids, list) or not track_ids or len(track_ids) > 20
                        or any(not isinstance(track_id, str) for track_id in track_ids)):
                    raise MusicLibraryError("队列需要 1~20 首有效曲目")
                for track_id in track_ids:
                    self.server.library.get_track(track_id)
                with self.server.music_queue_lock:
                    self._authorize_music_players(players, request.get("startup_volume"))
                    old = self.server.music_queue
                    if old.get("session_id"):
                        self.server.session.handle({"op": "session.stop",
                                                    "session_id": old["session_id"]})
                    for old_track in old.get("track_ids", []):
                        self.server.library.stop(old_track)
                    self.server.music_queue = {"track_ids": list(track_ids),
                                               "players": list(dict.fromkeys(players)),
                                               "index": 0, "session_id": None,
                                               "state": "starting", "error": None}
                    result = self._start_queued_track()
                    self._save_queue()
                    if result.get("ok"):
                        result["queue"] = self._queue_snapshot()
            except MusicVolumeError as exc:
                result = {"ok": False, "code": "volume_too_high", "error": str(exc),
                          "players": exc.players, "max_start_volume": exc.maximum}
            except MusicLibraryError as exc:
                result = {"ok": False, "error": str(exc)}
        elif request.get("op") == "queue.next":
            with self.server.music_queue_lock:
                queue = self.server.music_queue
                if not queue["track_ids"] or queue["index"] + 1 >= len(queue["track_ids"]):
                    result = {"ok": False, "error": "队列已到末尾"}
                else:
                    result = self._start_queue_index(queue["index"] + 1)
        elif request.get("op") in ("queue.previous", "queue.jump"):
            with self.server.music_queue_lock:
                queue = self.server.music_queue
                target = queue.get("index", 0) - 1 if request.get("op") == "queue.previous" else request.get("index")
                if not queue["track_ids"]:
                    result = {"ok": False, "error": "队列为空"}
                elif (not isinstance(target, int) or isinstance(target, bool)
                      or target < 0 or target >= len(queue["track_ids"])
                      or (request.get("op") == "queue.previous" and target >= queue["index"])):
                    result = {"ok": False, "error": "队列位置无效或没有上一首"}
                elif (target == queue["index"] and queue.get("state") in ("playing", "paused")
                      and queue.get("session_id")):
                    result = {"ok": True, "queue": self._queue_snapshot()}
                else:
                    result = self._start_queue_index(target)
        elif request.get("op") == "queue.remove":
            with self.server.music_queue_lock:
                queue = self.server.music_queue
                index = request.get("index")
                if (not isinstance(index, int) or isinstance(index, bool)
                        or index < 0 or index >= len(queue["track_ids"])):
                    result = {"ok": False, "error": "队列位置无效"}
                elif index < queue["index"]:
                    queue["track_ids"].pop(index)
                    queue["index"] -= 1
                    self._save_queue()
                    result = {"ok": True, "queue": self._queue_snapshot()}
                elif index > queue["index"]:
                    queue["track_ids"].pop(index)
                    self._save_queue()
                    result = {"ok": True, "queue": self._queue_snapshot()}
                else:
                    old_track = queue["track_ids"][queue["index"]]
                    remaining = list(queue["track_ids"])
                    remaining.pop(index)
                    if not remaining:
                        if queue.get("session_id"):
                            self.server.session.handle({"op": "session.stop",
                                                        "session_id": queue["session_id"]})
                        self.server.library.stop(old_track)
                        self.server.music_queue = self._empty_queue()
                        self._save_queue()
                        result = {"ok": True, "queue": self._queue_snapshot()}
                    else:
                        queue["track_ids"] = remaining
                        queue["index"] = min(index, len(remaining) - 1)
                        result = self._start_queue_index(queue["index"], old_track=old_track)
        elif request.get("op") == "queue.retry":
            with self.server.music_queue_lock:
                queue = self.server.music_queue
                if not queue["track_ids"] or queue.get("state") != "error":
                    result = {"ok": False, "error": "当前没有可重试的失败曲目"}
                else:
                    try:
                        result = self._start_queued_track(request.get("startup_volume"))
                    except MusicVolumeError as exc:
                        result = {"ok": False, "code": "volume_too_high", "error": str(exc),
                                  "players": exc.players, "max_start_volume": exc.maximum}
                    except MusicLibraryError as exc:
                        result = {"ok": False, "error": str(exc)}
                    self._save_queue()
                    if result.get("ok"):
                        result["queue"] = self._queue_snapshot()
        elif request.get("op") == "queue.clear":
            with self.server.music_queue_lock:
                queue = self.server.music_queue
                if queue.get("session_id"):
                    self.server.session.handle({"op": "session.stop",
                                                "session_id": queue["session_id"]})
                for track_id in queue.get("track_ids", []):
                    self.server.library.stop(track_id)
                self.server.music_queue = self._empty_queue()
                self._save_queue()
                result = {"ok": True, "queue": self._queue_snapshot()}
        elif request.get("op") == "players.volume":
            player_ids = request.get("player_ids")
            volume = request.get("volume")
            if (not isinstance(player_ids, list) or not player_ids
                    or any(not isinstance(player_id, str) for player_id in player_ids)):
                result = {"ok": False, "error": "先选择至少一台设备"}
            elif (not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100):
                result = {"ok": False, "error": "音量必须是 0~100 的整数"}
            else:
                player_ids = list(dict.fromkeys(player_ids))
                current = self.server.session.handle({"op": "players.list"}).get("players", [])
                by_id = {player["id"]: player for player in current}
                missing = [player_id for player_id in player_ids if player_id not in by_id]
                offline = [player_id for player_id in player_ids
                           if player_id in by_id and not by_id[player_id]["connected"]]
                if missing or offline:
                    result = {"ok": False, "error": "所选设备已离线或不存在，未更改音量",
                              "missing": missing, "offline": offline, "applied": []}
                else:
                    applied, failed = [], []
                    for player_id in player_ids:
                        changed = self.server.session.handle({"op": "player.volume",
                                                              "player_id": player_id,
                                                              "volume": volume})
                        (applied if changed.get("ok") else failed).append(
                            {"player_id": player_id, "error": changed.get("error")})
                    result = {"ok": True, "volume": volume,
                              "applied": [row["player_id"] for row in applied],
                              "failed": failed}
        elif request.get("op") in ("session.pause", "session.resume"):
            session_id = request.get("session_id")
            sessions = self.server.session.handle({"op": "sessions.list"}).get("sessions", [])
            session = next((row for row in sessions if row.get("session_id") == session_id), None)
            if session and session.get("track_id"):
                try:
                    action = "pause" if request["op"] == "session.pause" else "resume"
                    getattr(self.server.library, action)(session["track_id"])
                    result = self.server.session.handle(request)
                except MusicLibraryError as exc:
                    result = {"ok": False, "error": str(exc)}
            else:
                result = self.server.session.handle(request)
            if result.get("ok"):
                with self.server.music_queue_lock:
                    queue = self.server.music_queue
                    if queue.get("session_id") == session_id:
                        queue["state"] = "paused" if request["op"] == "session.pause" else "playing"
                        self._save_queue()
        else:
            result = self.server.session.handle(request)
            if request.get("op") == "session.stop" and result.get("ok"):
                with self.server.music_queue_lock:
                    queue = self.server.music_queue
                    if queue.get("session_id") == request.get("session_id"):
                        for track_id in queue.get("track_ids", []):
                            self.server.library.stop(track_id)
                        self.server.music_queue = self._empty_queue()
                        self._save_queue()
        try:
            self._sync_library_sessions()
        except MusicLibraryError as exc:
            if result.get("ok"):
                result = {"ok": False, "error": f"播放状态同步失败: {exc}"}
        self._sync_music_activity()
        status = 200 if result.get("ok") else (409 if result.get("code") == "volume_too_high" else 400)
        return self._send(status,
                          "application/json; charset=utf-8",
                          json.dumps(result, ensure_ascii=False))

    def _read_json_request(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 65536:
                raise ValueError("请求长度必须在 1~65536 字节")
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, "application/json; charset=utf-8",
                       json.dumps({"ok": False, "error": f"无效 JSON: {exc}"}, ensure_ascii=False))
            return (None,)

    def log_message(self, fmt, *args):
        print("[music-ui] " + fmt % args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("XR_MUSIC_UI_BIND", "127.0.0.1"),
                        help="默认只监听本机；公开到 LAN 前请先加认证/防火墙")
    parser.add_argument("--port", type=int, default=int(os.environ.get("XR_MUSIC_UI_PORT", "8765")))
    parser.add_argument("--snapcast-host", default=os.environ.get("XR_SNAPCAST_CONTROL_HOST", "127.0.0.1"))
    parser.add_argument("--snapcast-port", type=int, default=int(os.environ.get("XR_SNAPCAST_CONTROL_PORT", "1780")))
    default_state = pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state")) / "xiaorui-music/sessions.json"
    parser.add_argument("--state-file", default=str(default_state))
    parser.add_argument("--queue-state-file", default=None,
                        help="队列状态文件；默认与 --state-file 同目录的 queue.json")
    parser.add_argument("--music-root", default=os.environ.get(
        "XR_MUSIC_LIBRARY", str(pathlib.Path.home() / "musics")))
    parser.add_argument("--music-fifo-root", default=os.environ.get(
        "XR_MUSIC_FIFO_ROOT", "/tmp/xiaorui-music-pipes"))
    parser.add_argument("--music-active-file", default=os.environ.get(
        "XR_MUSIC_ACTIVE_FILE", "/data/xiaorui/snapcast/music.active"))
    parser.add_argument("--ffmpeg", default=os.environ.get("XR_FFMPEG"))
    parser.add_argument("--music-max-start-volume", type=int,
                        default=int(os.environ.get("XR_MUSIC_MAX_START_VOLUME", "35")),
                        help="音乐启动前允许的最高 Snapcast 软件音量百分比")
    parser.add_argument("--tls-cert", default=os.environ.get("XR_MUSIC_UI_TLS_CERT"),
                        help="启用 HTTPS 所需的 PEM 证书")
    parser.add_argument("--tls-key", default=os.environ.get("XR_MUSIC_UI_TLS_KEY"),
                        help="启用 HTTPS 所需的 PEM 私钥")
    parser.add_argument("--access-password-file",
                        default=os.environ.get("XR_MUSIC_UI_PASSWORD_FILE"),
                        help="启用局域网访问时的口令文件（至少 24 字节，权限不可被组/其他用户读取）")
    parser.add_argument("--pairing-state-file", default=os.environ.get("XR_MUSIC_UI_PAIRING_STATE"),
                        help="启用一次性配对码与可撤销浏览器会话；状态文件应仅服务用户可读")
    parser.add_argument("--pairing-code-file", default=os.environ.get("XR_MUSIC_UI_PAIRING_CODE"),
                        help="配对码文件；默认使用配对状态文件同目录 pairing-code.json")
    parser.add_argument("--pairing-display-file", default=os.environ.get("XR_MUSIC_UI_PAIRING_DISPLAY_FILE"),
                        help="供板载屏幕读取的局域网扫码信息文件")
    parser.add_argument("--pairing-rotate-request-file",
                        default=os.environ.get("XR_MUSIC_UI_PAIRING_ROTATE_REQUEST_FILE"),
                        help="板载屏幕请求刷新一次性配对码的标记文件")
    parser.add_argument("--allowed-hosts", default=os.environ.get("XR_MUSIC_UI_ALLOWED_HOSTS", ""),
                        help="允许的 Host 名称/IP，逗号分隔；0.0.0.0 绑定时必填")
    parser.add_argument("--public-url", default=os.environ.get("XR_MUSIC_UI_PUBLIC_URL", ""),
                        help="手机打开控制页使用的固定 HTTPS 地址，例如 https://xiaorui-music.local:8765")
    args = parser.parse_args()
    remote_bind = not is_loopback_bind(args.bind)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert 和 --tls-key 必须同时设置")
    if args.pairing_code_file and not args.pairing_state_file:
        parser.error("--pairing-code-file 必须与 --pairing-state-file 一起设置")
    if args.pairing_state_file and args.access_password_file:
        parser.error("配对认证与 Basic 口令认证不能同时启用")
    if remote_bind and not (args.tls_cert and args.tls_key
                            and (args.access_password_file or args.pairing_state_file)):
        parser.error("局域网绑定必须配置 HTTPS，并启用口令文件或配对认证")
    if remote_bind and not args.allowed_hosts.strip():
        parser.error("局域网绑定必须配置 --allowed-hosts，防止 DNS rebinding")
    if remote_bind and args.public_url and not args.public_url.startswith("https://"):
        parser.error("局域网公开地址必须使用 HTTPS")
    if not 0 <= args.music_max_start_volume <= 100:
        parser.error("--music-max-start-volume 必须在 0~100")
    if args.public_url:
        try:
            public = urllib.parse.urlsplit(args.public_url)
            public.port
        except ValueError:
            parser.error("--public-url 地址或端口无效")
        if (public.scheme not in ("http", "https") or not public.hostname
                or public.path not in ("", "/")
                or public.username or public.password or public.query or public.fragment):
            parser.error("--public-url 必须是根路径、无凭据/查询参数的 http(s) URL")
    password = None
    if args.access_password_file:
        password_path = pathlib.Path(args.access_password_file).expanduser()
        try:
            mode = stat.S_IMODE(password_path.stat().st_mode)
            password = password_path.read_bytes().strip()
        except OSError as exc:
            parser.error(f"无法读取口令文件: {exc}")
        if mode & 0o077:
            parser.error("口令文件权限过宽；请设置为 chmod 600")
        if len(password) < 24:
            parser.error("访问口令至少需要 24 字节随机字符")
        args.access_password_file = str(password_path)
    pairing = None
    if args.pairing_state_file:
        state_path = pathlib.Path(args.pairing_state_file).expanduser()
        code_path = (pathlib.Path(args.pairing_code_file).expanduser()
                     if args.pairing_code_file else state_path.with_name("pairing-code.json"))
        for path in (state_path, code_path):
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                continue
            except OSError as exc:
                parser.error(f"无法检查配对文件 {path}: {exc}")
            if mode & 0o077:
                parser.error(f"配对文件权限过宽；请对 {path} 设置 chmod 600")
        pairing = PairingAuth(state_path, code_path)
        args.pairing_state_file = str(state_path)
        args.pairing_code_file = str(code_path)
        display_path = (pathlib.Path(args.pairing_display_file).expanduser()
                        if args.pairing_display_file else
                        state_path.with_name("pairing-display.txt"))
        rotate_path = (pathlib.Path(args.pairing_rotate_request_file).expanduser()
                       if args.pairing_rotate_request_file else
                       state_path.with_name("pairing-rotate.request"))
        args.pairing_display_file = str(display_path)
        args.pairing_rotate_request_file = str(rotate_path)
    allowed_hosts = set()
    for item in args.allowed_hosts.split(","):
        host = item.strip()
        if not host:
            continue
        try:
            host = normalize_host(host)
        except ValueError:
            parser.error(f"无效的允许 Host: {item!r}")
        allowed_hosts.add(host)
    if args.public_url:
        try:
            allowed_hosts.add(normalize_host(urllib.parse.urlsplit(args.public_url).hostname or ""))
        except ValueError:
            parser.error("--public-url 主机名无效")
    if not is_loopback_bind(args.bind):
        try:
            bind_ip = ipaddress.ip_address(args.bind)
            if not bind_ip.is_unspecified:
                allowed_hosts.add(str(bind_ip))
        except ValueError:
            try:
                allowed_hosts.add(normalize_host(args.bind))
            except ValueError:
                parser.error(f"无效的绑定 Host: {args.bind!r}")
    if args.tls_cert:
        args.tls_cert = str(pathlib.Path(args.tls_cert).expanduser())
        args.tls_key = str(pathlib.Path(args.tls_key).expanduser())
        for path in (args.tls_cert, args.tls_key):
            if not pathlib.Path(path).is_file():
                parser.error(f"TLS 文件不存在: {path}")
        key_mode = stat.S_IMODE(pathlib.Path(args.tls_key).stat().st_mode)
        if key_mode & 0o077:
            parser.error("TLS 私钥文件权限过宽；请设置为 chmod 600")
    client = SnapcastClient(args.snapcast_host, args.snapcast_port)
    session = MusicSession(control=client, state_file=args.state_file)
    library = MusicLibrary(args.music_root, args.music_fifo_root, args.ffmpeg)
    Handler.protocol_version = "HTTP/1.1"
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.music_access_password = password
    server.pairing_auth = pairing
    server.music_allowed_hosts = allowed_hosts
    server.music_tls_enabled = bool(args.tls_cert)
    server.music_public_url = args.public_url.rstrip("/")
    server.pairing_display_file = (pathlib.Path(args.pairing_display_file)
                                   if args.pairing_display_file else None)
    server.pairing_rotate_request_file = (pathlib.Path(args.pairing_rotate_request_file)
                                          if args.pairing_rotate_request_file else None)
    server.pairing_display_value = None
    _sync_pairing_display(server)
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as exc:
            parser.error(f"无法加载 TLS 证书/私钥: {exc}")
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.session = session
    server.library = library
    server.music_max_start_volume = args.music_max_start_volume
    server.music_queue_lock = threading.RLock()
    server.queue_state_file = (pathlib.Path(args.queue_state_file).expanduser()
                               if args.queue_state_file else
                               pathlib.Path(args.state_file).expanduser().with_name("queue.json"))
    server.music_queue = Handler._load_queue(server.queue_state_file, library, session)
    server.music_active_file = pathlib.Path(args.music_active_file).expanduser()
    # Handler owns the exact same maintenance method used by HTTP requests; a
    # tiny private instance lets the supervisor run it without fake HTTP calls.
    helper = object.__new__(Handler)
    helper.server = server
    helper._sync_music_activity()
    server.maintenance_stop = threading.Event()

    def maintain_sessions():
        next_reconcile = 0.0
        while not server.maintenance_stop.wait(0.5):
            try:
                _rotate_pairing_from_screen(server)
                _sync_pairing_display(server)
                helper._sync_library_sessions()
                now = time.monotonic()
                if now >= next_reconcile:
                    reconciled = server.session.handle({"op": "server.reconcile"})
                    if not reconciled.get("ok"):
                        print(f"[music-ui] Snapcast reconnect reconciliation failed: "
                              f"{reconciled.get('error', 'unknown error')}")
                    next_reconcile = now + 1.0
                helper._sync_music_activity()
            except (MusicLibraryError, OSError) as exc:
                print(f"[music-ui] background session sync failed: {exc}")

    maintenance = threading.Thread(target=maintain_sessions,
                                   name="music-session-maintenance", daemon=True)
    scheme = "https" if args.tls_cert else "http"
    print(f"小瑞音乐 UI: {scheme}://{args.bind}:{args.port}/; Snapserver={args.snapcast_host}:{args.snapcast_port}")
    try:
        maintenance.start()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.maintenance_stop.set()
        if maintenance.is_alive():
            maintenance.join(timeout=2)
        library.close()
        server.server_close()


if __name__ == "__main__":
    main()

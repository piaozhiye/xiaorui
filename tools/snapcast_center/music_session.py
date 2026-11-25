#!/usr/bin/env python3
"""MusicSession: 计划里 §4 的那层统一控制 API。

UI / 语音端只依赖这一层, 不直接碰 Snapcast 的协议细节:
    {"op":"players.list"}
    {"op":"session.play","source":"...","players":["id1","id2"]}
    {"op":"session.pause","session_id":"..."}
    {"op":"session.resume","session_id":"..."}
    {"op":"session.stop","session_id":"..."}
    {"op":"player.volume","player_id":"...","volume":65}

handle() 永远返回 dict, 不抛异常:
    成功 -> {"ok": True, ...}
    失败 -> {"ok": False, "error": "..."}

source 的写法:
    pipe:///abs/path?name=demo         命名管道(推荐: 由 ffmpeg -re 实时灌 PCM)
    file:///abs/path.pcm?name=demo      裸 PCM 文件(服务端直接读)
    http://...                         服务端能直接拉的地址

注意:
  - 服务端要求 URI 里有 ?name=<流名>; 流 id 就是这个 name。缺了会被服务端拒
    ("Stream name must not be empty")。这里会自动补上。
  - **服务端不重采样**: 流的 sampleformat 就是客户端收到的格式, 音源本身必须已经是
    那个格式(转换用 ffmpeg 在源侧做, 例如 -f s16le -ar 24000 -ac 1)。
  - 生产别用 file:// 源: 周期性 hard sync 可能插入约 50ms 静音。
    用 pipe:// + ffmpeg -re。
"""
import itertools
import json
import os
import pathlib
import tempfile
import threading
import urllib.parse
import uuid

from snapcast_control import SnapcastClient, SnapcastError


class MusicSession:
    """统一控制面。

    线程安全: 状态变更都在 _lock 里; 控制面通常会跑在多线程 HTTP 服务里。
    """

    def __init__(self, control=None, session_id_factory=None, state_file=None):
        self.control = control or SnapcastClient()
        self._sessions = {}
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._new_id = session_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._state_file = pathlib.Path(state_file).expanduser() if state_file else None
        self._load_state()

    # ---------- 统一入口 ----------

    def handle(self, request):
        if not isinstance(request, dict):
            return {"ok": False, "error": "请求必须是对象"}
        op = request.get("op")
        handler = getattr(self, "_op_" + str(op).replace(".", "_"), None) if op else None
        if handler is None:
            return {"ok": False, "error": f"未知 op: {op!r}"}
        try:
            # HTTP requests are served concurrently. Serialize operations so
            # two simultaneous play/reconcile calls cannot create competing
            # sessions or move the same client between groups out of order.
            with self._lock:
                return handler(request)
        except SnapcastError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # 兜底: 控制面不能被 UI 的坏输入打挂
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---------- op 实现 ----------

    def _op_players_list(self, _request):
        players = self.control.players()
        return {"ok": True, "players": players}

    def _op_sessions_list(self, _request):
        with self._lock:
            sessions = [dict(session_id=sid, **session)
                        for sid, session in self._sessions.items()]
        return {"ok": True, "sessions": sessions}

    def _op_server_reconcile(self, _request):
        """Reapply desired active sessions after a Snapserver/client reconnect.

        Snapserver does not persist dynamic streams or group assignments across a
        server restart. The UI polls this operation, so a recovered server gets
        its streams and player routing back without a manual stop/play cycle.
        """
        players = self.control.players()
        known = {p["id"]: p for p in players}
        stream_rows = self.control.streams()
        streams = {s.get("id") for s in stream_rows}
        stream_states = {s.get("id"): str(s.get("status", "")).lower()
                         for s in stream_rows}
        with self._lock:
            sessions = [(sid, dict(value)) for sid, value in self._sessions.items()
                        if value.get("state") in ("playing", "paused")]
        results = []
        for sid, session in sessions:
            wanted = session.get("players", [])
            available = [pid for pid in wanted if pid in known]
            connected = [pid for pid in available if known[pid]["connected"]]
            if not connected:
                results.append({"session_id": sid, "state": "waiting_for_players"})
                continue
            stream_id = session["stream_id"]
            if stream_id not in streams:
                added = self.control.add_stream(session["source"])
                added_id = added.get("id") if isinstance(added, dict) else None
                if added_id and added_id != stream_id:
                    raise SnapcastError(
                        f"恢复流 ID 不匹配: 期望 {stream_id}, 服务端返回 {added_id}")
                streams.add(stream_id)
            connected_groups = {known[pid]["group_id"] for pid in connected}
            current_ok = (
                all(known[pid]["stream_id"] == stream_id for pid in connected)
                and len(connected_groups) == 1
                and set(known[connected[0]]["group_clients"]) == set(available)
            )
            if not current_ok:
                # Keep known but currently disconnected members in the desired
                # group when the server still reports them; otherwise retry on
                # a later poll after they reconnect.
                group_id = self.control.assign_players_to_stream(available, stream_id)
                with self._lock:
                    if sid in self._sessions:
                        self._sessions[sid]["group_id"] = group_id
                        self._save_state()
            if (session["state"] == "paused" and not session.get("track_id")
                    and stream_states.get(stream_id) != "paused"):
                self.control.stream_control(stream_id, "pause")
            results.append({"session_id": sid, "state": session["state"],
                            "stream_id": stream_id,
                            "players_connected": connected,
                            "restored": not current_ok})
        return {"ok": True, "sessions": results}

    def _op_session_play(self, request):
        source = (request.get("source") or "").strip()
        wanted = request.get("players") or []
        if not source:
            return {"ok": False, "error": "缺少 source"}
        if not wanted:
            return {"ok": False, "error": "缺少 players"}

        known = {p["id"]: p for p in self.control.players()}
        by_name = {p["name"]: p for p in known.values()}
        resolved, unknown = [], []
        for item in wanted:
            if item in known:
                resolved.append(item)
            elif item in by_name:
                resolved.append(by_name[item]["id"])
            else:
                unknown.append(item)
        if unknown:
            return {"ok": False, "error": f"未知设备: {', '.join(unknown)}"}

        stream_id = self._stream_id(source)
        uri = self._prepare_source(source, stream_id)
        resolved = list(dict.fromkeys(resolved))

        # Reuse a tracked session when the user selects more devices for the
        # same stream. Keeping one owner per stream prevents duplicate session
        # records from racing during the periodic reconcile performed by the UI.
        with self._lock:
            same_stream = next(((sid, dict(value)) for sid, value in self._sessions.items()
                                if value.get("state") in ("playing", "paused")
                                and (value.get("stream_id") == stream_id
                                     or value.get("source") == uri)), None)
        if same_stream:
            session_id, session = same_stream
            if session.get("source") != uri:
                return {"ok": False,
                        "error": f"流 {stream_id!r} 已由另一个音源 URI 使用，请先停止原会话"}
            stream_id = session["stream_id"]
            merged_players = list(dict.fromkeys(session.get("players", []) + resolved))
            group_id = self.control.assign_players_to_stream(merged_players, stream_id)
            with self._lock:
                current = self._sessions.get(session_id)
                if current:
                    current["players"] = merged_players
                    current["group_id"] = group_id
                    for key in ("track_id", "track_title"):
                        if isinstance(request.get(key), str) and request[key]:
                            current[key] = request[key]
                    self._save_state()
            return {"ok": True, "session_id": session_id, "stream_id": stream_id,
                    "group_id": group_id, "players": merged_players, "reused": True}

        # 流已经在 server 上就别重复加
        existing = {s.get("id") for s in self.control.streams()}
        if stream_id not in existing:
            result = self.control.add_stream(uri)
            # 服务端回 {"id": "<流 id>"}, 以它为准
            if isinstance(result, dict) and result.get("id"):
                stream_id = result["id"]
        group_id = self.control.assign_players_to_stream(resolved, stream_id)

        # A device can belong to only one group/stream at a time. Remove the
        # moved devices from older desired sessions so their next reconcile
        # cannot steal them back. Devices left behind keep the old session.
        moved = set(resolved)
        with self._lock:
            overlapping = [(sid, dict(value)) for sid, value in self._sessions.items()
                           if value.get("state") in ("playing", "paused")
                           and value.get("stream_id") != stream_id
                           and moved.intersection(value.get("players", []))]
        warnings = []
        for old_id, old in overlapping:
            remaining = [pid for pid in old.get("players", []) if pid not in moved]
            with self._lock:
                current = self._sessions.get(old_id)
                if current:
                    current["players"] = remaining
                    # Prevent a failed repair from reclaiming the moved devices
                    # on the next UI poll. A user can retry stop on this record.
                    current["state"] = "unknown"
                    self._save_state()
            if remaining:
                try:
                    old_group = self.control.assign_players_to_stream(remaining,
                                                                       old["stream_id"])
                except SnapcastError as exc:
                    warnings.append(f"旧会话 {old_id} 剩余设备恢复失败: {exc}")
                    continue
                with self._lock:
                    current = self._sessions.get(old_id)
                    if current:
                        current["group_id"] = old_group
                        current["state"] = old["state"]
                        self._save_state()
            else:
                errors = []
                try:
                    self.control.stream_control(old["stream_id"], "stop")
                except SnapcastError as exc:
                    errors.append(f"stop: {exc}")
                try:
                    self.control.remove_stream(old["stream_id"])
                except SnapcastError as exc:
                    errors.append(f"remove: {exc}")
                with self._lock:
                    if errors:
                        warnings.append(f"旧会话 {old_id} 清理失败: {'; '.join(errors)}")
                    else:
                        self._sessions.pop(old_id, None)
                    self._save_state()

        session_id = self._new_id()
        with self._lock:
            self._sessions[session_id] = {
                "stream_id": stream_id,
                "source": uri,
                "players": resolved,
                "group_id": group_id,
                "state": "playing",
            }
            for key in ("track_id", "track_title"):
                value = request.get(key)
                if isinstance(value, str) and value:
                    self._sessions[session_id][key] = value
            self._save_state()
        return {
            "ok": True,
            "session_id": session_id,
            "stream_id": stream_id,
            "group_id": group_id,
            "players": resolved,
            "warnings": warnings,
        }

    def _op_session_pause(self, request):
        return self._control_stream(request, "pause")

    def _op_session_resume(self, request):
        return self._control_stream(request, "play")

    def _op_session_stop(self, request):
        session_id = request.get("session_id") or ""
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {"ok": False, "error": f"未知 session_id: {session_id!r}"}
        stop_warning = None
        try:
            self.control.stream_control(session["stream_id"], "stop")
        except SnapcastError as exc:
            # Some sources do not implement Stream.Control(stop). Removing the
            # stream is the authoritative stop operation below.
            stop_warning = f"stop: {exc}"
        remove_error = None
        try:
            self.control.remove_stream(session["stream_id"])
        except SnapcastError as exc:
            remove_error = f"remove_stream: {exc}"
        if remove_error:
            # 摘流失败 = 服务端可能还在放, 不能报成功(否则 UI 显示停了实际还在响)
            with self._lock:
                session["state"] = "unknown"
            return {"ok": False, "session_id": session_id, "state": "unknown",
                    "error": "; ".join(x for x in (stop_warning, remove_error) if x)}
        with self._lock:
            session["state"] = "stopped"
            self._sessions.pop(session_id, None)
            self._save_state()
        result = {"ok": True, "session_id": session_id, "state": "stopped"}
        if stop_warning:
            result["warnings"] = [stop_warning]
        return result

    def _op_player_volume(self, request):
        player_id = request.get("player_id")
        volume = request.get("volume")
        if not player_id:
            return {"ok": False, "error": "缺少 player_id"}
        try:
            percent = int(volume)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"音量不是数字: {volume!r}"}
        if not 0 <= percent <= 100:
            return {"ok": False, "error": f"音量必须在 0~100, 收到 {percent}"}
        muted = request.get("muted")
        if muted is not None and not isinstance(muted, bool):
            return {"ok": False, "error": "muted 必须是布尔值"}
        self.control.set_volume(player_id, percent, muted=muted)
        return {"ok": True, "player_id": player_id, "volume": percent, "muted": muted}

    def _op_group_mute(self, request):
        group_id = request.get("group_id")
        muted = request.get("muted")
        if not isinstance(group_id, str) or not group_id:
            return {"ok": False, "error": "缺少 group_id"}
        if not isinstance(muted, bool):
            return {"ok": False, "error": "muted 必须是布尔值"}
        if not any(player.get("group_id") == group_id for player in self.control.players()):
            return {"ok": False, "error": "播放组不存在"}
        self.control.group_set_mute(group_id, muted)
        return {"ok": True, "group_id": group_id, "muted": muted}

    def _op_player_name(self, request):
        player_id = request.get("player_id")
        name = request.get("name")
        if not isinstance(player_id, str) or not player_id:
            return {"ok": False, "error": "缺少 player_id"}
        if not isinstance(name, str):
            return {"ok": False, "error": "设备名称必须是文本"}
        name = name.strip()
        if not name or len(name) > 32 or any(ord(char) < 32 for char in name):
            return {"ok": False, "error": "设备名称需为 1~32 个可见字符"}
        if not any(player["id"] == player_id for player in self.control.players()):
            return {"ok": False, "error": "设备不存在"}
        self.control.set_name(player_id, name)
        return {"ok": True, "player_id": player_id, "name": name}

    # ---------- 工具 ----------

    def _control_stream(self, request, command):
        with self._lock:
            session_id = request.get("session_id") or ""
            session = self._sessions.get(session_id)
        if not session:
            return {"ok": False, "error": f"未知 session_id: {session_id!r}"}
        # Snapcast pipe streams report canPause=false. Music-library streams
        # pause their ffmpeg source process in the HTTP controller instead.
        if not session.get("track_id"):
            self.control.stream_control(session["stream_id"], command)
        state = "paused" if command == "pause" else "playing"
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["state"] = state
                self._save_state()
        return {"ok": True, "session_id": session_id, "state": state}

    def _stream_id(self, source):
        """流 id: URI 里 ?name= 的值; 没有就生成一个。"""
        query = urllib.parse.urlparse(source).query
        name = urllib.parse.parse_qs(query).get("name")
        if name and name[0]:
            return name[0]
        return f"session-{next(self._seq)}"

    @staticmethod
    def _prepare_source(source, stream_id):
        """服务端要求 URI 里有 ?name=<流名>(流 id 就是它), 没有会被拒。"""
        if "name=" in source:
            return source
        sep = "&" if "?" in source else "?"
        return f"{source}{sep}name={urllib.parse.quote(stream_id)}"

    def sessions(self):
        with self._lock:
            return dict(self._sessions)

    def _load_state(self):
        if not self._state_file:
            return
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            sessions = payload.get("sessions", {}) if isinstance(payload, dict) else {}
            if not isinstance(sessions, dict):
                return
            for sid, session in sessions.items():
                if (isinstance(sid, str) and isinstance(session, dict)
                        and session.get("state") in ("playing", "paused")
                        and isinstance(session.get("stream_id"), str)
                        and isinstance(session.get("source"), str)
                        and isinstance(session.get("players"), list)):
                    self._sessions[sid] = session
        except (OSError, ValueError, TypeError):
            # A corrupt state file must not prevent the control UI from starting.
            return

    def _save_state(self):
        if not self._state_file:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "sessions": self._sessions},
                             ensure_ascii=False, separators=(",", ":"))
        fd, tmp_name = tempfile.mkstemp(prefix=".music-sessions-",
                                       dir=str(self._state_file.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._state_file)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

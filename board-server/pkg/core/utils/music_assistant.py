"""Small Music Assistant WebSocket client for voice playback controls.

Authentication is supplied by environment variables so credentials stay out of
the shared gateway YAML.  The normal MA server WebSocket API is used; playback
continues to be rendered by the MA player (Snapcast in this installation).
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from urllib.parse import urlsplit, urlunsplit

try:
    from websockets.sync.client import connect as websocket_connect
except ImportError:  # pragma: no cover - covered by deployment preflight
    websocket_connect = None


_ACTIONS = {"play", "pause", "resume", "stop", "next", "previous",
            "volume_set", "volume_up", "volume_down", "status"}
_SOURCE_PREFIXES = (
    ("QQ音乐的", "qqmusic"), ("QQ音乐", "qqmusic"),
    ("网易云音乐", "netease"), ("网易云", "netease"),
)


class MusicAssistantError(RuntimeError):
    pass


def _get_config(conn):
    plugins = getattr(conn, "config", {}).get("plugins", {})
    plugin = plugins.get("music_assistant", {}) if isinstance(plugins, dict) else {}
    if not isinstance(plugin, dict):
        plugin = {}
    env_url = os.environ.get("XR_MA_URL", "").strip()
    return {
        "url": env_url or str(plugin.get("base_url", "")).strip(),
        "username": os.environ.get("XR_MA_USERNAME", ""),
        "password": os.environ.get("XR_MA_PASSWORD", ""),
        "token": os.environ.get("XR_MA_ACCESS_TOKEN", ""),
        "player_id": os.environ.get("XR_MA_PLAYER_ID", "").strip()
                     or str(plugin.get("player_id", "")).strip(),
        "player_name": os.environ.get("XR_MA_PLAYER_NAME", "").strip()
                       or str(plugin.get("player_name", "")).strip(),
        "provider_domain": os.environ.get("XR_MA_PROVIDER_DOMAIN", "").strip()
                           or str(plugin.get("provider_domain", "")).strip(),
    }


def _websocket_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise MusicAssistantError("Music Assistant 地址无效")
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/ws"
    elif not path.endswith("/ws"):
        path += "/ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


class _MAConnection:
    def __init__(self, ws, timeout=4.0):
        self.ws = ws
        self.timeout = timeout
        self.next_id = 1

    def command(self, name, args=None):
        message_id = str(self.next_id)
        self.next_id += 1
        message = {"message_id": message_id, "command": name}
        if args is not None:
            message["args"] = args
        self.ws.send(json.dumps(message, ensure_ascii=False))
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("command deadline elapsed")
                response = json.loads(self.ws.recv(timeout=remaining))
            except TimeoutError as exc:
                raise MusicAssistantError("Music Assistant 响应超时") from exc
            except (ValueError, TypeError) as exc:
                raise MusicAssistantError("Music Assistant 返回了无效响应") from exc
            if not isinstance(response, dict) or response.get("message_id") != message_id:
                # The server sends version/event messages on the same socket.
                continue
            if "error_code" in response:
                detail = str(response.get("details") or response.get("error_code"))
                raise MusicAssistantError(f"Music Assistant: {detail[:180]}")
            return response.get("result")


def _as_items(value):
    if isinstance(value, dict):
        return list(value.values())
    return value if isinstance(value, list) else []


def _source_request(query, configured_domain):
    song = query.strip()
    domain = configured_domain
    for prefix, candidate in _SOURCE_PREFIXES:
        if song.startswith(prefix):
            song = song[len(prefix):].strip(" ：:，,、")
            domain = domain or candidate
            break
    return song, domain


def _provider_ids(ma, domain):
    if not domain:
        return None
    providers = _as_items(ma.command("providers"))
    aliases = {
        "qqmusic": ("qqmusic", "qq 音乐", "qq music"),
        "netease": ("netease", "cloud music", "网易云"),
    }.get(domain, (domain.lower(),))
    matches = [p.get("instance_id") for p in providers if isinstance(p, dict)
               and p.get("type") == "music"
               and any(alias in " ".join((str(p.get("domain", "")),
                                          str(p.get("instance_id", "")),
                                          str(p.get("name", "")))).lower()
                       for alias in aliases)
               and p.get("available", True)]
    if not matches:
        raise MusicAssistantError(f"Music Assistant 中没有可用的{domain}音乐源")
    return matches


def _select_player(ma, cfg):
    players = [p for p in _as_items(ma.command("players/all")) if isinstance(p, dict)
               and p.get("available", True)
               and "snapcast" in str(p.get("provider", p.get("provider_id", ""))).lower()]
    if cfg["player_id"]:
        players = [p for p in players if p.get("player_id") == cfg["player_id"]]
    if cfg["player_name"]:
        players = [p for p in players if p.get("name") == cfg["player_name"]]
    if not players:
        raise MusicAssistantError("没有找到在线的 Snapcast 播放设备")
    queues = [q for q in _as_items(ma.command("player_queues/all")) if isinstance(q, dict)]
    if len(players) > 1:
        active_ids = set()
        for q in queues:
            if q.get("state") in ("playing", "paused", "paused".upper()):
                active_ids.add(q.get("player_id") or q.get("queue_id"))
        active = [p for p in players if p.get("player_id") in active_ids]
        if len(active) == 1:
            players = active
        else:
            raise MusicAssistantError("找到多个 Snapcast 播放设备，请设置 XR_MA_PLAYER_NAME")
    player = players[0]
    player_id = player.get("player_id")
    queue = next((q for q in queues if q.get("queue_id") == player_id
                  or q.get("player_id") == player_id), None)
    if queue is None:
        raise MusicAssistantError("Snapcast 播放设备没有可控制的播放队列")
    return player, queue


def _choose_track(results, query):
    # MA's music/search response is grouped by media type (tracks, albums,
    # artists, ...), while older API variants and test doubles may return a
    # flat list. Prefer the explicit tracks group, then retain flat-list
    # compatibility.
    grouped_tracks = results.get("tracks") if isinstance(results, dict) else None
    candidates = _as_items(grouped_tracks) if grouped_tracks is not None else _as_items(results)
    tracks = [item for item in candidates if isinstance(item, dict)
              and item.get("media_type", item.get("type", "track")) == "track"]
    if not tracks:
        raise MusicAssistantError("没有找到这首歌")
    key = re.sub(r"[\W_]+", "", query.casefold(), flags=re.UNICODE)
    for track in tracks:
        title = str(track.get("name", track.get("title", "")))
        if re.sub(r"[\W_]+", "", title.casefold(), flags=re.UNICODE) == key:
            return track
    return tracks[0]


def control(conn, action, *, query=None, volume=None):
    """Execute one voice command against the configured MA Snapcast player."""
    if action not in _ACTIONS:
        return {"ok": False, "error": "不支持的音乐操作"}
    cfg = _get_config(conn)
    if not cfg["url"]:
        return {"ok": False, "error": "未配置 Music Assistant 地址"}
    if not cfg["token"] and not (cfg["username"] and cfg["password"]):
        return {"ok": False, "error": "未配置 Music Assistant 登录信息"}
    if action == "play" and query is not None and not isinstance(query, str):
        return {"ok": False, "error": "歌曲名称无效"}
    if action == "volume_set" and (not isinstance(volume, int) or isinstance(volume, bool)
                                    or not 0 <= volume <= 100):
        return {"ok": False, "error": "音量需要是 0 到 100 的整数"}
    if websocket_connect is None:
        return {"ok": False, "error": "网关缺少 websockets 同步客户端"}
    try:
        ws_url = _websocket_url(cfg["url"])
        with websocket_connect(ws_url, open_timeout=4, close_timeout=1,
                               max_size=2 * 1024 * 1024) as ws:
            ma = _MAConnection(ws)
            if cfg["token"]:
                ma.command("auth", {"token": cfg["token"], "device_name": "小瑞语音"})
            else:
                login = ma.command("auth/login", {
                    "username": cfg["username"], "password": cfg["password"],
                    "device_name": "小瑞语音",
                })
                token = login.get("access_token", login.get("token")) if isinstance(login, dict) else None
                if not token:
                    raise MusicAssistantError("Music Assistant 登录失败")
                ma.command("auth", {"token": token, "device_name": "小瑞语音"})
            player, queue = _select_player(ma, cfg)
            player_id = player["player_id"]
            queue_id = queue["queue_id"]
            if action == "play":
                song, domain = _source_request(query or "", cfg["provider_domain"])
                provider_ids = _provider_ids(ma, domain)
                if not song or song.casefold() == "random":
                    library_args = {"limit": 100, "offset": 0}
                    if provider_ids and len(provider_ids) == 1:
                        library_args["provider"] = provider_ids[0]
                    tracks = [item for item in _as_items(
                        ma.command("music/tracks/library_items", library_args))
                        if isinstance(item, dict)]
                    if not tracks:
                        raise MusicAssistantError("Music Assistant 曲库中没有可随机播放的歌曲")
                    track = random.choice(tracks)
                else:
                    search_args = {
                        "search_query": song, "media_types": ["track"], "limit": 10,
                    }
                    if provider_ids:
                        search_args["providers"] = provider_ids
                    results = ma.command("music/search", search_args)
                    track = _choose_track(results, song)
                ma.command("player_queues/play_media", {
                    "queue_id": queue_id, "media": track, "option": "replace",
                    "start_from_beginning": True,
                })
                title = str(track.get("name", track.get("title", song)))
                return {"ok": True, "response": f"正在播放{title}"}
            if action == "status":
                item = queue.get("current_item") or {}
                title = item.get("name", "") if isinstance(item, dict) else ""
                state = str(queue.get("state", "idle")).lower()
                if not title:
                    return {"ok": True, "response": "当前没有正在播放的音乐", "active": False}
                paused = state in ("paused", "idle") or not queue.get("playing", state == "playing")
                response = f"{title}，已暂停" if paused else f"正在播放{title}"
                return {"ok": True, "response": response, "active": True, "state": state}
            if action in ("volume_set", "volume_up", "volume_down"):
                if action == "volume_set":
                    ma.command("players/cmd/volume_set", {
                        "player_id": player_id, "volume_level": volume,
                    })
                    return {"ok": True, "response": f"音量调到{volume}%了"}
                ma.command(f"players/cmd/{action}", {"player_id": player_id})
                response = "音量调大了" if action == "volume_up" else "音量调小了"
                return {"ok": True, "response": response}
            ma.command(f"player_queues/{action}", {"queue_id": queue_id})
            responses = {"pause": "好，暂停了", "resume": "好，继续播放",
                         "stop": "好，音乐停了", "next": "好，切到下一首了",
                         "previous": "好，切到上一首了"}
            return {"ok": True, "response": responses[action]}
    except MusicAssistantError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        # Do not include environment credentials or request payloads in errors.
        return {"ok": False, "error": f"Music Assistant 暂时不可用：{type(exc).__name__}"}

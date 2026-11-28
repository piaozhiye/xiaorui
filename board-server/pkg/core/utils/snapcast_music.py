"""Loopback client for the Snapcast multiroom music controller."""
import json
import ipaddress
import urllib.error
import urllib.parse
import urllib.request
import os


def _music_assistant_enabled(conn):
    plugins = conn.config.get("plugins", {})
    config = plugins.get("music_assistant", {}) if isinstance(plugins, dict) else {}
    if isinstance(config, dict) and config.get("enabled") is True:
        return True
    return os.environ.get("XR_MA_CONTROL", "").strip().lower() in {"1", "true", "yes", "on"}


def music_assistant_enabled(conn):
    """Expose the selected backend for intent routing and tests."""
    return _music_assistant_enabled(conn)


def enabled(conn):
    plugins = conn.config.get("plugins", {})
    config = plugins.get("snapcast_music", {}) if isinstance(plugins, dict) else {}
    return ((isinstance(config, dict) and config.get("enabled", False) is True)
            or _music_assistant_enabled(conn))


def control(conn, action, *, query=None, volume=None):
    """Control MA when enabled; otherwise keep the legacy local controller."""
    if _music_assistant_enabled(conn):
        from core.utils.music_assistant import control as ma_control
        return ma_control(conn, action, query=query, volume=volume)

    config = conn.config.get("plugins", {}).get("snapcast_music", {})
    base_url = str(config.get("base_url", "http://127.0.0.1:8765")).rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    try:
        host = parsed.hostname
        is_loopback = (host == "localhost" or
                       (host is not None and ipaddress.ip_address(host).is_loopback))
    except ValueError:
        is_loopback = False
    if (parsed.scheme != "http" or not is_loopback or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return {"ok": False, "error": "语音音乐控制地址必须是本机 HTTP 地址"}
    if not isinstance(action, str) or action not in {"play", "pause", "resume", "stop", "next", "previous",
                      "volume_set", "volume_up", "volume_down", "status"}:
        return {"ok": False, "error": "不支持的音乐操作"}
    request_data = {"action": action}
    if query is not None:
        if not isinstance(query, str) or len(query) > 120:
            return {"ok": False, "error": "歌曲名称无效"}
        request_data["query"] = query
    if volume is not None:
        if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100:
            return {"ok": False, "error": "音量需要是 0 到 100 的整数"}
        request_data["volume"] = volume
    data = json.dumps(request_data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url + "/api/voice", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2.5) as response:
            result = json.loads(response.read(65536).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read(65536).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            result = {"ok": False, "error": f"音乐服务返回 HTTP {exc.code}"}
    except (OSError, TimeoutError, ValueError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"音乐服务暂时不可用：{exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "error": "音乐服务响应无效"}
    return result


async def control_async(conn, action, *, query=None, volume=None):
    """Keep network control I/O off the ASR/TTS event loop."""
    import asyncio
    return await asyncio.to_thread(control, conn, action, query=query, volume=volume)


def music_active(conn):
    """Return True/False for a known queue state, or None if status is unavailable."""
    if not enabled(conn):
        return False
    result = control(conn, "status")
    if result.get("ok") is not True:
        return None
    response = result.get("response")
    if not isinstance(response, str):
        return None
    return response != "当前没有正在播放的音乐"

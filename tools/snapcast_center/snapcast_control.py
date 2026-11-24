#!/usr/bin/env python3
"""Snapcast 控制适配器: 把 Snapcast 的 JSON-RPC 包成一个薄薄的 Python 接口。

只用标准库(urllib), 不引第三方依赖, 方便直接丢到 PC 上的控制中心/网关里跑。

要点(都是实测踩出来的):
  - 控制协议走 **HTTP 1780 端口的 POST /jsonrpc**(JSON-RPC 2.0)。
    客户端自己的二进制协议在 **1704**(tcp-streaming), 1705 是 JSON-RPC 文本行,
    两者别混: 用 1705 连 snapclient 会"连上但一个字节都收不到"。
  - "谁在放什么" 靠 group 决定: 一个 group 播一个 stream。
    所以"让这几台设备听这一路" = Group.SetClients + Group.SetStream。
  - 音量是 **每客户端** 的(Client.SetVolume), 不是每 group。

用法:
    from snapcast_control import SnapcastClient
    c = SnapcastClient(host="127.0.0.1", port=1780)
    for p in c.players():
        print(p["name"], p["connected"], p["volume"])
    c.set_volume("aa:bb:cc:dd:ee:01", 65)
"""
import json
import urllib.error
import urllib.request


class SnapcastError(RuntimeError):
    """Snapcast 调用失败(服务端报错或连不上)。"""


def _as_int(value, default=0):
    """服务端可能给 null / 字符串, 统一成 int。"""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


class SnapcastClient:
    """Snapcast JSON-RPC 客户端。

    transport 参数用于注入假的实现(单测用); 传 None 时走 HTTP。
    transport 的签名是 transport(method, params) -> result。
    """

    def __init__(self, host="127.0.0.1", port=1780, timeout=5.0, transport=None):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._transport = transport or self._http_transport
        self._req_id = 0

    # ---------- 底层 ----------

    def call(self, method, params=None):
        """发一条 JSON-RPC 请求, 返回 result; 出错抛 SnapcastError。"""
        try:
            return self._transport(method, params or {})
        except SnapcastError:
            raise
        except Exception as exc:  # 连不上/超时/协议错
            raise SnapcastError(f"{method} 失败: {exc}") from exc

    def _http_transport(self, method, params):
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/jsonrpc",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise SnapcastError(f"{method} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SnapcastError(f"{method} 连不上 {self.host}:{self.port}: {exc.reason}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SnapcastError(f"{method} 返回的不是 JSON: {raw[:200]}") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise SnapcastError(err.get("message") or json.dumps(err, ensure_ascii=False))
        if isinstance(data, dict):
            return data.get("result")
        return data

    # ---------- 查询 ----------

    def server_status(self):
        return self.call("Server.GetStatus") or {}

    @staticmethod
    def _inner(status):
        """v0.35 的结构是 result = {"server": {"groups":..., "streams":...}},
        旧版本/推送通知的格式是 result = {"groups":..., "streams":...}。
        两种都认。"""
        inner = status.get("server")
        if isinstance(inner, dict) and ("groups" in inner or "streams" in inner):
            return inner
        return status

    def players(self):
        """返回所有客户端(设备)列表。

        每项: id/name/host/connected/volume/muted/latency/group_id/stream_id/version
        """
        status = self._inner(self.server_status())
        out = []
        for group in status.get("groups") or []:
            for client in group.get("clients") or []:
                cfg = client.get("config") or {}
                host = client.get("host") or {}
                vol = cfg.get("volume") or {}
                cid = client.get("id") or ""
                # 服务端可能给 null, int(None) 会炸, 统一走 _as_int
                group_muted = bool(group.get("muted"))
                out.append({
                    "id": cid,
                    "name": cfg.get("name") or host.get("name") or cid,
                    "host": host.get("ip") or "",
                    "connected": bool(client.get("connected")),
                    "volume": _as_int(vol.get("percent"), 100),
                    # 生效的静音 = 客户端自己静音 或 所在 group 静音
                    "muted": bool(vol.get("muted")) or group_muted,
                    "client_muted": bool(vol.get("muted")),
                    "group_muted": group_muted,
                    "latency": _as_int(cfg.get("latency"), 0),
                    "group_id": group.get("id"),
                    "stream_id": group.get("stream_id"),
                    "group_clients": [c.get("id") for c in group.get("clients") or []
                                      if c.get("id")],
                    "version": (client.get("snapclient") or {}).get("version", ""),
                })
        return out

    def streams(self):
        status = self._inner(self.server_status())
        return status.get("streams") or []

    # ---------- 控制 ----------

    def set_volume(self, player_id, percent, muted=None):
        """设音量。

        muted=None(默认) = 保持当前静音状态(改音量不该顺手取消静音);
        muted=True/False  = 显式设置。
        """
        percent = int(percent)
        if not 0 <= percent <= 100:
            raise SnapcastError(f"音量必须在 0~100, 收到 {percent}")
        if muted is None:
            muted = self.current_muted(player_id)
        return self.call("Client.SetVolume", {
            "id": player_id,
            "volume": {"muted": bool(muted), "percent": percent},
        })

    def current_muted(self, player_id):
        """查当前这台设备的静音状态(改音量时保持它)。查不到就当没静音。"""
        try:
            for p in self.players():
                if p["id"] == player_id:
                    return p["client_muted"]
        except SnapcastError:
            pass
        return False

    def set_latency(self, player_id, ms):
        ms = int(ms)
        if not -10000 <= ms <= 10000:
            raise SnapcastError(f"latency 超出范围(-10000~10000): {ms}")
        return self.call("Client.SetLatency", {"id": player_id, "latency": ms})

    def set_name(self, player_id, name):
        return self.call("Client.SetName", {"id": player_id, "name": name})

    def group_set_mute(self, group_id, muted):
        # 服务端读的是 "mute"(不是 "muted"), 写错会 InvalidParams
        return self.call("Group.SetMute", {"id": group_id, "mute": bool(muted)})

    def group_set_stream(self, group_id, stream_id):
        return self.call("Group.SetStream", {"id": group_id, "stream_id": stream_id})

    def group_set_clients(self, group_id, client_ids):
        return self.call("Group.SetClients", {"id": group_id, "clients": list(client_ids)})

    def add_stream(self, uri):
        return self.call("Stream.AddStream", {"streamUri": uri})

    def remove_stream(self, stream_id):
        try:
            return self.call("Stream.RemoveStream", {"id": stream_id})
        except SnapcastError as original:
            # Snapserver 0.35 may close the HTTP JSON-RPC socket while removing
            # a live pipe, after the removal has already taken effect. Treat
            # that response loss as idempotent success only after verifying
            # the stream is absent; preserve the original error otherwise.
            try:
                if not any(s.get("id") == stream_id for s in self.streams()):
                    return {"id": stream_id, "removed": True}
            except SnapcastError:
                pass
            raise original

    def stream_control(self, stream_id, command, param=None):
        # 服务端的可选项叫 "params"(对象), 例如 seek 用 {"offset": 60000}
        params = {"id": stream_id, "command": command}
        if param is not None:
            params["params"] = param if isinstance(param, dict) else {"value": param}
        return self.call("Stream.Control", params)

    # ---------- 分派 ----------

    def pick_group(self, client_ids=None):
        """给一组客户端挑一个 group: 优先用他们已经在的那个, 否则用第一个。"""
        status = self._inner(self.server_status())
        groups = status.get("groups") or []
        wanted = set(client_ids or [])
        for group in groups:
            ids = {c.get("id") for c in group.get("clients") or []}
            if ids & wanted:
                return group.get("id")
        if groups:
            return groups[0].get("id")
        raise SnapcastError("server 上还没有任何 group, 客户端可能没连上")

    def assign_players_to_stream(self, client_ids, stream_id, group_id=None):
        """让这批设备听这一路流(group 决定 stream)。"""
        if not client_ids:
            raise SnapcastError("没有指定设备")
        gid = group_id or self.pick_group(client_ids)
        self.group_set_clients(gid, client_ids)
        self.group_set_stream(gid, stream_id)
        return gid

"""provider <-> helper 进程的帧协议.

帧 = 4 字节大端长度 + JSON 头; 头里 body_len > 0 时紧跟原始字节体。
PCM 用原始体传(不走 base64), 文本走 JSON。

PC -> helper:  {"cmd": "...", ...参数, "body_len": N} + N 字节 PCM
helper -> PC:  {"ok": true, ...结果, "body_len": N} + N 字节 PCM
               {"ok": false, "error": "..."}
"""
import json
import struct

MAX_BODY = 64 * 1024 * 1024


class ProtocolError(Exception):
    pass


def send_msg(wfile, header, body=b""):
    body_len = len(body)
    if body_len > MAX_BODY:
        raise ProtocolError(f"body 过大: {body_len}")
    head = dict(header)
    head["body_len"] = body_len
    data = json.dumps(head, ensure_ascii=False).encode("utf-8")
    wfile.write(struct.pack(">I", len(data)))
    wfile.write(data)
    if body_len:
        wfile.write(body)
    wfile.flush()


def recv_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            raise ProtocolError("对端关闭 (流中断)")
        buf += chunk
    return buf


def recv_msg(rfile):
    (length,) = struct.unpack(">I", recv_exact(rfile, 4))
    if length <= 0 or length > 4 * 1024 * 1024:
        raise ProtocolError(f"帧头长度非法: {length}")
    header = json.loads(recv_exact(rfile, length).decode("utf-8"))
    body_len = header.get("body_len", 0)
    body = recv_exact(rfile, body_len) if body_len else b""
    return header, body

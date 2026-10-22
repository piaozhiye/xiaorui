#!/data/ha-env/bin/python
"""homeassistant.local 的原生 mDNS A 应答器(2026-10-19).

小米 OAuth 回调域名硬编码 homeassistant.local:8123(注册死在小米服务器),
board 无 avahi; 本脚本用 stdlib socket 直接应答该主机名的 A/ANY 查询.
zeroconf 库试过: 只播服务记录, 对纯主机名 A 查询不可靠, 弃.
部署: /data/xiaorui-server/mdns-ha.py, start-all.sh 末尾 setsid 拉起;
应答 IP: 环境变量 XR_HA_IP 优先, 未设时自动探测本机非回环 IPv4.
自测: 向 224.0.0.251:5353 发 homeassistant.local 的 A 查询, 应答本机 IP.
"""
import os
import socket
import struct
import sys

GROUP, PORT = "224.0.0.251", 5353
NAME = b"homeassistant.local"


def answer_ip():
    env = os.environ.get("XR_HA_IP")
    if env:
        return env
    try:  # 用组播目标探路由出口地址, 不实际发包
        t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t.connect((GROUP, PORT))
        ip = t.getsockname()[0]
        t.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


IP_STR = answer_ip()
IP = socket.inet_aton(IP_STR)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
except (AttributeError, OSError):
    pass
s.bind(("", PORT))
# 在全部 IPv4 接口入组(非默认路由的接口必须显式入组, 主机名枚举不到)
import psutil
ips = set()
for a in psutil.net_if_addrs().values():
    for x in a:
        if x.family == socket.AF_INET:
            ips.add(x.address)
ips.add("0.0.0.0")
print("join on:", sorted(ips), flush=True)
for ip in ips:
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(GROUP) + socket.inet_aton(ip))
    except OSError:
        pass
print("mdns responder: homeassistant.local ->", IP_STR, flush=True)


def parse_questions(data):
    if len(data) < 12:
        return None
    qd = struct.unpack("!H", data[4:6])[0]
    off, out = 12, []
    for _ in range(qd):
        parts = []
        while off < len(data):
            n = data[off]
            if n == 0:
                off += 1
                break
            parts.append(data[off + 1: off + 1 + n].lower())
            off += 1 + n
        qtype = struct.unpack("!H", data[off:off + 2])[0] if off + 2 <= len(data) else 0
        out.append((b".".join(parts), qtype))
        off += 4
    return out


while True:
    data, src = s.recvfrom(4096)
    try:
        if data[2] & 0x80:  # 只要查询
            continue
        qs = parse_questions(data)
        if not qs:
            continue
        print("q from", src, qs, flush=True)
        if not any(n == NAME and t in (1, 255) for n, t in qs):
            continue
        qd = struct.unpack("!H", data[4:6])[0]
        off = 12
        for _ in range(qd):
            while off < len(data) and data[off] != 0:
                off += 1 + data[off]
            off += 5
        question = data[12:off]
        resp = (data[:2] + struct.pack("!H", 0x8400)
                + struct.pack("!HHHH", qd, 1, 0, 0) + question
                + struct.pack("!HHIIH", 0xC00C, 1, 1, 120, 4) + IP)
        dst = src if src[1] != PORT else (GROUP, PORT)  # legacy 查询单播回
        s.sendto(resp, dst)
    except Exception as e:  # noqa: BLE001
        print("err:", e, file=sys.stderr, flush=True)

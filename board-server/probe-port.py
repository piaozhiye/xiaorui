#!/usr/bin/env python3
"""板端端口探针: 退出码 0 = 端口可连接。

为什么用文件而不是 `adb shell "python -c '...'"`:
内联引号经过 adb shell 会被吃掉, 导致远端脚本语法错误(表现为"端口不通"的
假阴性)。板端 busybox 也没有 /dev/tcp 可用(util-linux setsid 新会话里
bash 的 net redirection 失效), 所以统一用这个脚本。

用法: python3 /data/xiaorui-gateway/probe-port.py 8001
"""
import socket
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: probe.py PORT [PORT...]", file=sys.stderr)
        return 2
    worst = 0
    for raw in sys.argv[1:]:
        try:
            port = int(raw)
        except ValueError:
            print(f"invalid port: {raw}", file=sys.stderr)
            worst = 2
            continue
        sock = socket.socket()
        sock.settimeout(1)
        code = sock.connect_ex(("127.0.0.1", port))
        sock.close()
        print(f"port {port} rc={code}")
        if code != 0:
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())

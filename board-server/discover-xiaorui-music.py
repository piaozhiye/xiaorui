#!/data/ha-env/bin/python
"""Resolve the unique Xiaorui Snapserver advertised on the local mDNS link."""
import argparse
import ipaddress
import sys
import threading
import time

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE_TYPE = "_xiaorui-music._tcp.local."


class MusicServerListener(ServiceListener):
    def __init__(self, zeroconf):
        self.zeroconf = zeroconf
        self.rows = {}
        self.lock = threading.Lock()

    def add_service(self, _zeroconf, service_type, name):
        info = self.zeroconf.get_service_info(service_type, name, timeout=1500)
        if not info:
            return
        addresses = []
        for raw in info.parsed_addresses():
            try:
                parsed = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if parsed.version == 4 and not parsed.is_loopback and not parsed.is_unspecified:
                addresses.append(str(parsed))
        properties = info.properties or {}
        server_id = properties.get(b"id", b"").decode("ascii", errors="ignore")
        label = properties.get(b"name", b"Xiaorui Music").decode("utf-8", errors="replace")
        if not addresses:
            return
        with self.lock:
            self.rows[server_id or name] = {
                "id": server_id or name,
                "service_name": name,
                "name": label,
                "host": sorted(set(addresses))[0],
                "port": int(properties.get(b"snapcast_port", info.port)),
            }

    def remove_service(self, _zeroconf, _service_type, name):
        with self.lock:
            for key, row in list(self.rows.items()):
                if row.get("service_name") == name:
                    self.rows.pop(key, None)

    def update_service(self, zeroconf, service_type, name):
        self.add_service(zeroconf, service_type, name)


def discover(timeout=4.0, server_id=None):
    zeroconf = Zeroconf()
    listener = MusicServerListener(zeroconf)
    browser = ServiceBrowser(zeroconf, SERVICE_TYPE, listener)
    try:
        time.sleep(max(0.5, timeout))
        with listener.lock:
            rows = list(listener.rows.values())
    finally:
        browser.cancel()
        zeroconf.close()
    if server_id:
        rows = [row for row in rows if row["id"] == server_id]
    if len(rows) != 1:
        if not rows:
            raise RuntimeError("局域网内没有发现小瑞音乐主机")
        choices = ", ".join(f"{row['id']}={row['host']}:{row['port']}" for row in rows)
        raise RuntimeError(f"发现多个小瑞音乐主机，请配置 XR_SNAPCAST_SERVER_ID：{choices}")
    return rows[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--server-id", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        row = discover(args.timeout, args.server_id)
    except Exception as exc:
        print(f"music discovery failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        import json
        print(json.dumps(row, ensure_ascii=False))
    else:
        print(row["host"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/data/ha-env/bin/python
"""Answer mDNS A/ANY queries for board-local names.

The default ``homeassistant.local`` name preserves the Xiaomi callback setup.
Set ``XR_MDNS_ALIASES=xiaorui-music.local`` when the music UI is enabled to add
a stable same-link browser address. This is an A-record alias, not DNS-SD
service browsing; mDNS remains link-local and does not cross routers.
"""
import os
import ipaddress
import pathlib
import socket
import struct
import sys

GROUP, PORT = "224.0.0.251", 5353
BASE_NAMES = ("homeassistant.local",)


def configured_names(aliases=None, service_env_path="/data/xiaorui/snapcast/service.env"):
    if aliases is None:
        aliases = os.environ.get("XR_MDNS_ALIASES")
    if aliases is None:
        aliases = ""
        try:
            lines = pathlib.Path(service_env_path).read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        settings = {}
        for line in lines:
            key, separator, value = line.partition("=")
            if separator and key in ("XR_MUSIC_UI", "XR_MUSIC_MDNS_ALIAS"):
                settings[key] = value.strip().strip("\"'")
        if settings.get("XR_MUSIC_UI") == "1":
            aliases = settings.get("XR_MUSIC_MDNS_ALIAS") or "xiaorui-music.local"
    names = set()
    for raw in (*BASE_NAMES, *aliases.split(",")):
        name = raw.strip().lower().rstrip(".")
        labels = name.split(".")
        if (not name or len(name) > 253
                or any(not label or len(label) > 63 or not label[0].isalnum()
                       or not label[-1].isalnum()
                       or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                              for char in label)
                       for label in labels)):
            continue
        names.add(name.encode("ascii"))
    return names


def read_service_settings(service_env_path="/data/xiaorui/snapcast/service.env"):
    settings = {}
    try:
        lines = pathlib.Path(service_env_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return settings
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            settings[key.strip()] = value.strip().strip("\"'")
    return settings


def music_server_advertisement(settings):
    """Return the DNS-SD service definition only on a configured master."""
    if settings.get("XR_MUSIC_UI") != "1":
        return None
    if settings.get("XR_SNAPCAST_ROLE", "client") not in ("server", "server-client"):
        return None
    server_host = settings.get("XR_SNAPCAST_SERVER_BIND", "")
    try:
        address = ipaddress.ip_address(server_host)
    except ValueError:
        address = None
    if not address or address.is_unspecified or address.version != 4:
        return None
    try:
        mac = pathlib.Path("/sys/class/net/eth0/address").read_text().strip()
    except OSError:
        mac = "00:00:00:00:00:00"
    identity = "".join(char for char in mac.lower() if char in "0123456789abcdef")
    if len(identity) != 12:
        identity = "unknown"
    return {
        "name": f"xiaorui-{identity}._xiaorui-music._tcp.local.",
        "server_id": identity,
        "address": str(address),
        "port": int(settings.get("XR_SNAPCAST_SERVER_PORT", "1704")),
    }


def parse_questions(data):
    if len(data) < 12:
        return []
    count = struct.unpack("!H", data[4:6])[0]
    offset, questions = 12, []
    for _ in range(count):
        start = offset
        labels = []
        while offset < len(data):
            size = data[offset]
            if size == 0:
                offset += 1
                break
            if size & 0xC0 or size > 63 or offset + 1 + size > len(data):
                return []  # mDNS questions should use uncompressed names.
            labels.append(data[offset + 1:offset + 1 + size].lower())
            offset += 1 + size
        if offset + 4 > len(data):
            return []
        qtype = struct.unpack("!H", data[offset:offset + 2])[0]
        offset += 4  # qtype + qclass
        questions.append((b".".join(labels), qtype, start, offset))
    return questions


def build_a_response(query, question, ip_address, ttl=120):
    _name, _qtype, start, end = question
    question_wire = query[start:end]
    answer = (struct.pack("!HHHIH", 0xC00C, 1, 1, ttl, 4)
              + socket.inet_aton(ip_address))
    return (query[:2] + struct.pack("!H", 0x8400)
            + struct.pack("!HHHH", 1, 1, 0, 0) + question_wire + answer)


def answer_ip():
    env = os.environ.get("XR_HA_IP")
    if env:
        return env
    try:  # Probe the route to mDNS without sending a packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((GROUP, PORT))
            address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    return "127.0.0.1"


def answer_ip_for_peer(peer_ip, interface_addresses, fallback):
    """Choose this board's address on the requester's IPv4 subnet."""
    if os.environ.get("XR_HA_IP"):
        return fallback
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return fallback
    for address, netmask in interface_addresses:
        try:
            network = ipaddress.ip_network(f"{address}/{netmask}", strict=False)
            if peer in network:
                return address
        except ValueError:
            continue
    return fallback


def main():
    import psutil

    zeroconf = None
    service_info = None
    try:
        settings = read_service_settings()
        advertisement = music_server_advertisement(settings)
        if advertisement:
            from zeroconf import ServiceInfo, Zeroconf
            zeroconf = Zeroconf()
            service_info = ServiceInfo(
                "_xiaorui-music._tcp.local.", advertisement["name"],
                addresses=[socket.inet_aton(advertisement["address"])],
                port=advertisement["port"],
                properties={
                    b"id": advertisement["server_id"].encode("ascii"),
                    b"name": "小瑞音乐".encode("utf-8"),
                    b"snapcast_port": str(advertisement["port"]).encode("ascii"),
                },
            )
            zeroconf.register_service(service_info, allow_name_change=True)
            print("DNS-SD music server:", advertisement, flush=True)
    except Exception as exc:
        print("DNS-SD music advertisement failed:", exc, file=sys.stderr, flush=True)

    ip_address = answer_ip()
    names = configured_names()
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    server.bind(("", PORT))
    # Join each IPv4 interface so queries arriving on a non-default network
    # interface are received too.
    interface_addresses = [(entry.address, entry.netmask)
                           for rows in psutil.net_if_addrs().values()
                           for entry in rows if entry.family == socket.AF_INET and entry.netmask]
    addresses = {address for address, _netmask in interface_addresses}
    addresses.add("0.0.0.0")
    print("join on:", sorted(addresses), flush=True)
    for address in addresses:
        try:
            server.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                              socket.inet_aton(GROUP) + socket.inet_aton(address))
        except OSError:
            pass
    print("mDNS A names:", ", ".join(sorted(name.decode() for name in names)),
          "->", ip_address, flush=True)

    try:
        while True:
            data, source = server.recvfrom(4096)
            try:
                if len(data) < 12 or data[2] & 0x80:
                    continue
                questions = parse_questions(data)
                # The service configuration can change while this responder stays
                # up; pick up the music alias without a reboot.
                names = configured_names()
                match = next((question for question in questions
                              if question[0] in names and question[1] in (1, 255)), None)
                if match is None:
                    continue
                print("q from", source, match[:2], flush=True)
                response_ip = answer_ip_for_peer(source[0], interface_addresses, ip_address)
                response = build_a_response(data, match, response_ip)
                destination = source if source[1] != PORT else (GROUP, PORT)
                if source[1] == PORT:
                    server.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                      socket.inet_aton(response_ip))
                server.sendto(response, destination)
            except (OSError, ValueError, struct.error) as exc:
                print("mDNS error:", exc, file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        if zeroconf is not None:
            if service_info is not None:
                zeroconf.unregister_service(service_info)
            zeroconf.close()


if __name__ == "__main__":
    main()

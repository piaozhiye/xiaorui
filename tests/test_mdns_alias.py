import importlib.util
import os
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "board-server" / "mdns-ha.py"
SPEC = importlib.util.spec_from_file_location("mdns_ha", MODULE_PATH)
mdns = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mdns
SPEC.loader.exec_module(mdns)


def question(name, qtype=1):
    wire = b"".join(bytes((len(label),)) + label.encode("ascii")
                    for label in name.rstrip(".").split(".")) + b"\0"
    return wire + struct.pack("!HH", qtype, 1)


class MdnsAliasTests(unittest.TestCase):
    def test_configured_aliases_keep_homeassistant_default(self):
        names = mdns.configured_names("xiaorui-music.local,custom.local.")
        self.assertEqual(names, {b"homeassistant.local", b"xiaorui-music.local", b"custom.local"})

    def test_music_alias_is_enabled_from_service_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            service_env = Path(directory) / "service.env"
            service_env.write_text("XR_MUSIC_UI=1\nXR_MUSIC_MDNS_ALIAS=studio.local\n")
            names = mdns.configured_names(None, service_env)
            self.assertIn(b"studio.local", names)
            service_env.write_text("XR_MUSIC_UI=0\nXR_MUSIC_MDNS_ALIAS=studio.local\n")
            names = mdns.configured_names(None, service_env)
            self.assertNotIn(b"studio.local", names)

    def test_music_server_advertisement_requires_enabled_server_role(self):
        server = mdns.music_server_advertisement({
            "XR_MUSIC_UI": "1", "XR_SNAPCAST_ROLE": "server-client",
            "XR_SNAPCAST_SERVER_BIND": "192.0.2.10", "XR_SNAPCAST_SERVER_PORT": "1704",
        })
        self.assertIsNotNone(server)
        self.assertEqual(server["address"], "192.0.2.10")
        self.assertEqual(server["port"], 1704)
        self.assertIn("_xiaorui-music._tcp.local.", server["name"])
        self.assertIsNone(mdns.music_server_advertisement({
            "XR_MUSIC_UI": "0", "XR_SNAPCAST_ROLE": "server-client",
            "XR_SNAPCAST_SERVER_BIND": "192.0.2.10",
        }))
        self.assertIsNone(mdns.music_server_advertisement({
            "XR_MUSIC_UI": "1", "XR_SNAPCAST_ROLE": "client",
            "XR_SNAPCAST_SERVER_BIND": "192.0.2.10",
        }))

    def test_invalid_alias_is_ignored(self):
        names = mdns.configured_names("bad..local,spaces are bad.local")
        self.assertEqual(names, {b"homeassistant.local"})

    def test_a_response_answers_only_the_matching_question(self):
        query = (b"\0\0\0\0" + struct.pack("!HHHH", 2, 0, 0, 0)
                 + question("unrelated.local") + question("xiaorui-music.local"))
        parsed = mdns.parse_questions(query)
        match = next(row for row in parsed if row[0] == b"xiaorui-music.local")
        response = mdns.build_a_response(query, match, "192.0.2.10")
        self.assertEqual(struct.unpack("!H", response[4:6])[0], 1)
        self.assertEqual(struct.unpack("!H", response[6:8])[0], 1)
        only_question = mdns.parse_questions(response)
        self.assertEqual(len(only_question), 1)
        self.assertEqual(only_question[0][:2], (b"xiaorui-music.local", 1))
        answer_start = only_question[0][3]
        owner, record_type, record_class, ttl, size = struct.unpack(
            "!HHHIH", response[answer_start:answer_start + 12])
        self.assertEqual((owner, record_type, record_class, ttl, size), (0xC00C, 1, 1, 120, 4))
        self.assertEqual(socket.inet_ntoa(response[answer_start + 12:answer_start + 16]),
                         "192.0.2.10")

    def test_unsupported_compressed_question_is_ignored(self):
        query = b"\0\0\0\0" + struct.pack("!HHHH", 1, 0, 0, 0) + b"\xc0\x0c\0\x01\0\x01"
        self.assertEqual(mdns.parse_questions(query), [])

    def test_responder_selects_interface_on_requesters_subnet(self):
        rows = [("198.51.100.10", "255.255.255.0"),
                ("192.0.2.11", "255.255.255.0")]
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(mdns.answer_ip_for_peer("192.0.2.55", rows, "192.0.2.10"),
                             "192.0.2.11")
            self.assertEqual(mdns.answer_ip_for_peer("10.0.0.5", rows, "198.51.100.10"),
                             "198.51.100.10")


if __name__ == "__main__":
    unittest.main()

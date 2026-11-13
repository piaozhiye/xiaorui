import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess

import pytest

from test_ui_client import Message


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def interface(tmp_path):
    library = tmp_path / "server.so"
    subprocess.run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        str(ROOT / "board/ui_server.c"), str(ROOT / "board/ui_json.c"),
        "-o", str(library),
    ], check=True)
    api = ctypes.CDLL(str(library))
    api.xr_ui_server_open.argtypes = [ctypes.c_char_p]
    api.xr_ui_server_open.restype = ctypes.c_void_p
    api.xr_ui_server_close.argtypes = [ctypes.c_void_p]
    api.xr_ui_server_begin_turn.argtypes = [ctypes.c_void_p]
    api.xr_ui_server_state.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint]
    api.xr_ui_server_text.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p]
    api.xr_ui_server_poll.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    api.xr_ui_json_string.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                      ctypes.c_char_p, ctypes.c_size_t]
    return api


@pytest.mark.parametrize("payload,capacity,expected,result", [
    (b'{"text":"\\u4f60\\u597d\\ud83d\\ude00"}', 64, "你好😀", 1),
    (json.dumps({"text": '说"你好"\\再见\n'}).encode(), 64, '说"你好"\\再见\n', 1),
    ('{"text":"你好😀结束"}'.encode(), 8, "你好", 1),
    ('{"text":"你好"}'.encode(), 1, "", 1),
    (b'{"nested":{"text":"wrong"},"text":"right"}', 64, "right", 1),
    (b'{"other":"absent"}', 64, "", 0),
    (b'{"text":"\\ud800"}', 64, "", -1),
    (b'{"text":"\\u0000"}', 64, "", -1),
    (b'{"text":"\xf0\x80\x80\x80"}', 64, "", -1),
    (b'{"text":"unfinished', 64, "", -1),
])
def test_unicode_decoder(interface, payload, capacity, expected, result):
    output = ctypes.create_string_buffer(capacity)
    assert interface.xr_ui_json_string(payload, b"text", output, capacity) == result
    assert output.value.decode() == expected


def test_status_controls_and_captions(interface, tmp_path):
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    assert server
    volume = ctypes.c_uint()
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        interface.xr_ui_server_state(server, 3, 80)
        interface.xr_ui_server_text(server, True, json.dumps({"text": "现在几点了？"}).encode())
        for sentence in ["现在是", "上午十一点。"]:
            interface.xr_ui_server_text(server, False, json.dumps(
                {"state": "sentence_start", "text": sentence}).encode())
        interface.xr_ui_server_text(server, False, b'{"text":"ignored","state":"stop"}')
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:
            peer.bind(b"\0xr-test-" + os.urandom(8))
            peer.settimeout(0.1)

            def send(message):
                peer.sendto(bytes(message), str(path))
                return interface.xr_ui_server_poll(server, ctypes.byref(volume))

            query = Message(magic=0x58525549, version=1, type=1)
            assert send(query) == 0
            status = Message.from_buffer_copy(peer.recv(2048))
            assert (status.state, status.volume) == (3, 80)
            assert status.question.decode() == "现在几点了？"
            assert status.answer.decode() == "现在是上午十一点。"
            assert send(Message(magic=0x58525549, version=1, type=3, volume=70)) == 3
            assert volume.value == 70
            assert send(Message(magic=0x58525549, version=1, type=3, volume=101)) == 0
            assert send(Message(magic=0x58525549, version=1, type=4)) == 4
            assert send(Message(magic=0, version=1, type=4)) == 0
            peer.sendto(bytes(query) + b"oversized", str(path))
            assert interface.xr_ui_server_poll(server, ctypes.byref(volume)) == 0
            with pytest.raises(socket.timeout):
                peer.recv(2048)
            interface.xr_ui_server_begin_turn(server)
            assert send(query) == 0
            status = Message.from_buffer_copy(peer.recv(2048))
            assert status.question == status.answer == b""
        assert not interface.xr_ui_server_open(os.fsencode(path))
        assert path.exists()
    finally:
        interface.xr_ui_server_close(server)
    assert not path.exists()


def test_recover_stale_socket_preserve_files_and_reject_public_directory(interface, tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "voice.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as stale:
        stale.bind(str(path))
    server = interface.xr_ui_server_open(os.fsencode(path))
    assert server
    interface.xr_ui_server_close(server)
    path.write_text("preserve")
    assert not interface.xr_ui_server_open(os.fsencode(path))
    assert path.read_text() == "preserve"
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    assert not interface.xr_ui_server_open(os.fsencode(public / "voice.sock"))
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    assert not interface.xr_ui_server_open(os.fsencode(link / "voice.sock"))

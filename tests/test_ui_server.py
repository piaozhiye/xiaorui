import ctypes
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
        str(ROOT / "board/ui_server.c"),
        "-o", str(library),
    ], check=True)
    api = ctypes.CDLL(str(library))
    api.xr_ui_server_open.argtypes = [ctypes.c_char_p]
    api.xr_ui_server_open.restype = ctypes.c_void_p
    api.xr_ui_server_close.argtypes = [ctypes.c_void_p]
    api.xr_ui_server_turn_begin.argtypes = [ctypes.c_void_p]
    api.xr_ui_server_state.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint]
    api.xr_ui_server_text.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_char_p]
    api.xr_ui_server_poll.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    api.xr_ui_server_client_active.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    return api


def test_status_controls_and_captions(interface, tmp_path):
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    assert server
    volume = ctypes.c_uint()
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        interface.xr_ui_server_state(server, 3, 80)
        interface.xr_ui_server_text(server, True, "现在几点了？".encode())
        for sentence in ["现在是", "上午十一点。"]:
            interface.xr_ui_server_text(server, False, sentence.encode())
        interface.xr_ui_server_text(server, False, b"")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:
            peer.bind(b"\0xr-test-" + os.urandom(8))
            peer.settimeout(0.1)

            def send(message):
                peer.sendto(bytes(message), str(path))
                return interface.xr_ui_server_poll(server, ctypes.byref(volume))

            query = Message(magic=0x58525549, version=1, type=1)
            assert send(query) == 0
            status = Message.from_buffer_copy(peer.recv(8192))
            assert (status.state, status.volume) == (3, 80)
            assert status.question.decode() == "现在几点了？"
            assert status.answer.decode() == "现在是\n上午十一点。"
            assert send(Message(magic=0x58525549, version=1, type=3, volume=70)) == 3
            assert volume.value == 70
            assert send(Message(magic=0x58525549, version=1, type=3, volume=101)) == 0
            assert send(Message(magic=0x58525549, version=1, type=4)) == 4
            assert send(Message(magic=0, version=1, type=4)) == 0
            peer.sendto(bytes(query) + b"oversized", str(path))
            assert interface.xr_ui_server_poll(server, ctypes.byref(volume)) == 0
            with pytest.raises(socket.timeout):
                peer.recv(8192)
            interface.xr_ui_server_turn_begin(server)
            assert send(query) == 0
            status = Message.from_buffer_copy(peer.recv(8192))
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


def query_status(interface, server, path):
    volume = ctypes.c_uint()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:
        peer.bind(b"\0xr-test-" + os.urandom(8))
        peer.settimeout(0.5)
        peer.sendto(bytes(Message(magic=0x58525549, version=1, type=1)), str(path))
        assert interface.xr_ui_server_poll(server, ctypes.byref(volume)) == 0
        return Message.from_buffer_copy(peer.recv(8192))


def test_client_activity_follows_ui_queries(interface, tmp_path):
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    try:
        assert interface.xr_ui_server_client_active(server, 2000) == 0
        query_status(interface, server, path)
        assert interface.xr_ui_server_client_active(server, 2000) == 1
        assert interface.xr_ui_server_client_active(server, 0) == 0
    finally:
        interface.xr_ui_server_close(server)


def test_captions_truncate_on_character_boundary(interface, tmp_path):
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    try:
        interface.xr_ui_server_text(server, True, ("问" * 200).encode())
        interface.xr_ui_server_text(server, False, ("答" * 2000).encode())
        status = query_status(interface, server, path)
        question = status.question.decode()
        answer = status.answer.decode()
        assert question == "问" * 85
        assert answer == "答" * 1365
        interface.xr_ui_server_text(server, False, ("答" * 100).encode())
        assert query_status(interface, server, path).answer.decode() == "答" * 1365
    finally:
        interface.xr_ui_server_close(server)

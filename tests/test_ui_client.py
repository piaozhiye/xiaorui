import ctypes
from pathlib import Path
import socket
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Message(ctypes.Structure):
    _fields_ = [
        ("magic", ctypes.c_uint32), ("version", ctypes.c_uint32),
        ("type", ctypes.c_uint32), ("state", ctypes.c_uint32),
        ("volume", ctypes.c_uint32), ("question", ctypes.c_char * 256),
        ("answer", ctypes.c_char * 4096),
    ]


@pytest.fixture
def client(tmp_path):
    library = tmp_path / "client.so"
    subprocess.run([
        "cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        str(ROOT / "ui/ui_client.c"), "-o", str(library),
    ], check=True)
    interface = ctypes.CDLL(str(library))
    interface.xr_ui_client_open.argtypes = [ctypes.c_char_p]
    interface.xr_ui_client_open.restype = ctypes.c_int
    interface.xr_ui_client_tick.argtypes = [ctypes.c_uint64]
    interface.xr_ui_client_connected.argtypes = [ctypes.c_uint64]
    interface.xr_ui_client_connected.restype = ctypes.c_bool
    interface.xr_ui_message_valid.argtypes = [ctypes.POINTER(Message), ctypes.c_uint]
    interface.xr_ui_message_valid.restype = ctypes.c_bool
    interface.xr_ui_client_status.restype = ctypes.POINTER(Message)
    interface.xr_ui_client_command.argtypes = [ctypes.c_int, ctypes.c_uint]
    interface.xr_ui_client_command.restype = ctypes.c_int
    yield interface
    interface.xr_ui_client_close()


def status():
    return Message(magic=0x58525549, version=1, type=2, state=1, volume=60,
                   question="你叫什么名字？".encode(), answer="我叫小瑞。".encode())


def test_status_validation(client):
    message = status()
    assert ctypes.sizeof(message) == 4372
    assert client.xr_ui_message_valid(ctypes.byref(message), ctypes.sizeof(message))
    for field, value in [("magic", 0), ("version", 2), ("type", 4), ("state", 4),
                         ("volume", 0), ("volume", 101)]:
        invalid = status()
        setattr(invalid, field, value)
        assert not client.xr_ui_message_valid(ctypes.byref(invalid), ctypes.sizeof(invalid))
    assert not client.xr_ui_message_valid(ctypes.byref(message), ctypes.sizeof(message) - 1)
    message.question = b"x" * 256
    assert not client.xr_ui_message_valid(ctypes.byref(message), ctypes.sizeof(message))


def test_missing_voice_endpoint_is_not_connected(client, tmp_path):
    assert client.xr_ui_client_open(str(tmp_path / "missing.sock").encode()) == 0
    client.xr_ui_client_tick(1000)
    assert not client.xr_ui_client_connected(1000)
    assert not (tmp_path / "missing.sock").exists()


def test_live_status_timeout_and_controls(client, tmp_path):
    path = tmp_path / "voice.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(path))
        server.settimeout(1)
        assert client.xr_ui_client_open(str(path).encode()) == 0
        client.xr_ui_client_tick(1000)
        query, peer = server.recvfrom(8192)
        assert Message.from_buffer_copy(query).type == 1
        message = status()
        server.sendto(bytes(message), peer)
        client.xr_ui_client_tick(1001)
        assert client.xr_ui_client_connected(1001)
        assert client.xr_ui_client_status().contents.question.decode() == "你叫什么名字？"
        assert client.xr_ui_client_status().contents.volume == 60
        assert client.xr_ui_client_command(3, 70) == 0
        command, _ = server.recvfrom(8192)
        assert Message.from_buffer_copy(command).volume == 70
        assert client.xr_ui_client_command(4, 0) == 0
        command, _ = server.recvfrom(8192)
        assert Message.from_buffer_copy(command).type == 4
        assert client.xr_ui_client_command(3, 101) == -1
        assert client.xr_ui_client_command(99, 0) == -1
        assert not client.xr_ui_client_connected(3501)


def test_other_sender_and_oversized_messages_are_rejected(client, tmp_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(tmp_path / "voice.sock"))
        server.settimeout(1)
        assert client.xr_ui_client_open(str(tmp_path / "voice.sock").encode()) == 0
        client.xr_ui_client_tick(1000)
        _, peer = server.recvfrom(8192)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as other:
            other.bind(str(tmp_path / "other.sock"))
            other.sendto(bytes(status()), peer)
        client.xr_ui_client_tick(1001)
        assert not client.xr_ui_client_connected(1001)
        server.sendto(bytes(status()) + b"oversized", peer)
        client.xr_ui_client_tick(1002)
        assert not client.xr_ui_client_connected(1002)


def test_invalid_endpoint_and_double_open_are_rejected(client, tmp_path):
    assert client.xr_ui_client_open(b"relative.sock") == -1
    assert client.xr_ui_client_open(b"/" + b"x" * 200) == -1
    path = str(tmp_path / "voice.sock").encode()
    assert client.xr_ui_client_open(path) == 0
    assert client.xr_ui_client_open(path) == -1

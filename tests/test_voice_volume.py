import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "snapcast_center"))

from voice_volume import MESSAGE, VoiceVolumeError, get_gain, set_gain


def test_get_and_set_voice_gain_over_frontend_protocol(tmp_path):
    path = str(tmp_path / "voice.sock")
    state = {"gain": 40}
    ready = threading.Event()

    def serve():
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
            server.bind(path)
            ready.set()
            while True:
                try:
                    request, address = server.recvfrom(MESSAGE.size + 1)
                except OSError:
                    return
                fields = MESSAGE.unpack(request)
                if fields[2] == 6:
                    state["gain"] = fields[4]
                elif fields[2] == 7:
                    server.sendto(MESSAGE.pack(0x58525549, 1, 8, 1,
                                               state["gain"], 0, b"", b""), address)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(1)
    assert get_gain(path) == 40
    assert set_gain(path, 27) == 27
    assert state["gain"] == 27
    with pytest.raises(VoiceVolumeError):
        set_gain(path, 101)


def test_rejects_wrong_protocol_and_unavailable_endpoint(tmp_path):
    path = str(tmp_path / "voice.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        ready = threading.Event()

        def serve():
            _, address = server.recvfrom(MESSAGE.size + 1)
            server.sendto(struct.pack("=6I256s4096s", 0x58525549, 9, 8, 1,
                                      40, 0, b"", b""), address)
            ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with pytest.raises(VoiceVolumeError, match="协议版本"):
            get_gain(path)
    with pytest.raises(VoiceVolumeError, match="无法连接"):
        get_gain(str(tmp_path / "missing.sock"))

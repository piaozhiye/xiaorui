"""Small authenticated-by-UID bridge to the local frontend voice-volume IPC."""
import os
import socket
import struct
import threading

MAGIC = 0x58525549
VERSION = 1
QUERY = 1
STATUS = 8
TTS_GAIN = 6
QUERY_GAIN = 7
MESSAGE = struct.Struct("=6I256s4096s")
_counter = 0
_counter_lock = threading.Lock()


class VoiceVolumeError(RuntimeError):
    pass


def _socket(path):
    global _counter
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    client.settimeout(0.8)
    with _counter_lock:
        _counter += 1
        serial = _counter
    client.bind("\0xiaorui-music-%d-%d" % (os.getpid(), serial))
    try:
        client.connect(path)
    except OSError:
        client.close()
        raise
    return client


def _pack(kind, value=0):
    return MESSAGE.pack(MAGIC, VERSION, kind, 0, value, 0, b"", b"")


def _query(client):
    client.send(_pack(QUERY_GAIN))
    try:
        raw = client.recv(MESSAGE.size + 1)
    except (socket.timeout, OSError) as exc:
        raise VoiceVolumeError("语音前端没有响应") from exc
    if len(raw) != MESSAGE.size:
        raise VoiceVolumeError("语音前端返回了无效状态")
    fields = MESSAGE.unpack(raw)
    if fields[0] != MAGIC or fields[1] != VERSION or fields[2] != STATUS:
        raise VoiceVolumeError("语音前端协议版本不匹配，请更新前端")
    gain = fields[4]
    if not 1 <= gain <= 100:
        raise VoiceVolumeError("语音音量状态无效")
    return gain


def get_gain(path):
    try:
        with _socket(path) as client:
            return _query(client)
    except VoiceVolumeError:
        raise
    except OSError as exc:
        raise VoiceVolumeError("无法连接语音前端") from exc


def set_gain(path, gain):
    if isinstance(gain, bool) or not isinstance(gain, int) or not 10 <= gain <= 100:
        raise VoiceVolumeError("语音音量必须是 10 到 100 的整数")
    try:
        with _socket(path) as client:
            client.send(_pack(TTS_GAIN, gain))
            # The frontend applies and persists the command during its next
            # poll. A following query is handled on the next poll iteration.
            actual = _query(client)
            if actual != gain:
                raise VoiceVolumeError("语音前端未确认新音量")
            return actual
    except VoiceVolumeError:
        raise
    except OSError as exc:
        raise VoiceVolumeError("无法连接语音前端") from exc

#!/usr/bin/env python3
"""Exercise the actual C cloud backend against an isolated loopback WS peer."""
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import ctypes
import json


def exact(conn, size):
    data = b""
    while len(data) < size:
        part = conn.recv(size - len(data))
        if not part:
            raise EOFError("client closed")
        data += part
    return data


def receive(conn):
    first, second = exact(conn, 2)
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(conn, 8))[0]
    assert second & 128, "client frames must be masked"
    mask = exact(conn, 4)
    data = exact(conn, length)
    return first & 15, bytes(v ^ mask[i % 4] for i, v in enumerate(data))


def frame(opcode, data):
    assert len(data) < 126
    return bytes((128 | opcode, len(data))) + data


def serve(listener, scenario, errors):
    try:
        with listener.accept()[0] as conn:
            conn.settimeout(3)
            request = b""
            while not request.endswith(b"\r\n\r\n"):
                request += exact(conn, 1)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
            assert b'"hello"' in receive(conn)[1]
            hello = frame(1, b'{"type":"hello","session_id":"test"}')
            conn.sendall(hello[:1])
            time.sleep(0.03)
            conn.sendall(hello[1:])
            assert b'"start"' in receive(conn)[1]
            if scenario == "announce_ack":
                for accepted in (False, True):
                    conn.sendall(frame(1, json.dumps({"type": "announce", "state": "offer",
                        "request_id": "a" * 32}, separators=(",", ":")).encode()))
                    ack = json.loads(receive(conn)[1])
                    assert ack["state"] == "announce_ack" and ack["accepted"] is accepted
                conn.sendall(frame(1, json.dumps({"type":"announce","state":"grant",
                    "request_id":"a"*32}, separators=(",", ":")).encode()) +
                    frame(2, b"abc") + frame(1, b'{"type":"tts","state":"stop"}'))
                time.sleep(.2)
                return
            if scenario == "reboot":
                # stt 变量在此之后才定义, 用字面帧
                conn.sendall(frame(1, b'{"type": "stt", "text": "r"}')
                             + frame(1, b'{"type": "reboot", "session_id": "t"}'))
                time.sleep(1.2)
                return
            if scenario == "announce":
                # 主动播报(定时提醒): 客户端未提交任何轮次, announce+start+音频+stop
                time.sleep(0.2)
                start = '{"type": "tts", "state": "start", "text": "timer"}'.encode()
                conn.sendall(frame(1, b'{"type": "announce", "session_id": "t"}')
                             + frame(1, start)
                             + frame(2, b"abc")
                             + frame(1, b'{"type": "tts", "state": "stop"}'))
                time.sleep(1.2)
                return
            if scenario == "audio":
                start = receive(conn)[1]
                assert b'"manual"' in start and b'"start"' in start
                opus = ctypes.CDLL("libopus.so.0")
                opus.opus_packet_get_nb_samples.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
                for _ in range(3):
                    opcode, packet = receive(conn)
                    assert opcode == 2 and opus.opus_packet_get_nb_samples(packet, len(packet), 16000) == 960
                assert b'"stop"' in receive(conn)[1]
                conn.sendall(frame(1, b'{"type":"stt","text":"audio recognized"}')
                             + frame(2,b"abc") + frame(1,b'{"type":"tts","state":"stop"}'))
                return
            assert b'"detect"' in receive(conn)[1]
            if scenario in ("continue-cloud", "continue-local"):
                conn.sendall(frame(1, b'{"type":"stt","text":"answer"}')
                             + frame(2, b"abc")
                             + frame(1, b'{"type":"tts","state":"sentence_end","continuation":"music"}')
                             + frame(1, b'{"type":"tts","state":"sentence_start","media":"music","text":"radio"}')
                             + frame(2, b"abc") + frame(1, b'{"type":"tts","state":"stop"}'))
                return
            if scenario in ("abort-cloud", "abort-local"):
                stt = frame(1, b'{"type":"stt","text":"music"}')
                conn.sendall(stt + frame(2, b"abc"))
                assert json.loads(receive(conn)[1]) == {"type": "abort"}
                conn.sendall(frame(1, b'{"type":"tts","state":"stop"}')
                             + frame(9, b"still-connected"))
                assert receive(conn) == (10, b"still-connected")
                assert b'"second turn"' in receive(conn)[1]
                conn.sendall(stt + frame(2, b"abc")
                             + frame(1, b'{"type":"tts","state":"stop"}'))
                return
            if scenario == "silent":
                time.sleep(1.2)
                return
            if scenario == "before_audio":
                return
            # Previous stop before acceptance, followed by a real ping.
            conn.sendall(frame(1, b'{"type":"tts","state":"stop"}') + frame(9, b"ping"))
            assert receive(conn) == (10, b"ping")
            stt = frame(1, b'{"type":"stt","text":"test"}')
            if scenario == "disorder":
                # 此时尚未发送 STT，客户端须丢弃音频和 stop。
                early_audio = frame(2, b"abc")
                conn.sendall(early_audio + frame(1, b'{"type":"tts","state":"stop"}'))
                conn.sendall(frame(9, b"before-stt"))
                assert receive(conn) == (10, b"before-stt")
                conn.sendall(stt + early_audio + frame(1, b'{"type":"tts","state":"stop"}'))
                conn.sendall(frame(1, b'{"type":"tts","state":"stop"}'))
                time.sleep(0.15)
                conn.sendall(stt + early_audio + frame(1, b'{"type":"tts","state":"stop"}'))
                # 等待客户端确认已消费整批迟到事件，避免提前退出的假绿/断管。
                conn.sendall(frame(9, b"late-done"))
                assert receive(conn) == (10, b"late-done")
                return
            if scenario == "volume":
                # 音量指令轮: stt 回执 + volume 指令, 无音频无 stop,
                # 客户端应报 EV_VOLUME 并把 ev_text 归一为 set:60
                conn.sendall(stt + frame(
                    1, b'{"type": "volume", "op": "set", "value": 60, "session_id": "t"}'))
                # 客户端事件循环靠 1.0s 时限自然收束, 连接须保持到它退出之后
                # (否则读到 close 帧记 EV_ERROR), 与 silent 场景同款等待。
                time.sleep(1.2)
                return
            conn.sendall(stt[:3])
            time.sleep(0.1)
            conn.sendall(stt[3:] + frame(2, b"abc"))
            if scenario == "normal":
                conn.sendall(frame(1, b'{"type":"tts","state":"stop"}'))
                conn.sendall(stt + frame(2, b"abc")
                             + frame(1, b'{"type":"tts","state":"stop"}'))
                assert b'"second turn"' in receive(conn)[1]
                conn.sendall(stt + frame(2, b"abc")
                             + frame(1, b'{"type":"tts","state":"stop"}'))
    except Exception as exc:
        errors.append(exc)


def main():
    for scenario in ("normal", "volume", "reboot", "announce", "announce_ack", "disorder", "midstream", "silent", "before_audio", "audio", "abort-cloud", "abort-local", "continue-cloud", "continue-local"):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(4)
            errors = []
            thread = threading.Thread(target=serve, args=(listener, scenario, errors))
            thread.start()
            env = os.environ.copy()
            env.pop("XR_CLOUD_TOKEN", None)
            env["XR_CLOUD_ASR"] = "1" if scenario == "audio" else "0"
            env["XR_CLOUD_URL"] = f"ws://127.0.0.1:{listener.getsockname()[1]}/test"
            env["XR_LOCAL_PORT"] = str(listener.getsockname()[1])
            try:
                subprocess.run([sys.argv[1], scenario], env=env, check=True, timeout=5)
            finally:
                thread.join(timeout=4)
            assert not thread.is_alive(), "fake gateway did not finish"
            if errors:
                raise errors[0]


if __name__ == "__main__":
    main()

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
    api.xr_ui_server_state.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
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
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:
            peer.bind(b"\0xr-test-" + os.urandom(8))
            peer.settimeout(0.1)

            def send(message):
                peer.sendto(bytes(message), str(path))
                return interface.xr_ui_server_poll(server, ctypes.byref(volume))

            query = Message(magic=0x58525549, version=1, type=1)
            # 客户端先接入一次(会话开始: 服务端会清掉此前的文本),
            # 再看状态 —— 真实流程也是 UI 先连上, 才有后续问答。
            assert send(query) == 0
            peer.recv(8192)
            interface.xr_ui_server_state(server, 3, 80, 40)
            interface.xr_ui_server_text(server, True, "现在几点了？".encode())
            for sentence in ["现在是", "上午十一点。"]:
                interface.xr_ui_server_text(server, False, sentence.encode())
            interface.xr_ui_server_text(server, False, b"")
            assert send(query) == 0
            status = Message.from_buffer_copy(peer.recv(8192))
            assert (status.state, status.volume) == (3, 80)
            assert status.question.decode() == "现在几点了？"
            assert status.answer.decode() == "现在是\n上午十一点。"
            assert send(Message(magic=0x58525549, version=1, type=3, volume=70)) == 3
            assert volume.value == 70
            assert send(Message(magic=0x58525549, version=1, type=3, volume=101)) == 0
            assert send(Message(magic=0x58525549, version=1, type=6, volume=32)) == 6
            assert volume.value == 32
            assert send(Message(magic=0x58525549, version=1, type=7)) == 0
            status = Message.from_buffer_copy(peer.recv(8192))
            assert status.type == 8 and status.volume == 40
            assert send(Message(magic=0x58525549, version=1, type=6, volume=0)) == 0
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


def query_status(interface, server, path, peer=None):
    """问一次状态。

    peer: 客户端的抽象地址; 同一地址 = 同一个客户端(真实 UI 的地址是
    @xiaorui-ui.<pid>, 进程重启才换)。不同地址会被服务端当成新客户端
    接入(会话重来, 旧文本清空)。
    """
    volume = ctypes.c_uint()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as handle:
        handle.bind(peer if peer is not None else b"\0xr-test-fixed")
        handle.settimeout(0.5)
        handle.sendto(bytes(Message(magic=0x58525549, version=1, type=1)), str(path))
        assert interface.xr_ui_server_poll(server, ctypes.byref(volume)) == 0
        return Message.from_buffer_copy(handle.recv(8192))


def test_reset_message_clears_previous_conversation(interface, tmp_path):
    """UI 启动时发的 XR_UI_RESET 必须清掉上一轮文本。

    回归(2026-11-20): 原来靠'客户端断过 3 秒'判定新会话, UI 快速重启
    (<3s)时旧文本残留在文本框里。改成 UI 主动发 RESET 信令。
    """
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    try:
        # 先建立客户端并放入上一轮文本
        query_status(interface, server, path)
        interface.xr_ui_server_text(server, True, "上一轮问题".encode())
        interface.xr_ui_server_text(server, False, "上一轮回答".encode())
        assert query_status(interface, server, path).question.decode() == "上一轮问题"
        # UI 重启 -> 发 RESET -> 清空
        volume = ctypes.c_uint()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:
            peer.bind(b"\0xr-test-reset")
            peer.settimeout(0.5)
            peer.sendto(bytes(Message(magic=0x58525549, version=1,
                                      type=5)), str(path))
            assert interface.xr_ui_server_poll(server, ctypes.byref(volume)) == 0
            status = Message.from_buffer_copy(peer.recv(8192))
        assert status.question == b""
        assert status.answer == b""
    finally:
        interface.xr_ui_server_close(server)


def test_new_client_clears_previous_conversation(interface, tmp_path):
    """新客户端接入 = 会话重来: 清掉上一轮问答(UI 重启后别显示旧内容)。

    回归(2026-11-20 用户反馈): UI 进程重启后重新连上前端, 前端把
    "当前轮次"的旧文本原样给了它, 文本框里留着上一轮对话。
    """
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    try:
        interface.xr_ui_server_text(server, True, "旧问题".encode())
        interface.xr_ui_server_text(server, False, "旧回答".encode())
        # 第一个客户端接入: 会话开始, 旧文本被清掉
        status = query_status(interface, server, path)
        assert status.question == b""
        assert status.answer == b""
        # 同一个客户端继续问: 内容保留
        interface.xr_ui_server_text(server, True, "新问题".encode())
        interface.xr_ui_server_text(server, False, "新回答".encode())
        status = query_status(interface, server, path)
        assert status.question.decode() == "新问题"
        assert status.answer.decode() == "新回答"
        # 客户端短暂切到另一个地址(诊断脚本 / 重启瞬间的连接抖动):
        # 没断够 3 秒, 不算新会话, 文本不该被清掉
        status = query_status(interface, server, path, peer=b"\0xr-test-other")
        assert status.question.decode() == "新问题"
        assert status.answer.decode() == "新回答"
    finally:
        interface.xr_ui_server_close(server)


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
    """字幕缓冲按 UTF-8 字符边界截断, 且**截断是可见的**。

    ★ 2026-11-20 review: 原来满了就静默丢弃后面的句子, 界面上看起来像
    "回答到这就结束了"(截断冒充完整)。现在尾部留 5 字节写 "...\\n" 标记,
    并且标记要出现在**有效内容末尾**(不能把已有字幕清掉)。
    """
    path = tmp_path / "private" / "voice.sock"
    server = interface.xr_ui_server_open(os.fsencode(path))
    try:
        # 客户端先接入(会话开始, 服务端清空此前文本), 再放字幕
        query_status(interface, server, path)
        interface.xr_ui_server_text(server, True, ("问" * 200).encode())
        interface.xr_ui_server_text(server, False, ("答" * 2000).encode())
        status = query_status(interface, server, path)
        question = status.question.decode()
        answer = status.answer.decode()
        assert question == "问" * 85
        # 4096 字节缓冲, 尾部留 5 字节给标记 -> 最多 1363 个 3 字节汉字
        assert answer == "答" * 1363
        # 再追加: 放不下了 -> 末尾标记截断, 已有内容保留
        interface.xr_ui_server_text(server, False, ("答" * 100).encode())
        answer = query_status(interface, server, path).answer.decode()
        assert answer == "答" * 1363 + "...\n"
        # 之后继续追加不再改变(只标记一次)
        interface.xr_ui_server_text(server, False, ("答" * 100).encode())
        assert query_status(interface, server, path).answer.decode() == "答" * 1363 + "...\n"
        # 新一轮开始: 标记复位, 字幕清空重来
        interface.xr_ui_server_turn_begin(server)
        interface.xr_ui_server_text(server, False, ("新" * 10).encode())
        assert query_status(interface, server, path).answer.decode() == "新" * 10
    finally:
        interface.xr_ui_server_close(server)

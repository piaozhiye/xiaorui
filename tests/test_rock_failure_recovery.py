"""真实子进程故障注入：挂初始化、阻塞写入、迟到输出与请求资源回收。"""
import ctypes
import io
import json
from pathlib import Path
import struct
import sys
import time
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'board-server/pkg'))
from core.providers.rock.helper_proc import HelperError, HelperProcess
from core.providers.rock import framing, rockx


def helper(tmp_path, source, ready_timeout=.15):
    path = tmp_path / 'helper.py'
    path.write_text(source)
    hp = HelperProcess(str(path), [], ready_timeout=ready_timeout)
    return hp


def test_ready_timeout_reaps_process_and_can_restart(tmp_path):
    hp = helper(tmp_path, 'import time; time.sleep(30)')
    begin = time.monotonic()
    with pytest.raises(HelperError, match='超时'):
        hp.start()
    assert time.monotonic() - begin < 3
    assert hp.proc is None
    Path(hp.script).write_text(
        'import sys,struct\n'
        'b=b\'{"ok":true,"event":"ready"}\'\n'
        'sys.stdout.buffer.write(struct.pack(">I",len(b))+b)\n'
        'sys.stdout.buffer.flush()\n'
        'sys.stdin.buffer.read()\n')
    hp.ready_timeout = 2
    try:
        hp.start()
        assert hp.alive
    finally:
        hp.stop()


def test_request_timeout_covers_blocked_pipe_write(tmp_path):
    hp = helper(tmp_path,
        'import sys,struct,time\n'
        'b=b\'{"ok":true,"event":"ready"}\'\n'
        'sys.stdout.buffer.write(struct.pack(">I",len(b))+b)\n'
        'sys.stdout.buffer.flush()\n'
        'time.sleep(30)\n', ready_timeout=2)
    try:
        hp.start()
        proc = hp.proc
        begin = time.monotonic()
        with pytest.raises(HelperError, match='超时'):
            hp.request({'cmd': 'asr'}, b'x' * (2 * 1024 * 1024), timeout=.15)
        assert time.monotonic() - begin < 3
        assert proc.poll() is not None
        assert hp.proc is None
    finally:
        hp.stop()


def test_eof_during_handshake_is_reaped(tmp_path):
    hp = helper(tmp_path, 'pass', ready_timeout=2)
    with pytest.raises(HelperError, match='读写帧失败'):
        hp.start()
    assert hp.proc is None


@pytest.mark.parametrize('length', [-1, 64*1024*1024+1, '3', True])
def test_invalid_body_length_is_rejected(length):
    header = json.dumps({'body_len': length}).encode()
    with pytest.raises(framing.ProtocolError):
        framing.recv_msg(io.BytesIO(struct.pack('>I', len(header)) + header))


def test_request_buffers_released_after_input_destroy():
    """推理结束销毁 input -> PCM/字符串引用全部释放。

    wait 句柄按 SDK 官方用法传 NULL: 两个 SDK 的 demo 都是
    RockXProcessAsync(handle, input, NULL), 从不调 RockXWaitFinish;
    传真实句柄再 WaitFinish 板端实测 ret=-1(ROCKX_RET_FAIL)。
    """
    rx = rockx.RockX.__new__(rockx.RockX)
    rx.lib = Mock()
    rx.lib.RockXInputCreate.return_value = 10
    rx.lib.RockXInputGetPacketAt.return_value = 20
    rx.lib.RockXProcessAsync.return_value = 0
    rx.lib.RockXInputDestroy.return_value = 0
    rx._keepalive, rx._input_refs, rx._packet_inputs = [], {}, {}
    for _ in range(100):
        inp = rx.input_create(1)
        pkt = rx.input_get_packet(inp)
        rx.packet_set_pcm(pkt, b'\0\0'*16000, 16000, 0)
        rx.packet_set_string(pkt, '测试')
        assert len(rx._input_refs[inp]) == 2
        rx.process_async(1, inp)
        # wait 句柄必须是 NULL(SDK 官方用法)
        assert rx.lib.RockXProcessAsync.call_args[0][2] is None
        rx.input_destroy(1, inp)
        assert rx._input_refs == {}
        assert rx._packet_inputs == {}
        assert rx._keepalive == []
    assert rx.lib.RockXInputDestroy.call_count == 100


def test_asr_completion_uses_sdk_finish_signal_not_silence_heuristic():
    """ASR 完成信号必须是 SDK 的 FINISH/COMPLETE, 不能靠"结果静默 N 毫秒"。

    静默不能证明异步推理已结束: 提前销毁 input 会让 SDK 读已释放的 PCM,
    迟到回调把上一轮结果串进下一轮。
    """
    src = Path(__file__).resolve().parents[1] / (
        'board-server/pkg/core/providers/rock/asr_helper.py')
    text = src.read_text(encoding='utf-8')
    assert 'ASR_STATE_FINISH' in text
    assert 'ASR_STATE_COMPLETE' in text
    # 不再用"结果静默 N 毫秒"当完成信号
    assert '_last_event_at) > 0.8' not in text

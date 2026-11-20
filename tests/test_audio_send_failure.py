"""发送任务死亡必须唤醒完成等待(Codex review P1, 2026-11-20 故障注入实证)。

★ 根因链: _send_loop 吞掉发送异常后直接退出, queue_empty_event 不置位
  -> send_tts_message("stop") 与 continue_music 路径的
  _wait_for_audio_completion 永久挂起 -> tts/base.py 的 future.result()
  (无超时)把 TTS 消费线程卡死, 无法消费后续轮次。

修法: 发送任务终止(异常或取消)时在 finally 置位 queue_empty_event 并
  记录 send_error; 完成等待醒后检查 send_error, 失败就抛错上溯 ——
  不能只置位事件后继续走"成功 stop", 把失败伪装成成功。
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'board-server/pkg'))
from core.utils.audioRateController import AudioRateController
from core.handle.sendAudioHandle import send_tts_message, _wait_for_audio_completion


class _QuietLogger:
    """conn.logger 替身: 不炸就行"""

    def bind(self, **kw):
        return self

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _make_failed_controller():
    """构造"发送 callback 抛 OSError 后发送任务死亡"的控制器(需在事件循环内调用)。"""

    async def cb(packet):
        raise OSError("websocket boom")

    ctrl = AudioRateController(60, 0)
    ctrl.start_sending(cb)
    ctrl.add_audio(b"x")
    return ctrl


def test_sender_exception_releases_completion_wait():
    """发送异常后, 完成等待必须被释放并带出错误(旧代码: 永久挂起)。"""

    async def run():
        ctrl = _make_failed_controller()
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 2)
        assert ctrl.pending_send_task.done()
        return ctrl.send_error

    err = asyncio.run(run())
    assert isinstance(err, OSError)


def test_sender_cancel_releases_completion_wait_without_error():
    """节拍等待中取消(轮间收尾): 等待要释放, 且不算发送失败。"""

    async def run():
        sent = []

        async def cb(p):
            sent.append(p)

        ctrl = AudioRateController(60, 0)
        ctrl.start_sending(cb)
        ctrl.add_audio(b"a")
        await asyncio.sleep(0.02)      # 首包已发出
        ctrl.add_audio(b"b")           # 次包进入 60ms 节拍等待
        ctrl.stop_sending()            # 取消落在节拍等待里, 队列非空
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 2)
        return ctrl.send_error

    assert asyncio.run(run()) is None


def test_next_round_recovers_after_sender_death():
    """发送死亡 -> reset() 清错 -> 下一轮照常发包(恢复回归)。"""

    async def run():
        ctrl = _make_failed_controller()
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 2)
        ctrl.reset()
        assert ctrl.send_error is None
        sent = []

        async def cb(p):
            sent.append(p)

        ctrl.start_sending(cb)
        ctrl.add_audio(b"a")
        ctrl.add_audio(b"b")
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 2)
        return sent

    assert asyncio.run(run()) == [b"a", b"b"]


def test_wait_for_audio_completion_raises_on_sender_death():
    """完成等待醒后必须把失败抛上去, 不能静默返回(不伪装成功)。"""

    async def run():
        ctrl = _make_failed_controller()
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 2)
        conn = SimpleNamespace(logger=_QuietLogger(), config={},
                               audio_rate_controller=ctrl)
        await asyncio.wait_for(_wait_for_audio_completion(conn), 2)

    with pytest.raises(RuntimeError):
        asyncio.run(run())


def test_stop_message_fails_fast_on_sender_death():
    """stop 收尾路径: 发送已死必须快速失败, 不挂起、不伪装成成功 stop。"""

    async def run():
        ctrl = _make_failed_controller()
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 2)
        conn = SimpleNamespace(logger=_QuietLogger(), config={},
                               session_id="s1", sentence_id="s1",
                               audio_rate_controller=ctrl)
        await asyncio.wait_for(send_tts_message(conn, "stop", None), 2)

    with pytest.raises(RuntimeError):
        asyncio.run(run())


def _interrupted_round_controller():
    """模拟"轮N被打断(sentence_id 变化)-> reset 取消活 sender"的前置状态:
    轮N sender 活着、两包在节拍队列里。返回 controller(同步函数)。"""

    async def cb(p):
        pass

    ctrl = AudioRateController(60, 0)
    ctrl.start_sending(cb)
    ctrl.add_audio(b"n1")
    ctrl.add_audio(b"n2")
    return ctrl


def test_reset_canceled_sender_does_not_prematurely_set_empty_event():
    """轮间 reset 取消旧 sender: 旧任务的 finally 不得在队列非空时置位事件。

    三轮 review 实证(2026-11-20): 无 guard 时旧任务 finally 无条件 set,
    落在新一轮批次已入队、还在预缓冲的窗口上 -> 完成等待提前放行。
    不变式: 事件置位 => 队列必须已空。
    """

    async def run():
        ctrl = _interrupted_round_controller()
        await asyncio.sleep(0.03)        # 轮N sender 进入节拍等待(活着)
        ctrl.reset()                     # 轮N+1: 取消活 sender
        ctrl.prebuffer_ms = 4000         # 板端配置: 新 sender 攒预缓冲
        async def cb(p):
            pass
        ctrl.start_sending(cb)
        for i in range(10):              # 单批短回答, 批内无让出点
            ctrl.add_audio(bytes([i]))
        await asyncio.sleep(0.05)        # 首个让出点: 旧任务 finally 在此投递
        if ctrl.queue_empty_event.is_set():
            assert len(ctrl.queue) == 0, (
                f"事件置位但队列还有 {len(ctrl.queue)} 包"
                "(stale finally 竞态, stop 会在排空前发出)")
        ctrl.pending_send_task.cancel()

    asyncio.run(run())


def test_short_round_not_truncated_by_stale_cancel():
    """短回答不得被 stale 取消截断: 完成等待放行时音频必须全部发出。"""

    async def run():
        sent = []

        async def cb(p):
            sent.append(p)

        ctrl = _interrupted_round_controller()
        await asyncio.sleep(0.03)
        ctrl.reset()
        ctrl.prebuffer_ms = 4000
        ctrl.start_sending(cb)
        for i in range(10):
            ctrl.add_audio(bytes([i]))
        ctrl.finish_production()         # 单句短回答: 这批就是全部
        await asyncio.wait_for(ctrl.queue_empty_event.wait(), 3)
        return len(sent)

    assert asyncio.run(run()) == 10

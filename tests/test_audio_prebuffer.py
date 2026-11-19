"""逐句供音不能由短空闲提前起播；真正结束的短回答及时放行。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'board-server/pkg'))
from core.utils.audioRateController import AudioRateController


def test_sentence_gap_does_not_release_first_sentence():
    async def run():
        # send_delay=0: 按 frame_duration(60ms) 节奏发。
        # 网关现在按真实音频时长(1x)下发, 67 包要 ~4s, 超时按此放宽。
        c = AudioRateController(60, 0)
        c.prebuffer_ms = 4000
        sent = []
        async def send(packet): sent.append(packet)
        for _ in range(38): c.add_audio(b'a')  # 首句 2280ms，不能误当短回答
        task = asyncio.create_task(c.check_queue(send))
        try:
            await asyncio.sleep(.5)
            assert not sent
            for _ in range(29): c.add_audio(b'b')
            await asyncio.wait_for(task, 6)
            assert len(sent) == 67
        finally:
            task.cancel()
    asyncio.run(run())


def test_finished_short_reply_releases_without_waiting_for_threshold():
    async def run():
        c = AudioRateController(60, 1)
        c.prebuffer_ms = 4000
        sent = []
        async def send(packet): sent.append(packet)
        c.add_audio(b'a')
        task = asyncio.create_task(c.check_queue(send))
        await asyncio.sleep(.04)
        assert not sent
        c.finish_production()
        await asyncio.wait_for(task, .3)
        assert sent == [b'a']
    asyncio.run(run())


def test_prebuffer_wait_does_not_become_catchup_burst():
    async def run():
        c = AudioRateController(60, 40)
        c.prebuffer_ms = 240
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        c.add_audio(b'a')
        task = asyncio.create_task(c.check_queue(send))
        await asyncio.sleep(.15)
        for _ in range(3): c.add_audio(b'b')
        await asyncio.wait_for(task, 1)
        assert min(b-a for a,b in zip(sent,sent[1:])) >= .025
    asyncio.run(run())


def test_prebuffer_timeout_is_bounded():
    async def run():
        c = AudioRateController(60, 1)
        c.prebuffer_ms = 100
        c.add_audio(b'a')
        sent = []
        async def send(packet): sent.append(packet)
        await asyncio.wait_for(c.check_queue(send), .5)
        assert sent == [b'a']
    asyncio.run(run())


def test_prebuffer_deadline_anchored_to_production_liveness():
    """★ 2026-11-19 短回答半截放行修复(实机 live7: 0.5s 段 3 次 EPIPE)。

    旧实现的 deadline 是"等待开始后 prebuffer_ms 硬上限": 慢生产(第二包
    晚到但在窗口内)在硬上限处被半截放行, 首句播完第二句还没合成 ->
    声卡干转 EPIPE。新实现锚定"距上次产音": 生产还活着就继续攒。
    """
    async def run():
        c = AudioRateController(60, 1)
        c.prebuffer_ms = 200
        sent = []
        async def send(packet): sent.append(packet)
        c.add_audio(b'a')
        task = asyncio.create_task(c.check_queue(send))
        try:
            await asyncio.sleep(.10)
            c.add_audio(b'b')       # t=0.10 存活刷新; 总量 120ms 仍 < 200ms
            await asyncio.sleep(.15)  # t=0.25: 旧硬上限(0.20)已过, 距末包 0.15 < 窗口
            assert not sent, "生产仍存活时不得按起播硬上限半截放行"
            c.finish_production()   # 生产结束 -> 立即放行全部
            await asyncio.wait_for(task, 1)
            assert sent == [b'a', b'b']
        finally:
            task.cancel()
    asyncio.run(run())


def test_liveness_window_extends_by_lead():
    """存活窗口 = prebuffer + lead(实机 live8: 句间供给 4.27s > prebuffer 4s)。

    lead 存在时句间隙按 lead 尺度额外容忍; 生产真死(超窗)仍及时放行。
    """
    async def run():
        c = AudioRateController(60, 1)
        c.prebuffer_ms = 200
        c.lead_ms = 300            # 窗口 = 500ms
        sent = []
        async def send(packet): sent.append(packet)
        c.add_audio(b'a')          # 60ms < 200ms 阈值
        task = asyncio.create_task(c.check_queue(send))
        try:
            await asyncio.sleep(.30)  # 沉默 0.30s: > prebuffer(0.20), < 窗口(0.50)
            assert not sent, "窗口应按 prebuffer+lead 容忍句间供给沉默"
            await asyncio.sleep(.25)  # 沉默 0.55s: 超窗 -> 放行手里有的
            assert sent == [b'a'], "生产真死后应放行(有界等待保护)"
        finally:
            task.cancel()
    asyncio.run(run())


def test_reset_rearms_completion_and_buffer():
    c = AudioRateController()
    c.finish_production()
    c._prebuffer_done = True
    c.reset()
    assert not c._production_finished
    assert not c._prebuffer_done


def test_no_prebuffer_sends_immediately():
    async def run():
        c = AudioRateController()
        sent = []
        async def send(packet): sent.append(packet)
        c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), .1)
        assert sent == [b'a']
    asyncio.run(run())


def test_send_path_respects_prebuffer_and_stop_flushes_short_reply():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from config.logger import setup_logging
    from core.handle.sendAudioHandle import sendAudio, _wait_for_audio_completion
    async def run():
        conn = SimpleNamespace(config={'tts_prebuffer_ms':4000, 'tts_audio_send_delay':0},
            sentence_id='one', client_abort=False, conn_from_mqtt_gateway=False,
            websocket=SimpleNamespace(send=AsyncMock()), logger=setup_logging())
        await sendAudio(conn, [b'a', b'b'])
        await asyncio.sleep(.05)
        conn.websocket.send.assert_not_awaited()
        await asyncio.wait_for(_wait_for_audio_completion(conn), 1)
        assert conn.websocket.send.await_count == 2
        conn.audio_rate_controller.stop_sending()
        await asyncio.gather(conn.audio_rate_controller.pending_send_task,
                             return_exceptions=True)
    asyncio.run(run())


def test_fast_sender_bounds_audio_ahead_and_does_not_bank_idle_time(monkeypatch):
    from types import SimpleNamespace
    import core.utils.audioRateController as module
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    async def sleep(seconds): clock[0] += max(seconds, .000001)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    async def run():
        c = AudioRateController(60, 20)
        c.max_ahead_ms = 120
        sent = []
        async def send(packet): sent.append(clock[0])
        for _ in range(10): c.add_audio(b'a')
        await c.check_queue(send)
        assert sent[-1] >= .479
        ahead = 0.0
        previous = sent[0]
        for t in sent:
            ahead = max(0, ahead - (t-previous)*1000) + 60
            assert ahead <= 120.01
            previous = t
        # 长空闲不得攒出超额额度，下一批仍须遵守同一个积压上限。
        clock[0] += 10
        sent.clear()
        for _ in range(10): c.add_audio(b'b')
        await c.check_queue(send)
        assert sent[-1]-sent[0] >= .479
        c.reset()
        assert c._ahead_ms == 0 and c._ahead_updated is None
    asyncio.run(run())


def test_lead_without_max_ahead_gets_default_cap():
    """★ 2026-11-19 review: lead 单独开启必须有积压上限兜底。

    句间隙把客户端积压越积越多(re-anchor 后每个间隙净攒 lead-间隙),
    无上限的长回答会溢出客户端环形缓冲 -> 静音丢整段。
    显式配 tts_max_ahead_ms 尊重配置; 没配则兜 max(6*lead, 12000)。
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from config.logger import setup_logging
    from core.handle.sendAudioHandle import sendAudio

    async def run():
        conn = SimpleNamespace(
            config={'tts_lead_ms': 2000, 'tts_audio_send_delay': 0},
            sentence_id='lead-only', client_abort=False,
            conn_from_mqtt_gateway=False,
            websocket=SimpleNamespace(send=AsyncMock()),
            logger=setup_logging())
        await sendAudio(conn, [b'a'])
        rc = conn.audio_rate_controller
        assert rc.lead_ms == 2000
        assert rc.max_ahead_ms == 12000, \
            f"lead 无 cap 时应兜 max(6*lead,12000), 实际 {rc.max_ahead_ms}"
        rc.stop_sending()
        await asyncio.gather(rc.pending_send_task, return_exceptions=True)

    asyncio.run(run())


def test_lead_with_explicit_max_ahead_not_overridden():
    """显式配置的 tts_max_ahead_ms 不被兜底值覆盖。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from config.logger import setup_logging
    from core.handle.sendAudioHandle import sendAudio

    async def run():
        conn = SimpleNamespace(
            config={'tts_lead_ms': 2000, 'tts_max_ahead_ms': 3000,
                    'tts_audio_send_delay': 0},
            sentence_id='lead-explicit', client_abort=False,
            conn_from_mqtt_gateway=False,
            websocket=SimpleNamespace(send=AsyncMock()),
            logger=setup_logging())
        await sendAudio(conn, [b'a'])
        rc = conn.audio_rate_controller
        assert rc.lead_ms == 2000
        assert rc.max_ahead_ms == 3000
        rc.stop_sending()
        await asyncio.gather(rc.pending_send_task, return_exceptions=True)

    asyncio.run(run())

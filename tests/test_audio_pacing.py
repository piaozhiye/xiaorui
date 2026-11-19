"""下行节拍器: 发送节奏必须与真实音频时长一致, 不能快于消费速度。

★ 2026-11-19 板端实机(PC 播"小瑞小瑞"唤醒 -> 3s -> "给我讲个长一点的故事"):
  全链路跑通, 首声 5s, 故事 66.4s 播完, 但音频开头 3.24s 处有 1 处
  1.920s 断音。1.920s 恒定 = ALSA 缓冲深度(16 period x 120ms),
  写卡耗时从 120ms(卡满)突变为 1.1ms(声卡空了) + 前端 "spk underrun,
  prepare" -> EPIPE -> prepare 重启, ALSA 里的余量被丢弃。

  根因: 网关按 send_delay(20ms)/包 推进"发送进度", 但一包 opus 是 60ms
  音频, 于是网关以 3 倍速把音频灌给板端; 板端 1 倍速消费, 30s 环的
  深度先涨后回落, 回落时撞上 TTS 逐句合成的供给间隙 -> 声卡欠载。

  修法: 播放进度按**真实音频时长**推进, 网关 1 倍速下发, 板端环深度恒定,
  供给间隙时环里还有存量可以桥接。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'board-server/pkg'))
from core.utils.audioRateController import AudioRateController


def test_play_position_advances_by_real_audio_duration():
    """播放进度按每包真实音频时长(60ms)推进, 不是 send_delay(20ms)。

    send_delay 是"两包之间的最短间隔"(下限), 不是播放进度步长。
    """
    c = AudioRateController(60, 20)
    assert c.frame_duration == 60
    assert c.interval_ms == 20          # 发送间隔下限仍是 20ms
    for _ in range(10):
        c.play_position += c._packet_play_ms()
    assert c.play_position == 600        # 10 包 x 60ms 真实音频, 不是 200


def test_gateway_does_not_send_faster_than_realtime():
    """核心回归: 网关不能快于 1 倍速下发, 否则板端环深度会先涨后回落。"""
    async def run():
        c = AudioRateController(60, 20)
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        for _ in range(10): c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), 3)
        assert len(sent) == 10
        span = sent[-1] - sent[0]
        # 10 包之间有 9 个间隔, 每间隔 = 1 包的真实音频时长 60ms -> 540ms。
        # 允许 20ms 松弛(调度抖动)。
        assert span >= .52, f"600ms 音频只用了 {span:.3f}s, 快于 1x"

    asyncio.run(run())


def test_ahead_budget_counts_real_audio_duration():
    """积压上限按真实音频时长算: 120ms 预算只允许 2 包(2 x 60ms)。"""
    c = AudioRateController(60, 20)
    c.max_ahead_ms = 120
    assert c._ahead_step_ms() == 60
    assert c._ahead_step_ms() <= c.max_ahead_ms


def test_send_interval_still_lower_bounds_spacing():
    """send_delay 仍是两包之间的最小间隔, 不因改播放进度而失效。"""
    async def run():
        c = AudioRateController(60, 20)
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        for _ in range(5): c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), 3)
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        assert min(gaps) >= .015, f"包间隔 {min(gaps):.4f}s 小于 send_delay 下限"

    asyncio.run(run())


def test_default_controller_unchanged():
    """没有 send_delay 时行为与原来一致(按 frame_duration 节奏发)。"""
    c = AudioRateController(60)
    assert c.interval_ms == 60
    assert c.play_position == 0


def test_lead_keeps_wire_ahead_of_realtime():
    """lead>0: 稳态下网关领先实时 lead 毫秒发送, 声卡常年有余量。

    2026-11-19 同包回放实验 D(无 lead)证: 严格 1x 下 14 处 0.6s 句间隙
    全部击穿声卡(写停顿); E(lead=2s)证: 0 击穿。这里是网关侧的对应回归。
    """
    async def run():
        c = AudioRateController(60, 20)
        c.lead_ms = 2000
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        # 一次性给足 5s 音频(83 包), 队列有存量可提前发。
        for _ in range(83):
            c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), 4)
        assert len(sent) == 83
        # 全部 5s 音频应在前 (5000-2000)ms + 尾包 lead 内发完:
        # 即发送总跨度明显小于音频时长(领先 2s)。
        # 期望跨度 = 82×60ms − 2000ms = 2920ms; 放宽到 3.5s 容调度抖动。
        span = sent[-1] - sent[0]
        assert span <= 3.5, f"83 包(5s 音频)用了 {span:.2f}s, lead 没生效"

    asyncio.run(run())


def test_lead_rebuilds_after_supply_gap():
    """句间供给间隙把 lead 耗光后, 恢复供音要立即补发重建 lead。

    间隙后 play_position 远落后于墙钟, wait 为负 -> 追发; 这是断音
    修复的关键: 卡里 2s 余量被 0.6s 间隙吃掉 0.6s, 恢复后要把余量补回。
    """
    async def run():
        c = AudioRateController(60, 20)
        c.lead_ms = 2000
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        for _ in range(33):          # 2s 音频, 全部按 lead 立即发出
            c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), 3)
        assert len(sent) == 33
        t_drain = asyncio.get_running_loop().time()
        await asyncio.sleep(0.8)     # 模拟 TTS 合成间隙: 声卡吃掉 0.8s 余量
        c.add_audio(b'b')            # 新句子来了
        c.finish_production()
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(c.check_queue(send), 3)
        # 间隙后首包必须立刻发(wait 为负), 不能按 1x 干等。
        assert sent[-1] - t0 < 0.15, "供给间隙后没有立即补发重建 lead"

    asyncio.run(run())


def test_lead_zero_keeps_strict_realtime():
    """lead=0 保持严格 1x(旧行为), 不因新参数引入回归。"""
    async def run():
        c = AudioRateController(60, 20)
        assert c.lead_ms == 0
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        for _ in range(10):
            c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), 3)
        span = sent[-1] - sent[0]
        assert span >= .52, f"lead=0 时 {span:.3f}s 发完 600ms 音频, 快于 1x"

    asyncio.run(run())


def test_burst_catchup_respects_send_delay_lower_bound():
    """★ 2026-11-19 review 发现的单位 bug 回归: _last_send_at 是秒,
    与 ms 混算后 send_delay 下限恒为负(死代码) -> lead 突发以 ~0.01ms
    间隔倾泻。修复后突发包间隔也必须 >= send_delay。
    """
    async def run():
        c = AudioRateController(60, 20)
        c.lead_ms = 2000
        sent = []
        async def send(packet): sent.append(asyncio.get_running_loop().time())
        for _ in range(33):          # 2s 音频, 全额走 lead 追发突发
            c.add_audio(b'a')
        await asyncio.wait_for(c.check_queue(send), 4)
        assert len(sent) == 33
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        assert min(gaps) >= .015, \
            f"突发包间隔 {min(gaps):.4f}s < send_delay 下限 20ms(单位又坏了)"

    asyncio.run(run())

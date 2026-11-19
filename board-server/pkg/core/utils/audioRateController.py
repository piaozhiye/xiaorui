import time
import asyncio
from collections import deque
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class AudioRateController:
    """
    音频速率控制器 - 按照60ms帧时长精确控制音频发送
    解决高并发下的时间累积误差问题
    """

    def __init__(self, frame_duration=60, send_delay=0):
        """
        Args:
            frame_duration: 单个音频帧时长（毫秒），默认60ms
            send_delay: 两包之间的**最小发送间隔**（毫秒），只是节流下限:
                        播放进度始终按真实音频时长(frame_duration)以 1x 推进,
                        不会因 send_delay 变小而加速。
                        0 = 不额外节流；>0 = 突发/追发时也保证包间隔 ≥ 此值
                        (大于 frame_duration 的值不生效, 节奏由播放进度决定)
        """
        # 流控间隔（毫秒）：send_delay > 0 时用配置值，否则用 frame_duration
        self.interval_ms = send_delay if send_delay > 0 else frame_duration
        self.queue = deque()
        self.play_position = 0  # 虚拟播放位置（毫秒）
        self.start_timestamp = None  # 开始时间戳（只读，不修改）
        self.pending_send_task = None
        self.logger = logger
        self.queue_empty_event = asyncio.Event()  # 队列清空事件
        self.queue_empty_event.set()  # 初始为空状态
        self.queue_has_data_event = asyncio.Event()  # 队列数据事件
        self._last_queue_empty_time = 0  # 上次队列清空的时间（秒）
        # 起播预缓冲(2026-11-19 板端"一句里字字顿"第二层根因):
        # 生产端(RKLLM+RockTTS)逐句流水, 平均 1.5x 实时但按句突发。
        # 起播就把手里那点音频放完 -> 第二句还在合成 -> 设备端空转
        # (板端实测写卡时序出现 2.3s/1.9s 停顿, 直接可听)。
        # prebuffer_ms>0 时, 首包要等队列攒够这么多音频再发;
        # 短回答永远攒不够，由生产结束标记放行。
        self.prebuffer_ms = 0
        self.max_ahead_ms = 0  # 0 保持原节奏；>0 限制客户端最多积压的音频量
        # 持久 wire 提前量(2026-11-19 断音第三层根因, 同包回放实验 D/E 证实):
        # 严格 1x 节拍下, 起播预缓冲被播放线程搬进声卡后 lead 归零, 声卡
        # just-in-time 空转; TTS 逐句合成的供给间隙(0.4~0.8s)一旦击穿,
        # 前端只能排空声卡重建流(EPIPE-prepare 还会丢 1.92s 余量)。
        # lead_ms>0 时发送目标从 play_position 改为 play_position-lead:
        # lead 不足立即补发(句间隙后自动重建), 到位后恢复 1x —— 声卡
        # 缓冲常年保有 lead 深度, 间隙被整段吸收(实验 E: 14 处 0.6s 间隙
        # 0 写停顿 0 EPIPE; 对照 D 无 lead: 14 处写停顿全中)。
        self.lead_ms = 0
        self.frame_duration = frame_duration
        self._prebuffer_done = False
        self._last_add_time = 0.0
        self._production_finished = False
        self._ahead_ms = 0.0
        self._ahead_updated = None
        self._last_send_at = None

    def reset(self):
        """重置控制器状态"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            # 取消任务后，任务会在下次事件循环时清理，无需阻塞等待

        self.queue.clear()
        self.play_position = 0
        self.start_timestamp = None  # 由首个音频包设置
        self._last_queue_empty_time = 0  # 重置时间
        self._prebuffer_done = False    # 新一轮重新攒缓冲
        self._last_add_time = 0.0
        self._production_finished = False
        self._ahead_ms = 0.0
        self._ahead_updated = None
        self._last_send_at = None
        # 注: prebuffer_ms/max_ahead_ms/lead_ms 与构造参数一样属于"每轮
        # 由 sendAudioHandle 重设"的旋钮, reset() 不清零(与 prebuffer 一致)。
        # 相关事件处理
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()

    def add_audio(self, opus_packet):
        """添加音频包到队列"""
        # 如果队列之前为空，需要调整时间戳以保持播放时间连续
        # 这样工具调用等待期间，新加入的音频不会提前播放
        # 如果间隔很短（<1帧），说明是正常的流式传输，不需要重置
        if len(self.queue) == 0 and self.play_position > 0:
            elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
            # 只有间隔超过1帧时长，才认为是真正的"暂停恢复"
            if elapsed_since_empty >= self.interval_ms:
                self.start_timestamp = time.monotonic() - (self.play_position / 1000)
                self.logger.bind(tag=TAG).debug(
                    f"队列从空恢复，重置时间戳，当前播放位置: {self.play_position}ms，间隔: {elapsed_since_empty:.0f}ms"
                )

        self.queue.append(("audio", opus_packet))
        self._last_add_time = time.monotonic()
        # 相关事件处理
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def add_message(self, message_callback):
        """
        添加消息到队列（立即发送，不占用播放时间）

        Args:
            message_callback: 消息发送回调函数 async def()
        """
        if len(self.queue) == 0 and self.play_position > 0:
            elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
            if elapsed_since_empty >= self.interval_ms:
                self.start_timestamp = time.monotonic() - (self.play_position / 1000)
                self.logger.bind(tag=TAG).debug(
                    f"队列从空恢复，重置时间戳，当前播放位置: {self.play_position}ms，间隔: {elapsed_since_empty:.0f}ms"
                )

        self.queue.append(("message", message_callback))
        # 相关事件处理
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def _get_elapsed_ms(self):
        """获取已经过的时间（毫秒）"""
        if self.start_timestamp is None:
            return 0
        return (time.monotonic() - self.start_timestamp) * 1000

    def _packet_play_ms(self):
        """一包音频对应的真实播放时长（毫秒）。

        ★ 2026-11-19 板端实机断音根因:
          播放进度曾用 send_delay(20ms) 推进, 但一包 opus 是 60ms 音频。
          网关因此以为"20ms 该发一包", 以 3 倍速把音频灌给板端; 板端按
          1 倍速消费, 30s 环的深度先涨后回落, 回落时撞上 TTS 逐句合成的
          供给间隙 -> 声卡欠载 -> EPIPE -> prepare 重启 -> 丢掉 ALSA 里
          1.92s 的余量(实测音频开头 3.24s 处一处 1.920s 断音)。
          播放进度必须按**真实音频时长**推进: 网关 1 倍速下发, 板端环
          深度恒定, 供给间隙时环里还有存量可以桥接。
          send_delay 保留为"两包之间的最小间隔"(下限), 不再决定播放速度。
        """
        return self.frame_duration

    def _ahead_step_ms(self):
        """积压一包增加多少毫秒（客户端已收到但还没播完的音频时长）。"""
        return self.frame_duration

    def _queued_audio_ms(self):
        """队列里还没发出的音频时长(毫秒)"""
        n = sum(1 for it in self.queue if it[0] == "audio")
        return n * self.frame_duration

    def finish_production(self):
        """LAST/stop 已到：后续不会再有本轮音频，短回答无需继续攒阈值。"""
        self._production_finished = True

    async def _wait_prebuffer(self):
        """攒够音频或生产结束才起播；超时按"生产存活"锚定。

        句间空闲不代表回答结束，不能用 400ms 无新包提前放行。
        ★ 2026-11-19 短回答半截放行修复(实机 live7: 0.5s 段 3 次 EPIPE):
          原来的 deadline 是"起播等待开始后 prebuffer_ms 硬上限"。中速生产
          (首句 3.5s 到、第二句 5.5s 到)在 4s 处被硬上限放行 —— 此时队列
          里只有第一句, 播完第二句还没合成, 声卡干转 -> EPIPE。
          改为: 只要距上次产音 < prebuffer+lead 且还没攒够, 就继续攒;
          生产真停了才放行手里有的。窗口加 lead 是因为句间供给间隙
          本来就该按 lead 的尺度容忍(实机 live8: cooperative 交错下句间
          供给最大观测 4.27s > prebuffer 4s, 只按 prebuffer 仍会放碎渣)。
          lead=0 时窗口=prebuffer, 与旧行为一致。
        """
        window_s = (self.prebuffer_ms + self.lead_ms) / 1000.0
        while (self.prebuffer_ms > 0
               and self._queued_audio_ms() < self.prebuffer_ms
               and not self._production_finished
               and time.monotonic() - self._last_add_time < window_s):
            await asyncio.sleep(0.02)
        self._prebuffer_done = True
        if self._production_finished:
            reason = "生产结束"
        elif self._queued_audio_ms() >= self.prebuffer_ms:
            reason = "攒够阈值"
        else:
            reason = "存活超窗(生产停滞, 注意防碎渣)"
        self.logger.bind(tag=TAG).debug(
            f"起播预缓冲放行[{reason}]: 队列 {self._queued_audio_ms()}ms "
            f"(目标 {self.prebuffer_ms}ms, lead {self.lead_ms}ms, "
            f"生产结束={self._production_finished})"
        )

    async def check_queue(self, send_audio_callback):
        """
        检查队列并按时发送音频/消息

        Args:
            send_audio_callback: 发送音频的回调函数 async def(opus_packet)
        """
        while self.queue:
            item = self.queue[0]
            item_type = item[0]

            if item_type == "message":
                # 消息类型：立即发送，不占用播放时间
                _, message_callback = item
                self.queue.popleft()
                try:
                    await message_callback()
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"发送消息失败: {e}")
                    raise

            elif item_type == "audio":
                if not self._prebuffer_done:
                    await self._wait_prebuffer()

                # 起播前的等待不算发送进度，避免放行后瞬间追发整段音频。
                if self.start_timestamp is None:
                    self.start_timestamp = time.monotonic()

                _, opus_packet = item

                # 循环等待直到时间到达
                while True:
                    # 计算时间差
                    elapsed_ms = self._get_elapsed_ms()
                    output_ms = self.play_position

                    # lead: 发送目标从 play_position 前移 lead_ms。
                    # lead 不足(句间供给间隙后)wait 为负 -> 立即补发重建;
                    # lead 到位 -> 恢复 1x。声卡缓冲常年保有 ~lead 深度。
                    wait_ms = output_ms - elapsed_ms - self.lead_ms
                    # send_delay 是"两包之间的最小间隔"下限, 与播放进度分开算:
                    # 播放进度按真实音频时长推进(1x), 发送间隔只是额外节流。
                    # ★ 注意单位: _last_send_at 存的是 monotonic 秒, 与 ms
                    #   混算会让下限恒为负(死代码, 2026-11-19 review 发现
                    #   lead 突发以 ~0.01ms 间隔倾泻)。统一换算成 ms 再比。
                    if self.interval_ms < self.frame_duration:
                        last = self._last_send_at
                        if last is not None:
                            wait_ms = max(
                                wait_ms,
                                (last + self.interval_ms / 1000.0
                                 - time.monotonic()) * 1000.0)
                    if self.max_ahead_ms > 0:
                        now = time.monotonic()
                        if self._ahead_updated is not None:
                            self._ahead_ms = max(0.0, self._ahead_ms -
                                                 (now - self._ahead_updated) * 1000)
                        self._ahead_updated = now
                        # 积压按实际音频时长增加，以 1x 实时消耗，空闲时最低为零。
                        # 不能用 20ms 发送间隔当成 60ms 音频的播放进度。
                        wait_ms = max(wait_ms, self._ahead_ms + self._ahead_step_ms()
                                      - max(self.frame_duration, self.max_ahead_ms))
                    if wait_ms > 0.001:
                        # 等待后继续检查（允许被中断）
                        try:
                            await asyncio.sleep(wait_ms / 1000)
                        except asyncio.CancelledError:
                            self.logger.bind(tag=TAG).debug("音频发送任务被取消")
                            raise
                        # 等待结束后重新检查时间（循环回到 while True）
                    else:
                        # 时间已到，跳出等待循环
                        break

                # 时间已到，从队列移除并发送
                self.queue.popleft()
                # 播放进度按**真实音频时长**推进: 网关 1 倍速下发, 板端环深度
                # 恒定, 供给间隙时环里还有存量可桥接(2026-11-19 实机断音修复)。
                self.play_position += self._packet_play_ms()
                self._last_send_at = time.monotonic()
                if self.max_ahead_ms > 0:
                    self._ahead_ms += self._ahead_step_ms()
                try:
                    await send_audio_callback(opus_packet)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"发送音频失败: {e}")
                    raise

        # 队列处理完后清除事件
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()
        self._last_queue_empty_time = time.monotonic()  # 记录队列清空时间

    def start_sending(self, send_audio_callback):
        """
        启动异步发送任务

        Args:
            send_audio_callback: 发送音频的回调函数

        Returns:
            asyncio.Task: 发送任务
        """

        async def _send_loop():
            try:
                while True:
                    # 等待队列数据事件，不轮询等待占用CPU
                    await self.queue_has_data_event.wait()

                    await self.check_queue(send_audio_callback)
            except asyncio.CancelledError:
                self.logger.bind(tag=TAG).debug("音频发送循环已停止")
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"音频发送循环异常: {e}")

        self.pending_send_task = asyncio.create_task(_send_loop())
        return self.pending_send_task

    def stop_sending(self):
        """停止发送任务"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            self.logger.bind(tag=TAG).debug("已取消音频发送任务")

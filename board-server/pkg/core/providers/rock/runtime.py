"""Rock 引擎(NPU 模型)的驻留仲裁 —— 2GB 板子的内存纪律。

实测数据(RK3562 AIOT LP4X V10, 1963MB, 2026-11-18, /proc/<pid>/smaps_rollup):
    RockASR helper   Rss 621MB = 558MB NPU 模型(shmem/DRM GEM, 内核不可回收) + 33MB anon
    RockTTS helper   Rss 232MB = 229MB 同上
    RKLLM            Private_Dirty 604MB(不可回收)
    网关+前端+UI+系统基线        ~280MB
    三个模型同时驻留 = 1704MB / 1963MB, 余量只剩 259MB
    -> 内核 OOM 干掉 init -> 整机黑屏(2026-11-18 实测三次)

仲裁规则:
    1. 每个 helper 占一个"槽位", 槽位知道自己大概占多少内存。
    2. 拉起模型前先看 MemAvailable, 不够就把别的槽位停掉腾地方。
       谁让位由真实可用内存决定, 不写死"谁大谁小"这种规则。
    3. 空闲一段时间就卸载 helper, 把 NPU 内存还给系统。
    4. prefetch(): 语音开始时提前拉起, 把 ~3s 的加载耗时藏在用户说话的时间里。
"""
import os
import threading
import time

from core.providers.rock.helper_proc import HelperProcess

# 加载模型后至少要留这么多可用内存, 低于它就认为"不能再占"
RESERVE_MB = int(os.environ.get("XR_ROCK_MEM_RESERVE_MB", "400"))
# zram 空闲空间折算成"等效可用内存"时用的压缩比。
# zstd 在这些页上实测能压到 1/4.4(罗总开发笔记), 保守取 2。
COMPRESS_RATIO = float(os.environ.get("XR_ROCK_COMPRESS_RATIO", "2"))


def _mem_available_raw_mb():
    """MemAvailable(MB); 读不到返回一个大值(宁可多占也别误卸载)。"""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        pass
    return 99999


def _zram_free_mb():
    """zram 还能吃下多少(按压缩后的物理占用算, MB)。"""
    try:
        with open("/sys/block/zram0/disksize") as f:
            size = int(f.read().strip())
        used = 0
        try:
            with open("/sys/block/zram0/mm_stat") as f:
                for line in f:
                    if line.startswith("orig_data_size:"):
                        used = int(line.split()[1])
                        break
        except Exception:  # noqa: BLE001
            pass
        # mm_stat 里的 orig_data_size 是"原始(未压缩)数据量", 不是物理占用;
        # 物理占用看 mem_used_total。两者都取, 保守用小的。
        phys = 0
        try:
            with open("/sys/block/zram0/mm_stat") as f:
                for line in f:
                    if line.startswith("mem_used_total:"):
                        phys = int(line.split()[1])
                        break
        except Exception:  # noqa: BLE001
            pass
        if phys:
            return max(0, (size - phys) // (1024 * 1024))
        return max(0, (size - used) // (1024 * 1024))
    except Exception:  # noqa: BLE001
        return 0


def mem_available_mb():
    """等效可用内存 = MemAvailable + zram 还能吃下的量(折算成未压缩大小)。

    有 zram 兜底时, 三个 NPU 模型可以同时驻留(罗总开发笔记的方案:
    zram 768M zstd + swappiness 80); 没 zram 时就退化成纯 RAM 口径,
    runtime 会让模型互相让位 —— 两条路都能活。
    """
    return _mem_available_raw_mb() + int(_zram_free_mb() * COMPRESS_RATIO)


_SLOTS = {}
_SLOTS_LOCK = threading.RLock()


class RockEngineSlot:
    """一个 rock helper 的驻留槽位(ASR 一个, TTS 一个)。

    - acquire(): 真正要用模型时调用。保证内存够, 保证自己活着。
    - prefetch(): 提前把模型拉起来(后台线程), 给用模型前留出加载时间。
    - stop(): 立刻卸载, 释放 NPU 内存。
    """

    def __init__(self, kind, factory, footprint_mb, idle_timeout_s=60.0):
        self.kind = kind
        self._factory = factory
        self.footprint_mb = int(footprint_mb)
        self.idle_timeout_s = float(idle_timeout_s)
        self._helper = None
        self._idle_timer = None
        self._lock = threading.RLock()

    # ---------- 内部 ----------
    def _cancel_idle(self):
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle(self):
        self._cancel_idle()
        if self.idle_timeout_s <= 0:
            return
        timer = threading.Timer(self.idle_timeout_s, self._idle_stop)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _idle_stop(self):
        """空闲到期。内存还够(zram 兜底)就留着, 省下次的加载延迟。"""
        with _SLOTS_LOCK:
            with self._lock:
                self._cancel_idle()
                if mem_available_mb() >= RESERVE_MB + self.footprint_mb:
                    self._schedule_idle()
                    return
                helper = self._helper
                self._helper = None
            if helper is None:
                return
            try:
                helper.stop()
            except Exception:  # noqa: BLE001
                pass

    def _make_room(self):
        """内存不够时, 把别的槽位停掉腾地方。

        返回是否腾出了足够的空间。
        """
        need = self.footprint_mb + RESERVE_MB
        others = [s for k, s in _SLOTS.items() if k != self.kind]
        # 先停占内存大的, 一次能腾出最多
        others.sort(key=lambda s: s.footprint_mb, reverse=True)
        for other in others:
            if mem_available_mb() >= need:
                break
            other._idle_stop()
        return mem_available_mb() >= need

    # ---------- 对外 ----------
    def acquire(self):
        """拿到可用的 helper: 必要时腾内存, 必要时拉起。"""
        with _SLOTS_LOCK:
            with self._lock:
                self._cancel_idle()
                if self._helper is None or not self._helper.alive:
                    self._make_room()
                    helper = self._factory()
                    helper.start()
                    self._helper = helper
                self._schedule_idle()
                return self._helper

    def prefetch(self):
        """后台预热: 不阻塞调用方。已经在跑就什么都不做。"""
        with _SLOTS_LOCK:
            with self._lock:
                self._cancel_idle()
                if self._helper is not None and self._helper.alive:
                    self._schedule_idle()
                    return
        threading.Thread(target=self._prefetch_worker, daemon=True).start()

    def _prefetch_worker(self):
        try:
            self.acquire()
        except Exception:  # noqa: BLE001
            # 预热失败不影响主流程: 真正请求时 acquire() 会再拉一次
            pass

    def stop(self):
        with _SLOTS_LOCK:
            with self._lock:
                self._cancel_idle()
                helper = self._helper
                self._helper = None
            if helper is not None:
                try:
                    helper.stop()
                except Exception:  # noqa: BLE001
                    pass


def get_slot(kind, factory, footprint_mb, idle_timeout_s=60.0):
    """按名字取(创建)一个槽位。同名槽位是全局唯一的。"""
    with _SLOTS_LOCK:
        slot = _SLOTS.get(kind)
        if slot is None:
            slot = RockEngineSlot(kind, factory, footprint_mb, idle_timeout_s)
            _SLOTS[kind] = slot
        return slot


def stop_all():
    """全部卸载(网关退出/回滚时用)。"""
    with _SLOTS_LOCK:
        for slot in list(_SLOTS.values()):
            slot.stop()


def reset_slots():
    """清空槽位注册表(测试用: 每个用例要独立的槽位)。"""
    with _SLOTS_LOCK:
        for slot in list(_SLOTS.values()):
            slot.stop()
        _SLOTS.clear()


__all__ = ["RockEngineSlot", "get_slot", "stop_all", "reset_slots",
           "mem_available_mb", "RESERVE_MB", "HelperProcess"]

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

from core.providers.rock.helper_proc import HelperError, HelperProcess

# 加载模型后至少要留这么多可用内存, 低于它就认为"不能再占"
RESERVE_MB = int(os.environ.get("XR_ROCK_MEM_RESERVE_MB", "128"))
# 压缩比仅用于估算换出冷 anon 后释放的净 RAM，不扩张 swap 逻辑容量。
COMPRESS_RATIO = max(1.0, float(os.environ.get("XR_ROCK_COMPRESS_RATIO", "2")))


def _meminfo():
    try:
        with open("/proc/meminfo") as source:
            return {line.split(":", 1)[0]: int(line.split()[1]) // 1024
                    for line in source if ":" in line and len(line.split()) >= 2}
    except (OSError, ValueError):
        return {}


def _mem_available_raw_mb():
    return _meminfo().get("MemAvailable", 0)


def _zram_free_mb():
    """只计算已启用 zram 的空闲逻辑 swap；未启用/读取失败按零处理。"""
    try:
        with open("/proc/swaps") as source:
            for line in source:
                fields = line.split()
                if fields and os.path.basename(fields[0]) == "zram0":
                    return max(0, (int(fields[2]) - int(fields[3])) // 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0


def mem_available_mb():
    """RAM + 可换出冷匿名页的净回收量；DRM/NPU 页不能靠 swap 扩容。"""
    info = _meminfo()
    cold_anon = info.get("Inactive(anon)", 0)
    reclaim = min(_zram_free_mb(), cold_anon) * (1.0 - 1.0 / COMPRESS_RATIO)
    return info.get("MemAvailable", 0) + int(reclaim)


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
        self._prefetch_lock = threading.Lock()

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
                if mem_available_mb() >= RESERVE_MB:
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
            other.stop()
        return mem_available_mb() >= need

    # ---------- 对外 ----------
    def acquire(self):
        """拿到可用的 helper: 必要时腾内存, 必要时拉起。"""
        with _SLOTS_LOCK:
            with self._lock:
                self._cancel_idle()
                if self._helper is None or not self._helper.alive:
                    if not self._make_room():
                        raise HelperError(f"{self.kind} 内存不足，拒绝加载 {self.footprint_mb}MB 模型")
                    helper = self._factory()
                    helper.start()
                    self._helper = helper
                self._schedule_idle()
                return self._helper

    def request(self, header, body=b"", timeout=60.0):
        # 仲裁锁覆盖获取和请求，避免尚在推理的 helper 被另一个槽位卸载。
        with _SLOTS_LOCK:
            helper = self.acquire()
            self._cancel_idle()
            try:
                return helper.request(header, body=body, timeout=timeout)
            finally:
                self._schedule_idle()

    def prefetch(self):
        """后台预热，调用线程不等待全局仲裁锁或正在执行的推理。"""
        with self._prefetch_lock:
            if getattr(self, "_prefetch_pending", False):
                return
            self._prefetch_pending = True
        threading.Thread(target=self._prefetch_worker, daemon=True).start()

    def _prefetch_worker(self):
        try:
            self.acquire()
        except Exception:  # noqa: BLE001
            pass  # 正式请求会重试并报告错误。
        finally:
            with self._prefetch_lock:
                self._prefetch_pending = False

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

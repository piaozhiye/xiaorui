"""Fail-closed resource/identity checks shared by soak runner and host tests."""
import time


def cleanup_confirmed(reply):
    return bool(reply and ("已取消" in reply or "没有正在进行的提醒" in reply))


class HealthGuard:
    # DMA allocations may warm up; that is not evidence they cannot leak.
    # Keep ordinary FDs strict and freeze the DMA budget after warmup.
    def __init__(self, fd_growth=8, rss_growth_kb=131072,
                 dma_growth=96, warmup_s=120):
        self.baselines = {}
        self.fd_growth = fd_growth
        self.rss_growth_kb = rss_growth_kb
        self.dma_growth = dma_growth
        self.warmup_s = warmup_s
        self.dma_baselines = {}
        self.dma_steady = {}

    @staticmethod
    def check_progress(tick_ms, now_ms, max_stall_ms=120000):
        if tick_ms <= 0 or tick_ms > now_ms or now_ms - tick_ms > max_stall_ms:
            raise RuntimeError("frontend: missing/stale main-loop heartbeat")

    def check(self, name, identity, rss, fd, dma=0, now=None):
        if not identity or rss <= 0 or fd <= 0 or dma < 0 or dma > fd:
            raise RuntimeError(f"{name}: missing/invalid process sample")
        now = time.monotonic() if now is None else now
        ordinary = fd - dma
        baseline = self.baselines.setdefault(name, (identity, rss, ordinary))
        if identity != baseline[0]:
            raise RuntimeError(f"{name}: process/boot identity changed")
        if ordinary > baseline[2] + self.fd_growth:
            raise RuntimeError(f"{name}: non-DMA fd growth {baseline[2]}->{ordinary}")
        initial_dma, started = self.dma_baselines.setdefault(name, (dma, now))
        if dma > initial_dma + self.dma_growth:
            raise RuntimeError(f"{name}: DMA fd warmup budget exceeded")
        if now - started >= self.warmup_s:
            steady = self.dma_steady.setdefault(name, dma)
            if dma > steady + self.fd_growth:
                raise RuntimeError(f"{name}: DMA fd continues growing after warmup")
        if rss > baseline[1] + self.rss_growth_kb:
            raise RuntimeError(f"{name}: RSS growth {baseline[1]}->{rss} KiB")

#!/usr/bin/env python3
"""开机问候语(欢迎语)的投递语义回归。

背景(2026-11-20 真板反馈): 网关重启后前端连上, 但 TTS 模型还在
加载(NPU 几十秒), 欢迎语投不出去 —— 而旧实现**在投出去之前**就打
上了 10 分钟去重标记, 于是这轮失败之后 10 分钟内都不再尝试,
表现就是"重启后没有'小瑞已经准备好了'"。

规则:
  - 真的投出去了(delivered=True)才记去重, 之后 10 分钟内不重复打扰
  - 没投出去 / 抛异常 -> 不记去重, 下一次连接还会再试
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "board-server/pkg"))

from core.handle import helloHandle  # noqa: E402


class _Logger:
    def bind(self, **kwargs):
        return self

    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


class _Conn:
    def __init__(self):
        self.logger = _Logger()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.delenv("XR_ROLE", raising=False)
    monkeypatch.setattr(helloHandle, "_LAST_WELCOME", 0.0)
    monkeypatch.setattr(
        "core.handle.timeIntent.spoken_now", lambda: "现在是下午三点零五分"
    )


def _patch_request(monkeypatch, delivered, boom=False):
    calls = []

    async def request(self, text, priority=0, ttl=0):
        calls.append(text)
        if boom:
            raise RuntimeError("tts not ready")
        return delivered

    monkeypatch.setattr(
        "core.session_coordinator.coordinator", lambda conn: _Coordinator(request)
    )
    return calls


class _Coordinator:
    def __init__(self, request):
        self._request = request

    async def request(self, text, priority=0, ttl=0):
        return await self._request(self, text, priority=priority, ttl=ttl)


def test_welcome_is_played_when_delivery_succeeds(monkeypatch):
    calls = _patch_request(monkeypatch, delivered=True)
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert len(calls) == 1
    assert "小瑞已经准备好了" in calls[0]
    assert "下午" in calls[0]


def test_failed_delivery_does_not_block_future_attempts(monkeypatch):
    """投失败(TTS 还没就绪)不能打上去重标记 —— 否则 10 分钟内不再尝试。"""
    calls = _patch_request(monkeypatch, delivered=False)
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert len(calls) == 1
    assert helloHandle._LAST_WELCOME == 0.0


def test_exception_does_not_block_future_attempts(monkeypatch):
    calls = _patch_request(monkeypatch, delivered=False, boom=True)
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert len(calls) == 1
    assert helloHandle._LAST_WELCOME == 0.0


def test_successful_welcome_suppresses_repeat_within_interval(monkeypatch):
    """投出去了才去重: 10 分钟内第二次连接不重复打扰。"""
    calls = _patch_request(monkeypatch, delivered=True)
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert helloHandle._LAST_WELCOME > 0
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert len(calls) == 1


def test_failed_welcome_is_retried_once(monkeypatch):
    """前端不会重连, hello 只来一次 —— 失败必须安排一次延迟重试。"""
    calls = _patch_request(monkeypatch, delivered=False)
    monkeypatch.setattr(helloHandle, "_WELCOME_RETRY_DELAY", 0.0)
    created = []
    monkeypatch.setattr(
        helloHandle.asyncio, "create_task", lambda coro: created.append(coro)
    )
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert len(created) == 1
    asyncio.run(created[0])          # 跑重试任务
    assert len(calls) == 2
    # 重试不再递归安排下一次
    assert len(created) == 1


def test_welcome_ttl_covers_cold_tts_start(monkeypatch):
    """TTS 冷启动合成要 30s+, 15s 的 ttl 必然超时失败。"""
    seen = {}

    async def request(self, text, priority=0, ttl=0):
        seen["ttl"] = ttl
        return True

    monkeypatch.setattr(
        "core.session_coordinator.coordinator", lambda conn: _Coordinator(request)
    )
    asyncio.run(helloHandle._maybe_welcome(_Conn()))
    assert seen["ttl"] >= 60

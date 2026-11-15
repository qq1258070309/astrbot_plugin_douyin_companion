"""单 worker 限频门 + 写操作闸门。

移植自 dsh-douyin ``lib/backend.js`` 的 ``runExclusive / guarded / quotaState``
以及 README 的「私信三道闸」。这两块是那个项目最值得保留的工程资产：
所有抖音后端调用串行、滚动 1 小时配额、高斯抖动、触发风控后指数退避。
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from .errors import DouyinError

T = TypeVar("T")


@dataclass(frozen=True)
class GateConfig:
    hourly_request_budget: int = 60
    call_delay_min_ms: int = 1800
    call_delay_max_ms: int = 3200
    page_delay_min_ms: int = 3000
    page_delay_max_ms: int = 8000
    max_results: int = 20
    max_search_pages: int = 3


def clamp(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def jitter_ms(min_ms: int, max_ms: int) -> int:
    """Box-Muller 高斯抖动，避免机械的固定间隔暴露机器人节奏。"""
    if max_ms <= min_ms:
        return min_ms
    mid = (min_ms + max_ms) / 2
    sigma = max(120.0, (max_ms - min_ms) / 4)
    sample = random.gauss(mid, sigma)
    return int(max(min_ms, min(max_ms, round(sample))))


class AsyncRateGate:
    """所有抖音后端请求必须经 ``guarded``，保证同一时刻只有一个 worker。"""

    def __init__(self, config: GateConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._recent: deque[float] = deque()
        self._last_attempt_at = 0.0
        self._blocked_until = 0.0
        self._backoff_level = 0
        self.counter = 0
        self.last_error: dict[str, Any] | None = None

    def quota(self) -> dict[str, Any]:
        now = time.time()
        while self._recent and now - self._recent[0] >= 3600:
            self._recent.popleft()
        used = len(self._recent)
        return {
            "limit": self.config.hourly_request_budget,
            "used": used,
            "remaining": max(0, self.config.hourly_request_budget - used),
            "window_seconds": 3600,
        }

    def backoff_seconds(self) -> float:
        return max(0.0, self._blocked_until - time.time())

    async def guarded(
        self,
        tool: str,
        task: Callable[[], Awaitable[T]],
        units: int = 1,
    ) -> T:
        async with self._lock:
            now = time.time()
            remaining_wait = self._blocked_until - now
            if remaining_wait > 0:
                raise DouyinError(
                    "rate_limited",
                    f"退避中：还需等待 {int(remaining_wait) + 1} 秒（上次触发限流/验证），不要立即重试。",
                    {"retry_after_ms": int(remaining_wait * 1000)},
                )
            quota = self.quota()
            if quota["remaining"] < units:
                raise DouyinError(
                    "rate_limited",
                    f"本小时请求预算已用尽（{quota['used']}/{quota['limit']}），请稍后再试。",
                )
            wait = 0.0
            if self._last_attempt_at:
                wait = jitter_ms(self.config.call_delay_min_ms, self.config.call_delay_max_ms) / 1000
                wait -= time.time() - self._last_attempt_at
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_attempt_at = time.time()
            for _ in range(units):
                self._recent.append(time.time())
            self.counter += 1
            try:
                result = await task()
                self._backoff_level = 0
                return result
            except DouyinError as error:
                if error.code in ("rate_limited", "challenge_required"):
                    self._backoff_level = min(self._backoff_level + 1, 5)
                    cooldown = min(15 * 60, 60 * (2 ** (self._backoff_level - 1)))
                    self._blocked_until = time.time() + cooldown
                self.last_error = {
                    "code": error.code,
                    "message": error.message,
                    "tool": tool,
                    "at": time.time(),
                }
                raise

    async def page_delay(self, signal: Any = None) -> None:
        """搜索翻页间隔：3–8 s 高斯抖动。"""
        await asyncio.sleep(jitter_ms(self.config.page_delay_min_ms, self.config.page_delay_max_ms) / 1000)

    def dispose(self) -> None:
        self._recent.clear()


class WriteGate:
    """三道闸：调用方显式确认 + 服务端总闸 + 目标白名单。

    本插件**不随包提供任何写工具**（不代用户发抖音私信、不代发评论、不发布），
    但保留这个判定器：将来若增加写能力，必须复用它，且默认仍是 dry-run。
    """

    def __init__(self, writes_enabled: bool = False, allow_targets: tuple[str, ...] = ()):
        self.writes_enabled = bool(writes_enabled)
        self.allow_targets = tuple(t.strip() for t in allow_targets if t and t.strip())

    def evaluate(self, target: str, explicit_confirm: bool) -> tuple[bool, str]:
        if not explicit_confirm:
            return False, "dry_run"
        if not self.writes_enabled:
            return False, "writes_disabled"
        if self.allow_targets and target not in self.allow_targets:
            return False, "target_not_allowed"
        return True, "allowed"

    def describe(self) -> dict[str, Any]:
        return {
            "writes_enabled": self.writes_enabled,
            "allow_targets_configured": bool(self.allow_targets),
            "target_count": len(self.allow_targets),
            "shipped_write_tools": 0,
            "default": "dry_run",
        }

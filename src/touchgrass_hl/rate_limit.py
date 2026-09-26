"""Single priority rate limiter for Hyperliquid info calls.

Higher-priority waiters block lower-priority ones. Live collector health is
priority 0. Historical wallet research is priority 3 and cannot jump the queue.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import deque

PRIORITY_LIVE = 0
PRIORITY_CANDIDATE = 1
PRIORITY_VERIFIED = 2
PRIORITY_DISCOVERY = 3


class RateLimiter:
    def __init__(self, capacity: int = 1000, window_s: float = 60.0) -> None:
        self.capacity = max(1, int(capacity))
        self.window_s = float(window_s)
        self.events: deque[tuple[float, int]] = deque()
        self.waiters: list[tuple[int, int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self._timer: asyncio.TimerHandle | None = None
        self._lock = asyncio.Lock()

    def used(self, now: float | None = None) -> int:
        self._expire(time.monotonic() if now is None else now)
        return sum(weight for _, weight in self.events)

    def available(self, now: float | None = None) -> int:
        return self.capacity - self.used(now)

    def snapshot(self) -> dict[str, int | float]:
        return {
            "capacity": self.capacity,
            "used": self.used(),
            "available": self.available(),
            "waiters": len(self.waiters),
        }

    def penalize(self, weight: int) -> None:
        self.events.append((time.monotonic(), max(1, int(weight))))

    async def acquire(self, weight: int, priority: int = PRIORITY_DISCOVERY) -> None:
        weight = max(1, int(weight))
        if weight > self.capacity:
            weight = self.capacity
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        async with self._lock:
            heapq.heappush(self.waiters, (int(priority), next(self._seq), weight, fut))
            self._pump_locked(loop)
        await fut

    def _expire(self, now: float) -> None:
        cutoff = now - self.window_s
        while self.events and self.events[0][0] <= cutoff:
            self.events.popleft()

    def _pump_locked(self, loop: asyncio.AbstractEventLoop) -> None:
        now = time.monotonic()
        self._expire(now)
        while self.waiters:
            priority, seq, weight, fut = self.waiters[0]
            if fut.done():
                heapq.heappop(self.waiters)
                continue
            used = sum(item_weight for _, item_weight in self.events)
            if used + weight <= self.capacity:
                heapq.heappop(self.waiters)
                self.events.append((now, weight))
                fut.set_result(None)
                continue
            # Do not let a smaller lower-priority job skip a blocked higher-priority one.
            delay = self._seconds_until(weight, now)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = loop.call_later(max(0.01, delay), self._on_timer)
            _ = (priority, seq)
            break

    def _seconds_until(self, weight: int, now: float) -> float:
        self._expire(now)
        used = sum(item_weight for _, item_weight in self.events)
        if used + weight <= self.capacity:
            return 0.0
        need = used + weight - self.capacity
        freed = 0
        for ts, item_weight in self.events:
            freed += item_weight
            if freed >= need:
                return max(0.0, ts + self.window_s - now)
        return self.window_s

    def _on_timer(self) -> None:
        self._timer = None
        loop = asyncio.get_running_loop()
        # Timer runs on the loop; the lock is not held. Serialize via a task.
        loop.create_task(self._pump())

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._pump_locked(loop)

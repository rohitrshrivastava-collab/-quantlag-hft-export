"""Token-bucket rate limiting for exchange REST fan-out."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional


class TokenBucket:
    def __init__(self, capacity: int, refill_per_s: float, name: str = "") -> None:
        self.name = name
        self.capacity = max(1, int(capacity))
        self.refill_per_s = float(refill_per_s)
        self.tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self.acquired = 0
        self.throttled = 0
        self.total_wait_ms = 0.0

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.refill_per_s)
        self._last = now

    async def acquire(self) -> float:
        """Block until a token is available; returns ms waited."""
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self.acquired += 1
                    self.total_wait_ms += waited
                    return waited
                need = (1.0 - self.tokens) / self.refill_per_s
                self.throttled += 1
                await asyncio.sleep(need)
                waited += need * 1000.0

    def stats(self) -> Dict[str, Any]:
        self._refill()
        return {"name": self.name, "capacity": self.capacity, "per_min": round(self.refill_per_s * 60, 1),
                "tokens": round(self.tokens, 2), "acquired": self.acquired, "throttled": self.throttled,
                "total_wait_ms": round(self.total_wait_ms, 0)}


class RateLimiterGroup:
    """Per-key buckets (e.g. per REST path) with a default profile."""
    def __init__(self, default_per_min: int, profiles: Optional[Dict[str, int]] = None) -> None:
        self.default_per_min = default_per_min
        self.profiles = profiles or {}
        self._buckets: Dict[str, TokenBucket] = {}

    def bucket(self, key: str) -> TokenBucket:
        b = self._buckets.get(key)
        if b is None:
            per_min = self.profiles.get(key, self.default_per_min)
            b = self._buckets[key] = TokenBucket(per_min, per_min / 60.0, name=key)
        return b

    async def acquire(self, key: str) -> float:
        return await self.bucket(key).acquire()

    def stats(self) -> Dict[str, Any]:
        return {"default_per_min": self.default_per_min,
                "buckets": [b.stats() for b in self._buckets.values()]}

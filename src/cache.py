"""Small hand-rolled TTL cache used across the gateway and OAuth layers.

Single-process Railway deployment, so an in-memory dict is sufficient.
Not thread-safe in the strict sense but safe enough for asyncio under a
single uvicorn worker — set/get are atomic at the GIL level.
"""
from __future__ import annotations

import time
from typing import Callable, Generic, Hashable, Optional, TypeVar


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Tiny per-key TTL cache with a default TTL and optional max size.

    Each entry stores (value, expires_at). Lazy expiry on read; the cache
    sweeps the oldest entry when the size limit is exceeded.
    """

    def __init__(self, ttl: float, maxsize: int = 1024, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._store: dict[K, tuple[V, float]] = {}
        self._clock = clock

    def get(self, key: K) -> Optional[V]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= self._clock():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: K, value: V, ttl: Optional[float] = None) -> None:
        expires_at = self._clock() + (ttl if ttl is not None else self._ttl)
        if key not in self._store and len(self._store) >= self._maxsize:
            # Evict the entry closest to expiry (cheap O(n), small caches).
            oldest = min(self._store.items(), key=lambda kv: kv[1][1])
            self._store.pop(oldest[0], None)
        self._store[key] = (value, expires_at)

    def pop(self, key: K) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: K) -> bool:
        return self.get(key) is not None

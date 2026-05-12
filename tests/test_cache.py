"""Unit tests for src.cache.TTLCache.

Uses a controllable clock so TTL behaviour is verified without sleeping.
"""
from __future__ import annotations

from src.cache import TTLCache


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_set_and_get_hit() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl=10, clock=clock)
    cache.set("a", 1)
    assert cache.get("a") == 1


def test_miss_returns_none() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=10)
    assert cache.get("missing") is None


def test_ttl_expiry() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl=5, clock=clock)
    cache.set("a", 42)
    assert cache.get("a") == 42

    clock.advance(4.9)
    assert cache.get("a") == 42

    clock.advance(0.2)  # total 5.1 — past TTL
    assert cache.get("a") is None


def test_per_call_ttl_overrides_default() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl=60, clock=clock)
    cache.set("a", 1, ttl=1)
    clock.advance(2)
    assert cache.get("a") is None


def test_pop_removes_entry() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=60)
    cache.set("a", 1)
    cache.pop("a")
    assert cache.get("a") is None


def test_clear_drops_all() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    assert len(cache) == 0


def test_maxsize_eviction() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl=60, maxsize=2, clock=clock)
    cache.set("a", 1)
    clock.advance(0.01)
    cache.set("b", 2)
    clock.advance(0.01)
    cache.set("c", 3)  # forces eviction of the entry closest to expiry ('a')
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_contains() -> None:
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl=5, clock=clock)
    cache.set("a", 1)
    assert "a" in cache
    clock.advance(10)
    assert "a" not in cache

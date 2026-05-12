"""Smoke tests for the access-token TTL cache used in provider.load_access_token."""
from __future__ import annotations

from src.cache import TTLCache


def test_token_cache_set_pop_clear() -> None:
    cache: TTLCache[str, str] = TTLCache(ttl=60)
    cache.set("hash-1", "at-1")
    cache.set("hash-2", "at-2")
    assert cache.get("hash-1") == "at-1"

    cache.pop("hash-1")
    assert cache.get("hash-1") is None
    assert cache.get("hash-2") == "at-2"

    cache.clear()
    assert cache.get("hash-2") is None


def test_token_cache_respects_per_call_ttl() -> None:
    from tests.test_cache import FakeClock

    clock = FakeClock()
    cache: TTLCache[str, str] = TTLCache(ttl=60, clock=clock)
    # Simulate "min(60s, remaining)" with a tiny remaining lifetime
    cache.set("h", "v", ttl=2)
    clock.advance(1.5)
    assert cache.get("h") == "v"
    clock.advance(1)
    assert cache.get("h") is None

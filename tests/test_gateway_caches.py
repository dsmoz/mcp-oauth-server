"""Tests for the gateway module-level caches and parallel fan-out.

We avoid pulling in the full FastAPI/MCP server graph and just exercise the
helper functions, monkeypatching the Supabase client and upstream fetcher.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.gateway import routes as gateway_routes


class FakeQuery:
    """Chainable stand-in for the Supabase query builder."""

    def __init__(self, data: list[dict] | None) -> None:
        self._data = data or []

    def select(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def eq(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def limit(self, *_args, **_kwargs) -> "FakeQuery":
        return self

    def execute(self) -> Any:
        class R:
            data = self._data

        return R()


class FakeDB:
    def __init__(self, table_data: dict[str, list[dict]]) -> None:
        self._table_data = table_data
        self.call_count = 0

    def table(self, name: str) -> FakeQuery:
        self.call_count += 1
        return FakeQuery(self._table_data.get(name, []))


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    gateway_routes._credit_cost_cache.clear()
    gateway_routes._published_mcps_cache.clear()
    gateway_routes._tool_cache.clear()


def test_credit_cost_cache_hits_db_once(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB({"mcp_catalogue": [{"credit_cost_per_call": 2.5}]})
    monkeypatch.setattr(gateway_routes, "get_db", lambda: db)

    assert gateway_routes._get_credit_cost("foo") == 2.5
    assert gateway_routes._get_credit_cost("foo") == 2.5
    assert gateway_routes._get_credit_cost("foo") == 2.5
    assert db.call_count == 1  # second + third call served from cache


def test_credit_cost_cache_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace the cache with a short-TTL one to verify expiry without sleeping
    short = gateway_routes.TTLCache(ttl=0.0001, maxsize=8)  # type: ignore[call-arg]
    monkeypatch.setattr(gateway_routes, "_credit_cost_cache", short)

    db = FakeDB({"mcp_catalogue": [{"credit_cost_per_call": 1}]})
    monkeypatch.setattr(gateway_routes, "get_db", lambda: db)

    assert gateway_routes._get_credit_cost("foo") == 1.0
    import time as _t
    _t.sleep(0.01)
    assert gateway_routes._get_credit_cost("foo") == 1.0
    assert db.call_count == 2  # cache expired between calls


def test_published_mcps_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB({"mcp_catalogue": [{"slug": "a"}, {"slug": "b"}]})
    monkeypatch.setattr(gateway_routes, "get_db", lambda: db)

    a = gateway_routes._get_all_published_mcps()
    b = gateway_routes._get_all_published_mcps()
    assert a == b == [{"slug": "a"}, {"slug": "b"}]
    assert db.call_count == 1


def test_invalidate_user_tool_cache_drops_only_that_user() -> None:
    gateway_routes._tool_cache.set(("slug1", "user-a"), [{"name": "t1"}])
    gateway_routes._tool_cache.set(("slug2", "user-a"), [{"name": "t2"}])
    gateway_routes._tool_cache.set(("slug1", "user-b"), [{"name": "t3"}])

    gateway_routes._invalidate_user_tool_cache("user-a")

    assert gateway_routes._tool_cache.get(("slug1", "user-a")) is None
    assert gateway_routes._tool_cache.get(("slug2", "user-a")) is None
    assert gateway_routes._tool_cache.get(("slug1", "user-b")) == [{"name": "t3"}]

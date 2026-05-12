"""Test that the parallel fan-out used by search_tools/_promoted_ui_tools
tolerates a single upstream failure without losing the rest.

We exercise the bare asyncio.gather pattern rather than spinning up the MCP
server — the production code uses the exact same `gather(..., return_exceptions=True)`
shape.
"""
from __future__ import annotations

import asyncio
import pytest


async def _ok(name: str) -> list[dict]:
    return [{"name": name, "description": f"{name} tool"}]


async def _boom(name: str) -> list[dict]:
    raise RuntimeError(f"upstream {name} unreachable")


@pytest.mark.asyncio
async def test_gather_return_exceptions_keeps_good_results() -> None:
    coros = [_ok("a"), _boom("b"), _ok("c")]
    results = await asyncio.gather(*coros, return_exceptions=True)

    assert results[0] == [{"name": "a", "description": "a tool"}]
    assert isinstance(results[1], RuntimeError)
    assert results[2] == [{"name": "c", "description": "c tool"}]

    # Mirror the production filter: keep only non-exception entries
    good = [r for r in results if not isinstance(r, BaseException)]
    assert len(good) == 2

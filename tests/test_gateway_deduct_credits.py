"""Unit tests for gateway-side credit deduction.

The bug: ``_deduct_credits`` previously returned ``-1`` for **any** exception
raised by the Supabase RPC, which the call sites then surfaced as
"Insufficient credits". A network blip, schema drift, or missing user row
all looked identical to a real insufficient-balance error and produced no
Sentry signal — masking real billing breakage and confusing users who had
plenty of credit.

These tests pin the three-way contract:
  * RPC returns a numeric balance → ("ok", balance)
  * RPC raises with "INSUFFICIENT_CREDITS"/"P0001" in the message → ("insufficient", None)
  * Any other RPC exception → ("error", None)
  * RPC returns ``data=None`` → ("error", None)
"""
from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("INTROSPECT_SECRET", "test-introspect-secret")

import pytest

from src.gateway import routes as gw


class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeRPC:
    def __init__(self, mode: str, payload: Any = None) -> None:
        self.mode = mode
        self.payload = payload

    def __call__(self, name: str, params: dict) -> "_FakeRPC":
        return self

    def execute(self) -> Any:
        if self.mode == "raise_insufficient":
            raise RuntimeError("RPC error: INSUFFICIENT_CREDITS (P0001)")
        if self.mode == "raise_other":
            raise RuntimeError("connection reset")
        return _Result(self.payload)


class _FakeDB:
    def __init__(self, rpc: _FakeRPC) -> None:
        self._rpc = rpc

    def rpc(self, name: str, params: dict):
        return self._rpc(name, params)


@pytest.mark.parametrize(
    "mode,payload,expected_status,expected_balance",
    [
        ("ok", 42.5, "ok", 42.5),
        ("ok", "42.5", "ok", 42.5),          # numeric returned as string
        ("raise_insufficient", None, "insufficient", None),
        ("raise_other", None, "error", None),
        ("ok", None, "error", None),         # null data ≠ zero balance — fail closed
    ],
)
def test_deduct_credits_status_tuple(monkeypatch, mode, payload, expected_status, expected_balance):
    rpc = _FakeRPC(mode, payload)
    monkeypatch.setattr(gw, "get_db", lambda: _FakeDB(rpc))

    status, balance = gw._deduct_credits("usr_test", 1.0)

    assert status == expected_status
    assert balance == expected_balance

"""Gateway admin-flag resolution and forwarding.

Upstream MCPs cannot introspect the caller — the gateway forwards the shared
upstream API key in Authorization, not the user's token. So admin-only writes
(e.g. mcp-writing-library's add_rubric_criterion) can only be gated if the
gateway resolves ``users.is_admin`` itself and forwards it as X-Is-Admin.

``_is_admin_user`` is that resolver. It mirrors the /introspect endpoint and
must fail closed (non-admin) on any lookup error.
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


class _FakeQuery:
    def __init__(self, data: Any) -> None:
        self._data = data

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return _Result(self._data)


class _FakeDB:
    def __init__(self, data: Any = None, raise_exc: bool = False) -> None:
        self._data = data
        self._raise = raise_exc

    def table(self, name: str):
        if self._raise:
            raise RuntimeError("supabase unreachable")
        return _FakeQuery(self._data)


@pytest.mark.parametrize(
    "data,expected",
    [
        ([{"is_admin": True}], True),
        ([{"is_admin": False}], False),
        ([{}], False),          # row without the column → fail closed
        ([], False),            # no such user
        (None, False),          # null data
    ],
)
def test_is_admin_user_reads_flag(monkeypatch, data, expected):
    monkeypatch.setattr(gw, "get_db", lambda: _FakeDB(data))
    assert gw._is_admin_user("usr_93f07c15894b4877") is expected


def test_is_admin_user_none_user_id_skips_db(monkeypatch):
    """A missing user_id must not hit the DB and must return non-admin."""
    def _boom():
        raise AssertionError("get_db must not be called for a null user_id")

    monkeypatch.setattr(gw, "get_db", _boom)
    assert gw._is_admin_user(None) is False
    assert gw._is_admin_user("") is False


def test_is_admin_user_fails_closed_on_db_error(monkeypatch):
    """A transient lookup failure resolves to non-admin, never admin."""
    monkeypatch.setattr(gw, "get_db", lambda: _FakeDB(raise_exc=True))
    assert gw._is_admin_user("usr_93f07c15894b4877") is False

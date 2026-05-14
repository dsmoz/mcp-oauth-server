"""Public (multi-user) OAuth client behaviour.

A public client is registered once and authorised by many distinct end users.
Each user must receive access tokens bound to *their* ``user_id`` — never the
client's claimer (which is always NULL for public clients).

These tests stub Supabase with an in-memory table store so the provider can be
exercised end-to-end without touching the network.
"""
from __future__ import annotations

import os
from typing import Any, Optional

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
os.environ.setdefault("INTROSPECT_SECRET", "test-introspect-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("OAUTH_ISSUER_URL", "http://localhost:8000")

import pytest

from src.crypto import hash_token, now_unix
from src.oauth import provider as provider_mod


# ── Tiny in-memory Supabase double ───────────────────────────────────────────

class _Query:
    def __init__(self, table: "_Table", op: str, payload: Any = None) -> None:
        self.table = table
        self.op = op  # "select" | "update" | "delete" | "insert"
        self.payload = payload
        self.filters: list[tuple[str, str, Any]] = []  # (col, kind, value)
        self._limit: Optional[int] = None

    def select(self, *_args, **_kwargs) -> "_Query":
        self.op = "select"
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self.filters.append((col, "eq", val))
        return self

    def is_(self, col: str, val: Any) -> "_Query":
        self.filters.append((col, "is", val))
        return self

    def gte(self, col: str, val: Any) -> "_Query":
        self.filters.append((col, "gte", val))
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def _matches(self, row: dict) -> bool:
        for col, kind, val in self.filters:
            if kind == "eq":
                if row.get(col) != val:
                    return False
            elif kind == "is":
                # supabase .is_("col", "null") → matches NULL
                if val == "null":
                    if row.get(col) is not None:
                        return False
                else:
                    if row.get(col) != val:
                        return False
            elif kind == "gte":
                if not (row.get(col) is not None and row[col] >= val):
                    return False
        return True

    def execute(self) -> Any:
        class _R:
            pass

        r = _R()
        rows = self.table.rows
        if self.op == "select":
            matched = [dict(x) for x in rows if self._matches(x)]
            if self._limit is not None:
                matched = matched[: self._limit]
            r.data = matched
            r.count = len(matched)
            return r
        if self.op == "insert":
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            for row in payload:
                rows.append(dict(row))
            r.data = [dict(x) for x in payload]
            return r
        if self.op == "update":
            updated: list[dict] = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(dict(row))
            r.data = updated
            return r
        if self.op == "delete":
            kept: list[dict] = []
            deleted: list[dict] = []
            for row in rows:
                if self._matches(row):
                    deleted.append(dict(row))
                else:
                    kept.append(row)
            self.table.rows = kept
            r.data = deleted
            return r
        r.data = []
        return r


class _Table:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def select(self, *args, **kwargs) -> _Query:
        return _Query(self, "select").select(*args, **kwargs)

    def insert(self, payload: Any) -> _Query:
        return _Query(self, "insert", payload)

    def update(self, payload: dict) -> _Query:
        return _Query(self, "update", payload)

    def delete(self) -> _Query:
        return _Query(self, "delete")


class FakeDB:
    def __init__(self) -> None:
        self._tables: dict[str, _Table] = {}

    def table(self, name: str) -> _Table:
        return self._tables.setdefault(name, _Table())


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch) -> FakeDB:
    fake = FakeDB()
    # Patch the provider module's get_db so all SupabaseOAuthProvider instances
    # share the same in-memory store for the duration of the test.
    monkeypatch.setattr(provider_mod, "get_db", lambda: fake)
    # Provider also calls hash_token at issuance — keep the real one.
    return fake


@pytest.fixture
def public_client(db) -> dict:
    row = {
        "client_id": "public-academia",
        "client_secret_hash": "irrelevant",
        "client_name": "dsmoz-academia",
        "redirect_uris": ["https://academia.example.com/cb"],
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "is_active": True,
        "is_public_client": True,
        "user_id": None,
    }
    db.table("oauth_clients").insert(row).execute()
    return row


@pytest.fixture
def private_client(db) -> dict:
    row = {
        "client_id": "private-cli",
        "client_secret_hash": "irrelevant",
        "client_name": "claude-desktop",
        "redirect_uris": ["http://localhost:9999/cb"],
        "grant_types": ["authorization_code"],
        "scope": "mcp",
        "is_active": True,
        "is_public_client": False,
        "user_id": "claimer-user",
    }
    db.table("oauth_clients").insert(row).execute()
    return row


def _seed_auth_code(db: FakeDB, *, code: str, client_id: str, user_id: Optional[str]) -> None:
    db.table("oauth_authorization_codes").insert(
        {
            "code": code,
            "client_id": client_id,
            "redirect_uri": None,
            "redirect_uri_provided_explicitly": False,
            "scopes": ["mcp"],
            "code_challenge": None,
            "code_challenge_method": None,
            "resource": None,
            "expires_at": now_unix() + 600,
            "user_id": user_id,
        }
    ).execute()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_claim_unclaimed_client_rejects_public(db, public_client):
    """Defense in depth — claim_unclaimed_client must refuse public clients."""
    provider = provider_mod.SupabaseOAuthProvider()
    with pytest.raises(ValueError, match="public client"):
        provider.claim_unclaimed_client("public-academia", "any-user")


def test_public_client_token_binds_to_consent_user_A(db, public_client):
    """User A consents → token bound to user A."""
    _seed_auth_code(db, code="codeA", client_id="public-academia", user_id="user-A")
    provider = provider_mod.SupabaseOAuthProvider()

    access, _refresh, _ttl = provider.exchange_authorization_code(
        code="codeA", client_id="public-academia", code_verifier=None
    )

    rows = db.table("oauth_access_tokens").select("*").eq("token", hash_token(access)).execute().data
    assert len(rows) == 1
    assert rows[0]["user_id"] == "user-A"
    assert rows[0]["client_id"] == "public-academia"


def test_public_client_two_users_get_distinct_user_bindings(db, public_client):
    """Same client_id, two consenting users → two tokens, two different user_ids."""
    _seed_auth_code(db, code="codeA", client_id="public-academia", user_id="user-A")
    _seed_auth_code(db, code="codeB", client_id="public-academia", user_id="user-B")
    provider = provider_mod.SupabaseOAuthProvider()

    a_access, _, _ = provider.exchange_authorization_code(
        code="codeA", client_id="public-academia", code_verifier=None
    )
    b_access, _, _ = provider.exchange_authorization_code(
        code="codeB", client_id="public-academia", code_verifier=None
    )

    at_a = provider.load_access_token(a_access)
    at_b = provider.load_access_token(b_access)
    assert at_a is not None and at_b is not None
    assert at_a.user_id == "user-A"
    assert at_b.user_id == "user-B"
    assert at_a.user_id != at_b.user_id


def test_public_client_missing_consent_user_raises(db, public_client):
    """Public client + auth code without user_id is a programming error."""
    _seed_auth_code(db, code="codeX", client_id="public-academia", user_id=None)
    provider = provider_mod.SupabaseOAuthProvider()
    with pytest.raises(ValueError, match="missing user binding"):
        provider.exchange_authorization_code(
            code="codeX", client_id="public-academia", code_verifier=None
        )


def test_private_client_still_uses_client_user_id(db, private_client):
    """Backwards compat — non-public clients keep reading user_id from the client row."""
    _seed_auth_code(db, code="codeP", client_id="private-cli", user_id=None)
    provider = provider_mod.SupabaseOAuthProvider()

    access, _, _ = provider.exchange_authorization_code(
        code="codeP", client_id="private-cli", code_verifier=None
    )
    at = provider.load_access_token(access)
    assert at is not None
    assert at.user_id == "claimer-user"


def test_private_client_prefers_consent_user_id_when_set(db, private_client):
    """Forward-compat — if a consent user_id is captured, use it even for private clients."""
    _seed_auth_code(db, code="codeQ", client_id="private-cli", user_id="explicit-consent-user")
    provider = provider_mod.SupabaseOAuthProvider()

    access, _, _ = provider.exchange_authorization_code(
        code="codeQ", client_id="private-cli", code_verifier=None
    )
    at = provider.load_access_token(access)
    assert at is not None
    assert at.user_id == "explicit-consent-user"

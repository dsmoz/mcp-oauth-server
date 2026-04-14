"""User (tenant) provider.

A user is the human account. Credits, MCP toolbox, portal credentials all live
here. OAuth clients are per-device and reference users via user_id.
"""
from __future__ import annotations

from typing import Optional

from src.crypto import generate_user_id, hash_secret, verify_secret
from src.db import get_db
from src.models import User


class SupabaseUserProvider:
    def __init__(self):
        self.db = get_db()

    def _single(self, table: str, **filters) -> dict | None:
        q = self.db.table(table).select("*")
        for k, v in filters.items():
            q = q.eq(k, v)
        result = q.limit(1).execute()
        return result.data[0] if result.data else None

    # ── Reads ────────────────────────────────────────────────────────────────

    def get_user(self, user_id: str) -> Optional[User]:
        row = self._single("users", user_id=user_id)
        return User(**row) if row else None

    def get_user_by_email(self, email: str) -> Optional[User]:
        row = self._single("users", email=email)
        return User(**row) if row else None

    # ── Writes ───────────────────────────────────────────────────────────────

    def create_user(
        self,
        *,
        email: str,
        display_name: Optional[str] = None,
        password: Optional[str] = None,
        credit_balance: float = 0.0,
        allowed_mcp_resources: Optional[list[str]] = None,
        is_active: bool = False,
    ) -> User:
        """Create a new user. Raises ValueError if email already exists."""
        existing = self.get_user_by_email(email)
        if existing is not None:
            raise ValueError(f"User with email {email!r} already exists")

        user_id = generate_user_id()
        row = {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "password_hash": hash_secret(password) if password else None,
            "credit_balance": credit_balance,
            "allowed_mcp_resources": allowed_mcp_resources or [],
            "is_active": is_active,
        }
        try:
            self.db.table("users").insert(row).execute()
        except Exception as exc:
            raise ValueError("User creation failed") from exc
        return User(**row)

    def set_password(self, user_id: str, password: str) -> None:
        self.db.table("users").update(
            {"password_hash": hash_secret(password), "is_active": True}
        ).eq("user_id", user_id).execute()

    def verify_password(self, user: User, password: str) -> bool:
        if not user.password_hash:
            return False
        return verify_secret(password, user.password_hash)

    def update_email(self, user_id: str, email: str) -> None:
        self.db.table("users").update({"email": email}).eq("user_id", user_id).execute()

    def update_display_name(self, user_id: str, display_name: str) -> None:
        self.db.table("users").update({"display_name": display_name}).eq(
            "user_id", user_id
        ).execute()

    def set_allowed_mcps(self, user_id: str, slugs: list[str]) -> None:
        self.db.table("users").update({"allowed_mcp_resources": slugs}).eq(
            "user_id", user_id
        ).execute()

    def set_credit_balance(self, user_id: str, balance: float) -> None:
        self.db.table("users").update({"credit_balance": balance}).eq(
            "user_id", user_id
        ).execute()

    def add_credits(self, user_id: str, amount: float) -> None:
        user = self.get_user(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")
        self.set_credit_balance(user_id, user.credit_balance + amount)

    def deduct_credits(self, user_id: str, amount: float) -> float:
        """Atomic deduction via deduct_credits_user RPC. Raises on insufficient credits."""
        try:
            resp = self.db.rpc(
                "deduct_credits_user",
                {"p_user_id": user_id, "p_amount": amount},
            ).execute()
            return float(resp.data) if resp.data is not None else 0.0
        except Exception as exc:
            raise ValueError(f"Credit deduction failed: {exc}") from exc

    def list_user_clients(self, user_id: str) -> list[dict]:
        """Return all oauth_clients rows owned by this user (devices)."""
        result = (
            self.db.table("oauth_clients")
            .select("client_id, client_name, created_at, claimed_at, is_active")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return result.data or []

    def delete_user(self, user_id: str) -> None:
        """Hard delete a user. FK cascade wipes clients + tokens."""
        self.db.table("users").delete().eq("user_id", user_id).execute()

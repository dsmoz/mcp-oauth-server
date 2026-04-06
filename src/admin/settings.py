"""Admin settings — read/write helpers for the admin_settings table."""
from __future__ import annotations

import datetime

from src.db import get_db


def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a single setting value by key."""
    db = get_db()
    result = db.table("admin_settings").select("value").eq("key", key).limit(1).execute()
    if result.data:
        return result.data[0]["value"]
    return default


def set_setting(key: str, value: str, updated_by: str = "admin") -> None:
    """Update a setting value. Does nothing if key doesn't exist."""
    db = get_db()
    db.table("admin_settings").update({
        "value": value,
        "updated_by": updated_by,
        "updated_at": datetime.datetime.utcnow().isoformat(),
    }).eq("key", key).execute()


def get_all_settings() -> list[dict]:
    """Return all settings ordered by category then label."""
    db = get_db()
    result = db.table("admin_settings").select("*").order("category").order("label").execute()
    return result.data or []


def get_settings_by_category(category: str) -> list[dict]:
    """Return settings for a specific category."""
    db = get_db()
    result = (
        db.table("admin_settings")
        .select("*")
        .eq("category", category)
        .order("label")
        .execute()
    )
    return result.data or []


def get_settings_grouped() -> dict[str, list[dict]]:
    """Return all settings grouped by category."""
    all_settings = get_all_settings()
    grouped: dict[str, list[dict]] = {}
    for s in all_settings:
        cat = s["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(s)
    return grouped


# Category display metadata
CATEGORY_META = {
    "llm": {"label": "LLM & AI Models", "icon": "ph-brain", "order": 1},
    "auth": {"label": "Authentication", "icon": "ph-shield-check", "order": 2},
    "notifications": {"label": "Notifications", "icon": "ph-bell", "order": 3},
    "search": {"label": "Search & Indexing", "icon": "ph-magnifying-glass", "order": 4},
}

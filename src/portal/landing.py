"""DB helpers for the public landing page."""
from __future__ import annotations

from src.db import get_db


def get_featured_servers() -> list[dict]:
    """Return featured, published MCP servers ordered by sort_order then name."""
    db = get_db()
    result = (
        db.table("mcp_catalogue")
        .select("slug, name, description, icon, category, tool_count, credit_cost_per_call, is_featured")
        .eq("is_featured", True)
        .eq("is_published", True)
        .order("name")
        .limit(6)
        .execute()
    )
    return result.data or []


def get_testimonials() -> list[dict]:
    """Return active testimonials ordered by sort_order."""
    db = get_db()
    result = (
        db.table("landing_testimonials")
        .select("*")
        .eq("is_active", True)
        .order("sort_order")
        .execute()
    )
    return result.data or []


def get_partners() -> list[dict]:
    """Return active partners ordered by sort_order."""
    db = get_db()
    result = (
        db.table("landing_partners")
        .select("*")
        .eq("is_active", True)
        .order("sort_order")
        .execute()
    )
    return result.data or []


def get_landing_stats() -> dict:
    """Return aggregate counts for the hero stat card."""
    db = get_db()
    servers_result = (
        db.table("mcp_catalogue")
        .select("tool_count")
        .eq("is_published", True)
        .execute()
    )
    rows = servers_result.data or []
    server_count = len(rows)
    tool_count = sum(r.get("tool_count") or 0 for r in rows)
    return {"server_count": server_count, "tool_count": tool_count}

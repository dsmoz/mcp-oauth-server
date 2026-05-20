"""Cost-based per-call billing.

Single source of truth for credit deduction. Replaces the legacy flat
``mcp_catalogue.credit_cost_per_call`` model.

Formula (see _ADMIN/Reference-Documents/2026-05-20_saas-mcp-pricing-model.md):

    raw_usd          = railway_compute_usd + railway_egress_usd + llm_usd
                       + base_overhead_usd + fixed_surcharge_usd
    raw_usd_grossed  = raw_usd * (1 + withholding_rate)
    sell_usd         = raw_usd_grossed * (1 + margin_rate) * (1 + iva_rate)
    credits_charged  = sell_usd / usd_per_credit

All inputs come from Supabase tables:
  - pricing_config (singleton — tax + margin + usd_per_credit)
  - compute_rates  (Railway USD rates per profile)
  - model_prices   (LLM USD per 1k tokens)
  - mcp_cost_profile (per-MCP overrides)

Tables are cached at module level with short TTLs so the hot path stays fast.
Admin edits propagate within ``_CONFIG_TTL`` seconds without a redeploy.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from src.db import get_db
from src.cache import TTLCache


_CONFIG_TTL = 60  # seconds — pricing_config, compute_rates, mcp_cost_profile
_MODEL_TTL = 300  # seconds — model_prices (changes rarely)

_pricing_cache: TTLCache[str, dict] = TTLCache(ttl=_CONFIG_TTL, maxsize=1)
_compute_cache: TTLCache[str, dict] = TTLCache(ttl=_CONFIG_TTL, maxsize=16)
_profile_cache: TTLCache[str, dict] = TTLCache(ttl=_CONFIG_TTL, maxsize=256)
_model_cache:   TTLCache[str, dict] = TTLCache(ttl=_MODEL_TTL,  maxsize=128)


# Fallback defaults — used only when DB rows are missing. Keep aligned with
# migration seed values so behavior is identical if cache miss + DB error.
_FALLBACK_PRICING = {
    "withholding_rate": 0.0,
    "margin_rate": 0.6,
    "iva_rate": 0.16,
    "usd_per_credit": 0.01,
    "min_balance_to_call": 1.0,
}
_FALLBACK_COMPUTE = {
    "usd_per_vcpu_sec": 0.0000077,
    "usd_per_gb_egress": 0.05,
    "credit_base_per_call_usd": 0.0001,
}


def _get_pricing_config() -> dict:
    cached = _pricing_cache.get("singleton")
    if cached is not None:
        return cached
    try:
        row = (
            get_db()
            .table("pricing_config")
            .select("withholding_rate,margin_rate,iva_rate,usd_per_credit,min_balance_to_call")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
        cfg = (row.data or [_FALLBACK_PRICING])[0]
        # Normalize to floats
        cfg = {k: float(cfg.get(k, _FALLBACK_PRICING[k])) for k in _FALLBACK_PRICING}
    except Exception as exc:
        print(f"BILLING: pricing_config fetch failed: {exc}", file=sys.stderr)
        cfg = dict(_FALLBACK_PRICING)
    _pricing_cache.set("singleton", cfg)
    return cfg


def _get_compute_rate(name: str) -> dict:
    cached = _compute_cache.get(name)
    if cached is not None:
        return cached
    try:
        row = (
            get_db()
            .table("compute_rates")
            .select("usd_per_vcpu_sec,usd_per_gb_egress,credit_base_per_call_usd")
            .eq("name", name)
            .limit(1)
            .execute()
        )
        rate = (row.data or [_FALLBACK_COMPUTE])[0]
        rate = {k: float(rate.get(k, _FALLBACK_COMPUTE[k])) for k in _FALLBACK_COMPUTE}
    except Exception as exc:
        print(f"BILLING: compute_rates fetch failed for {name!r}: {exc}", file=sys.stderr)
        rate = dict(_FALLBACK_COMPUTE)
    _compute_cache.set(name, rate)
    return rate


def _get_mcp_profile(mcp_slug: str) -> dict:
    cached = _profile_cache.get(mcp_slug)
    if cached is not None:
        return cached
    try:
        row = (
            get_db()
            .table("mcp_cost_profile")
            .select("compute_rate_name,llm_margin_override,fixed_surcharge_usd")
            .eq("mcp_slug", mcp_slug)
            .limit(1)
            .execute()
        )
        if row.data:
            r = row.data[0]
            profile = {
                "compute_rate_name": r.get("compute_rate_name") or "default",
                "llm_margin_override": (
                    float(r["llm_margin_override"]) if r.get("llm_margin_override") is not None else None
                ),
                "fixed_surcharge_usd": float(r.get("fixed_surcharge_usd") or 0),
            }
        else:
            profile = {
                "compute_rate_name": "default",
                "llm_margin_override": None,
                "fixed_surcharge_usd": 0.0,
            }
    except Exception as exc:
        print(f"BILLING: mcp_cost_profile fetch failed for {mcp_slug!r}: {exc}", file=sys.stderr)
        profile = {
            "compute_rate_name": "default",
            "llm_margin_override": None,
            "fixed_surcharge_usd": 0.0,
        }
    _profile_cache.set(mcp_slug, profile)
    return profile


def _get_model_price(model: str) -> Optional[dict]:
    cached = _model_cache.get(model)
    if cached is not None:
        return cached
    try:
        row = (
            get_db()
            .table("model_prices")
            .select("input_per_1k_usd,output_per_1k_usd,cached_input_per_1k_usd")
            .eq("model", model)
            .limit(1)
            .execute()
        )
        if not row.data:
            _model_cache.set(model, {})  # negative cache
            return None
        r = row.data[0]
        price = {
            "input_per_1k_usd": float(r["input_per_1k_usd"]),
            "output_per_1k_usd": float(r["output_per_1k_usd"]),
            "cached_input_per_1k_usd": (
                float(r["cached_input_per_1k_usd"])
                if r.get("cached_input_per_1k_usd") is not None
                else None
            ),
        }
    except Exception as exc:
        print(f"BILLING: model_prices fetch failed for {model!r}: {exc}", file=sys.stderr)
        return None
    _model_cache.set(model, price)
    return price


@dataclass
class UsageMeta:
    """Optional LLM usage attached by upstream MCPs via response ``_meta``.

    Upstream MCP MAY return either:
      - exact USD passthrough: ``_meta.usage_usd: float``
      - token counts: ``_meta.llm = {model, input_tokens, output_tokens, cached_input_tokens?}``

    Both feed the same converter. USD passthrough wins if both present.
    """
    usage_usd: Optional[float] = None
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


def parse_usage_meta(structured: dict | None) -> UsageMeta:
    """Extract ``_meta`` from an upstream JSON response. Tolerant — returns
    an empty UsageMeta when ``_meta`` is missing or malformed."""
    if not isinstance(structured, dict):
        return UsageMeta()
    meta = structured.get("_meta")
    if not isinstance(meta, dict):
        return UsageMeta()
    usage_usd = meta.get("usage_usd")
    try:
        usage_usd = float(usage_usd) if usage_usd is not None else None
    except (TypeError, ValueError):
        usage_usd = None
    llm = meta.get("llm")
    if isinstance(llm, dict):
        try:
            return UsageMeta(
                usage_usd=usage_usd,
                model=str(llm.get("model")) if llm.get("model") else None,
                input_tokens=int(llm.get("input_tokens") or 0),
                output_tokens=int(llm.get("output_tokens") or 0),
                cached_input_tokens=int(llm.get("cached_input_tokens") or 0),
            )
        except (TypeError, ValueError):
            pass
    return UsageMeta(usage_usd=usage_usd)


@dataclass
class CostBreakdown:
    compute_usd: float
    llm_usd: float
    raw_usd: float
    sell_usd: float
    credits_charged: float
    model_used: Optional[str]
    input_tokens: int
    output_tokens: int


def compute_cost(
    mcp_slug: str,
    duration_ms: int,
    response_bytes: int,
    usage: UsageMeta,
) -> CostBreakdown:
    """Apply the pricing formula. Pure function over cached config.

    Returns a CostBreakdown ready to be persisted to oauth_usage_logs.
    """
    pricing = _get_pricing_config()
    profile = _get_mcp_profile(mcp_slug)
    rate = _get_compute_rate(profile["compute_rate_name"])

    # Railway compute + egress
    compute_usd = (max(duration_ms, 0) / 1000.0) * rate["usd_per_vcpu_sec"]
    egress_usd = (max(response_bytes, 0) / (1024.0 ** 3)) * rate["usd_per_gb_egress"]

    # LLM cost
    llm_usd = 0.0
    if usage.usage_usd is not None:
        llm_usd = max(usage.usage_usd, 0.0)
    elif usage.model:
        price = _get_model_price(usage.model)
        if price:
            in_tok = max(usage.input_tokens - usage.cached_input_tokens, 0)
            cached_tok = min(usage.cached_input_tokens, usage.input_tokens)
            out_tok = max(usage.output_tokens, 0)
            llm_usd = (
                (in_tok / 1000.0) * price["input_per_1k_usd"]
                + (out_tok / 1000.0) * price["output_per_1k_usd"]
            )
            if cached_tok and price.get("cached_input_per_1k_usd") is not None:
                llm_usd += (cached_tok / 1000.0) * price["cached_input_per_1k_usd"]

    raw_usd = (
        compute_usd
        + egress_usd
        + llm_usd
        + rate["credit_base_per_call_usd"]
        + profile["fixed_surcharge_usd"]
    )

    grossed = raw_usd * (1 + pricing["withholding_rate"])
    sell_usd = grossed * (1 + pricing["margin_rate"]) * (1 + pricing["iva_rate"])
    credits_charged = sell_usd / pricing["usd_per_credit"] if pricing["usd_per_credit"] > 0 else 0.0

    return CostBreakdown(
        compute_usd=compute_usd + egress_usd,
        llm_usd=llm_usd,
        raw_usd=raw_usd,
        sell_usd=sell_usd,
        credits_charged=round(credits_charged, 4),
        model_used=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )


def get_min_balance_to_call() -> float:
    return _get_pricing_config()["min_balance_to_call"]


def invalidate_caches() -> None:
    """Called by admin write-paths after editing pricing tables."""
    _pricing_cache.clear()
    _compute_cache.clear()
    _profile_cache.clear()
    _model_cache.clear()

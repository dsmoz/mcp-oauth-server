"""Admin pages for the cost-based pricing model (v1, 2026-05-20).

Routes mounted at /admin/cost-model:
  - GET  /                — hub: pricing_config form + compute_rates
  - POST /                — save pricing_config
  - GET  /models          — list/edit model_prices
  - POST /models          — upsert one model_prices row
  - POST /models/delete   — delete one model
  - POST /models/refresh  — pull current OpenRouter pricing
  - GET  /profiles        — per-MCP cost overrides
  - POST /profiles        — upsert one mcp_cost_profile row
  - GET  /preview         — interactive cost calculator
  - POST /preview         — compute breakdown for given inputs
  - GET  /audit           — recent oauth_usage_logs with cost breakdown

All writes invalidate src.gateway.billing TTL caches so changes propagate
without redeploy.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.admin.routes import _require_admin
from src.db import get_db
from src.gateway import billing


_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(prefix="/admin/cost-model")


# ── Hub ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def hub(request: Request, saved: bool = False, _: str = Depends(_require_admin)):
    db = get_db()
    pricing = (
        db.table("pricing_config")
        .select("*").eq("id", 1).limit(1).execute()
    ).data
    pricing = pricing[0] if pricing else {}
    rates = (
        db.table("compute_rates").select("*").order("name").execute()
    ).data or []
    profile_count = (
        db.table("mcp_cost_profile").select("mcp_slug").execute()
    ).data or []
    model_count = (
        db.table("model_prices").select("model").execute()
    ).data or []
    return templates.TemplateResponse(
        request=request, name="cost_model_hub.html",
        context={
            "pricing": pricing,
            "rates": rates,
            "profile_count": len(profile_count),
            "model_count": len(model_count),
            "saved": saved,
        },
    )


@router.post("/", response_class=HTMLResponse)
async def save_pricing(
    request: Request,
    withholding_rate: float = Form(...),
    margin_rate: float = Form(...),
    iva_rate: float = Form(...),
    usd_per_credit: float = Form(...),
    fx_usd_mzn: float = Form(...),
    min_balance_to_call: float = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    db.table("pricing_config").update({
        "withholding_rate": withholding_rate,
        "margin_rate": margin_rate,
        "iva_rate": iva_rate,
        "usd_per_credit": usd_per_credit,
        "fx_usd_mzn": fx_usd_mzn,
        "min_balance_to_call": min_balance_to_call,
    }).eq("id", 1).execute()
    billing.invalidate_caches()
    return RedirectResponse(url="/admin/cost-model/?saved=true", status_code=303)


@router.post("/rates", response_class=HTMLResponse)
async def save_rate(
    request: Request,
    name: str = Form(...),
    usd_per_vcpu_sec: float = Form(...),
    usd_per_gb_egress: float = Form(...),
    credit_base_per_call_usd: float = Form(...),
    notes: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    db.table("compute_rates").upsert({
        "name": name.strip(),
        "usd_per_vcpu_sec": usd_per_vcpu_sec,
        "usd_per_gb_egress": usd_per_gb_egress,
        "credit_base_per_call_usd": credit_base_per_call_usd,
        "notes": notes or None,
    }, on_conflict="name").execute()
    billing.invalidate_caches()
    return RedirectResponse(url="/admin/cost-model/?saved=true", status_code=303)


# ── Model prices ─────────────────────────────────────────────────────────────

@router.get("/models", response_class=HTMLResponse)
async def models_list(request: Request, saved: bool = False, _: str = Depends(_require_admin)):
    db = get_db()
    rows = (
        db.table("model_prices")
        .select("*").order("provider").order("model").execute()
    ).data or []
    return templates.TemplateResponse(
        request=request, name="cost_model_models.html",
        context={"models": rows, "saved": saved},
    )


@router.post("/models", response_class=HTMLResponse)
async def models_upsert(
    request: Request,
    model: str = Form(...),
    provider: str = Form(...),
    input_per_1k_usd: float = Form(...),
    output_per_1k_usd: float = Form(...),
    cached_input_per_1k_usd: Optional[str] = Form(None),
    source_url: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    cached_val: Optional[float] = None
    if cached_input_per_1k_usd and str(cached_input_per_1k_usd).strip():
        try:
            cached_val = float(cached_input_per_1k_usd)
        except ValueError:
            cached_val = None
    db.table("model_prices").upsert({
        "model": model.strip(),
        "provider": provider.strip(),
        "input_per_1k_usd": input_per_1k_usd,
        "output_per_1k_usd": output_per_1k_usd,
        "cached_input_per_1k_usd": cached_val,
        "source_url": source_url or None,
    }, on_conflict="model").execute()
    billing.invalidate_caches()
    return RedirectResponse(url="/admin/cost-model/models?saved=true", status_code=303)


@router.post("/models/delete", response_class=HTMLResponse)
async def models_delete(
    request: Request,
    model: str = Form(...),
    _: str = Depends(_require_admin),
):
    db = get_db()
    db.table("model_prices").delete().eq("model", model).execute()
    billing.invalidate_caches()
    return RedirectResponse(url="/admin/cost-model/models?saved=true", status_code=303)


@router.post("/models/refresh", response_class=HTMLResponse)
async def models_refresh_openrouter(
    request: Request,
    _: str = Depends(_require_admin),
):
    """Pull current OpenRouter model list and upsert pricing for `openrouter/*`.

    Best-effort. OpenRouter pricing in `pricing.prompt` / `pricing.completion`
    is USD per token — convert to per-1k by × 1000.
    """
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=15)
        resp.raise_for_status()
        models = resp.json().get("data", [])
    except Exception as exc:
        print(f"ADMIN: openrouter refresh failed: {exc}", file=sys.stderr)
        return RedirectResponse(url="/admin/cost-model/models?saved=false", status_code=303)

    db = get_db()
    upserted = 0
    for m in models:
        model_id = m.get("id")
        pricing = m.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt") or 0) * 1000.0
            completion = float(pricing.get("completion") or 0) * 1000.0
        except (TypeError, ValueError):
            continue
        if not model_id or prompt <= 0:
            continue
        db.table("model_prices").upsert({
            "model": f"openrouter/{model_id}",
            "provider": "openrouter",
            "input_per_1k_usd": prompt,
            "output_per_1k_usd": completion,
            "cached_input_per_1k_usd": None,
            "source_url": "https://openrouter.ai/models",
        }, on_conflict="model").execute()
        upserted += 1
    billing.invalidate_caches()
    print(f"ADMIN: openrouter refresh upserted {upserted} models", file=sys.stderr)
    return RedirectResponse(url="/admin/cost-model/models?saved=true", status_code=303)


# ── Per-MCP profiles ─────────────────────────────────────────────────────────

@router.get("/profiles", response_class=HTMLResponse)
async def profiles_list(request: Request, saved: bool = False, _: str = Depends(_require_admin)):
    db = get_db()
    cat = (
        db.table("mcp_catalogue").select("slug,name,is_published").order("name").execute()
    ).data or []
    profs = (
        db.table("mcp_cost_profile").select("*").execute()
    ).data or []
    rates = (
        db.table("compute_rates").select("name").order("name").execute()
    ).data or []
    prof_by_slug = {p["mcp_slug"]: p for p in profs}
    rows = []
    for c in cat:
        slug = c["slug"]
        p = prof_by_slug.get(slug, {})
        rows.append({
            "slug": slug,
            "name": c["name"],
            "is_published": c.get("is_published"),
            "compute_rate_name": p.get("compute_rate_name") or "default",
            "llm_margin_override": p.get("llm_margin_override"),
            "fixed_surcharge_usd": float(p.get("fixed_surcharge_usd") or 0),
            "notes": p.get("notes") or "",
        })
    return templates.TemplateResponse(
        request=request, name="cost_model_profiles.html",
        context={"rows": rows, "rate_names": [r["name"] for r in rates], "saved": saved},
    )


@router.post("/profiles", response_class=HTMLResponse)
async def profiles_save(request: Request, _: str = Depends(_require_admin)):
    form = await request.form()
    db = get_db()
    updated = 0
    # form fields: rate_<slug>, surcharge_<slug>, margin_<slug>, notes_<slug>
    slugs: set[str] = set()
    for key in form.keys():
        if "_" in key and key.split("_", 1)[0] in {"rate", "surcharge", "margin", "notes"}:
            slugs.add(key.split("_", 1)[1])
    for slug in slugs:
        rate = form.get(f"rate_{slug}") or "default"
        try:
            surcharge = float(form.get(f"surcharge_{slug}") or 0)
        except (TypeError, ValueError):
            surcharge = 0.0
        margin_raw = form.get(f"margin_{slug}") or ""
        margin: Optional[float]
        try:
            margin = float(margin_raw) if str(margin_raw).strip() else None
        except (TypeError, ValueError):
            margin = None
        notes = (form.get(f"notes_{slug}") or "").strip() or None
        db.table("mcp_cost_profile").upsert({
            "mcp_slug": slug,
            "compute_rate_name": rate,
            "llm_margin_override": margin,
            "fixed_surcharge_usd": surcharge,
            "notes": notes,
        }, on_conflict="mcp_slug").execute()
        updated += 1
    billing.invalidate_caches()
    return RedirectResponse(url="/admin/cost-model/profiles?saved=true", status_code=303)


# ── Cost preview ─────────────────────────────────────────────────────────────

@router.get("/preview", response_class=HTMLResponse)
async def preview_form(request: Request, _: str = Depends(_require_admin)):
    db = get_db()
    slugs = [
        r["slug"] for r in (db.table("mcp_catalogue").select("slug").order("slug").execute().data or [])
    ]
    models = [
        r["model"] for r in (db.table("model_prices").select("model").order("model").execute().data or [])
    ]
    return templates.TemplateResponse(
        request=request, name="cost_model_preview.html",
        context={"slugs": slugs, "models": models, "result": None, "inputs": {}},
    )


@router.post("/preview", response_class=HTMLResponse)
async def preview_compute(
    request: Request,
    mcp_slug: str = Form(...),
    duration_ms: int = Form(...),
    response_bytes: int = Form(...),
    model: str = Form(""),
    input_tokens: int = Form(0),
    output_tokens: int = Form(0),
    cached_input_tokens: int = Form(0),
    usage_usd: str = Form(""),
    _: str = Depends(_require_admin),
):
    db = get_db()
    slugs = [
        r["slug"] for r in (db.table("mcp_catalogue").select("slug").order("slug").execute().data or [])
    ]
    models = [
        r["model"] for r in (db.table("model_prices").select("model").order("model").execute().data or [])
    ]
    usage_usd_f: Optional[float] = None
    if usage_usd.strip():
        try:
            usage_usd_f = float(usage_usd)
        except ValueError:
            usage_usd_f = None
    usage = billing.UsageMeta(
        usage_usd=usage_usd_f,
        model=model.strip() or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
    )
    cost = billing.compute_cost(mcp_slug, duration_ms, response_bytes, usage)
    return templates.TemplateResponse(
        request=request, name="cost_model_preview.html",
        context={
            "slugs": slugs, "models": models,
            "inputs": {
                "mcp_slug": mcp_slug,
                "duration_ms": duration_ms,
                "response_bytes": response_bytes,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "usage_usd": usage_usd,
            },
            "result": cost,
        },
    )


# ── Audit ────────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, limit: int = 200, _: str = Depends(_require_admin)):
    db = get_db()
    limit = max(10, min(int(limit or 200), 1000))
    rows = (
        db.table("oauth_usage_logs")
        .select("created_at,user_id,client_id,endpoint,duration_ms,response_bytes,"
                "compute_usd,llm_usd,raw_usd,sell_usd,credits_charged,model_used,"
                "input_tokens,output_tokens,credits_used")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    ).data or []
    return templates.TemplateResponse(
        request=request, name="cost_model_audit.html",
        context={"rows": rows, "limit": limit},
    )

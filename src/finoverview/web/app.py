"""Dashboard.

Deliberate constraints, all of which follow from "minimal, on a Pi, over LAN":
  - Zero external requests. No CDN, no webfonts, no analytics. A page showing
    every balance you own should not phone anywhere.
  - Charts are server-rendered SVG. No charting library, no build step, and the
    page works with JavaScript disabled.
  - The only JS is an auto-refresh timer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import db
from ..collectors import get_collector
from ..config import load_assets, load_settings
from ..metrics import allocation, health, networth, projection, returns, saxo_projection
from ..metrics.saxo_projection.categories import load_categories
from .charts import area_chart, bar_rows, band_chart, pie_chart
from .settings_store import (
    parse_assets_form,
    parse_categories_form,
    save_assets,
    save_categories,
)

HERE = Path(__file__).parent

app = FastAPI(title="finance-overview", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

settings = load_settings()


def money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}{(' ' + currency) if currency else ''}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def signed(value: float | None, places: int = 0) -> str:
    """Explicit sign, thousands separators. printf-style '%+,.0f' is not valid
    Python format syntax, so this exists rather than inlining it in a template."""
    if value is None:
        return "—"
    return f"{value:+,.{places}f}"


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.filters["signed"] = signed
templates.env.globals["version"] = __import__("finoverview").__version__


def get_conn() -> sqlite3.Connection:
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    return conn


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, window: int = Query(365, ge=7, le=3650)):
    conn = get_conn()
    try:
        base = settings.base_currency
        summary = networth.summary(conn, base)
        series = networth.history(conn, base, days=max(window, 90))
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)

        ret = returns.compute(conn, base, days=window)
        breakdown = allocation.breakdown(conn, base)
        class_rows = allocation.net_worth_by_class(conn, base)
        conc = allocation.concentration(conn, base)
        rec = allocation.recurring_summary(conn, base)
        dd = returns.max_drawdown(series)

        changes = {
            label: networth.change(series, days)
            for label, days in (("30d", 30), ("90d", 90), ("1y", 365))
        }

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "base": base,
                "summary": summary,
                "series": series,
                "chart": area_chart(series, width=1040, height=200),
                "changes": changes,
                "returns": ret,
                "breakdown": breakdown,
                "bars": {k: bar_rows(v) for k, v in breakdown.items()
                         if k.startswith("by_") and k != "by_asset_class"},
                "class_rows": class_rows,
                "class_pie": pie_chart(class_rows),
                "concentration": conc,
                "recurring": rec,
                "drawdown": dd,
                "collectors": collector_rows,
                "consents": consent_rows,
                "overall": health.overall(collector_rows, consent_rows),
                "window": window,
            },
        )
    finally:
        conn.close()


@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request):
    conn = get_conn()
    try:
        base = settings.base_currency
        rows = allocation.positions(conn, base)
        total = sum(r["value"] for r in rows)
        return templates.TemplateResponse(
            request, "positions.html",
            {"base": base, "positions": rows, "total": total,
             "collectors": health.collectors(conn, settings.stale_after_hours),
             "consents": health.consents(conn),
             "overall": health.overall(
                 health.collectors(conn, settings.stale_after_hours),
                 health.consents(conn))},
        )
    finally:
        conn.close()


@app.get("/projection", response_class=HTMLResponse)
def projection_page(request: Request):
    conn = get_conn()
    try:
        base = settings.base_currency
        assets = load_assets(settings.config_dir)
        out = projection.run(conn, base, assets.projection)
        bands = out["monte_carlo"]["bands"]
        det = out["deterministic"]
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)

        sp = saxo_projection.run(conn, base, assets, out["assumptions"].years)
        sp_total_start = sp["total"][0] or 1
        sp_bars = bar_rows([
            {"label": c.label, "value": sp["start_values"][c.key],
             "pct": sp["start_values"][c.key] / sp_total_start * 100}
            for c in sp["categories"] if sp["start_values"][c.key]
        ])
        sp_rows = [
            {"label": c.label, "rate": c.expected_real_return_pct,
             "start": sp["start_values"][c.key], "end": sp["series"][c.key][-1]}
            for c in sp["categories"] if sp["start_values"][c.key]
        ]

        return templates.TemplateResponse(
            request, "projection.html",
            {"base": base,
             "assumptions": out["assumptions"].as_display(),
             "bands": bands,
             "terminal": out["monte_carlo"]["terminal"],
             "monthly_contribution": out["monte_carlo"]["monthly_contribution"],
             "start": out["monte_carlo"]["start"],
             "chart": band_chart(bands, det, width=1040, height=260),
             "note": out["monte_carlo"]["note"],
             "saxo_bars": sp_bars,
             "saxo_rows": sp_rows,
             "saxo_years": out["assumptions"].years,
             "saxo_total_start": sp["total"][0],
             "saxo_total_end": sp["total"][-1],
             "collectors": collector_rows,
             "consents": consent_rows,
             "overall": health.overall(collector_rows, consent_rows)},
        )
    finally:
        conn.close()


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, error: str | None = None):
    conn = get_conn()
    try:
        assets = load_assets(settings.config_dir)
        fallback_pct = float(assets.projection.get("expected_real_return_pct", 5.0))
        categories = load_categories(assets.saxo_projection_categories, fallback_pct)
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)
        return templates.TemplateResponse(
            request, "settings.html",
            {"categories": categories,
             "manual_assets": assets.assets,
             "error": error,
             "collectors": collector_rows,
             "consents": consent_rows,
             "overall": health.overall(collector_rows, consent_rows)},
        )
    finally:
        conn.close()


@app.post("/settings/saxo-projection")
async def save_saxo_projection_settings(request: Request):
    form = await request.form()
    save_categories(settings.config_dir, parse_categories_form(form))
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/assets")
async def save_assets_settings(request: Request):
    conn = get_conn()
    try:
        form = await request.form()
        save_assets(settings.config_dir, parse_assets_form(form))
        get_collector("manual")(conn, settings, load_assets(settings.config_dir)).run()
    except Exception as exc:  # noqa: BLE001 - surface it to the settings page, not a 500
        msg = quote(f"{type(exc).__name__}: {exc}")
        return RedirectResponse(f"/settings?error={msg}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/health")
def health_endpoint():
    """Machine-readable. Point an uptime check at this: it returns 503 when a
    source is stale, so a broken collector pages you instead of quietly rotting."""
    conn = get_conn()
    try:
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)
        overall = health.overall(collector_rows, consent_rows)
        return JSONResponse(
            {"status": overall, "collectors": collector_rows, "consents": consent_rows},
            status_code=200 if overall in ("ok", "warn") else 503,
        )
    finally:
        conn.close()


@app.get("/api/summary")
def api_summary():
    conn = get_conn()
    try:
        base = settings.base_currency
        s = networth.summary(conn, base)
        return {
            "net_worth": s["net_worth"],
            "available": s["available"],
            "encumbered": s["encumbered"],
            "invested": s["invested"],
            "cash": s["cash"],
            "currency": base,
            "accounts": [
                {"label": a.label, "institution": a.institution, "kind": a.kind,
                 "value": a.amount_base, "currency": base, "encumbered": a.encumbered,
                 "as_of": a.ts}
                for a in s["accounts"]
            ],
        }
    finally:
        conn.close()


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Finance overview",
        "short_name": "Finance",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": "#0f1115",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }, media_type="application/manifest+json")

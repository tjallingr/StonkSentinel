"""FastAPI dashboard."""

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
from ..metrics import allocation, health, networth, projection, returns
from ..metrics import assets as asset_metrics
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
        assets_cfg = load_assets(settings.config_dir)
        summary = networth.summary(conn, base)
        series = networth.history(conn, base, days=max(window, 90))
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)

        ret = returns.compute(conn, base, days=window)
        breakdown = allocation.breakdown(conn, base)
        class_rows = asset_metrics.allocation.by_category(
            asset_metrics.gather(conn, base, assets_cfg)
        )
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
                "bars": {k: bar_rows(v) for k, v in breakdown.items() if k.startswith("by_")},
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
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)
        return templates.TemplateResponse(
            request, "positions.html",
            {"base": base, "positions": rows, "total": total,
             "collectors": collector_rows,
             "consents": consent_rows,
             "overall": health.overall(collector_rows, consent_rows)},
        )
    finally:
        conn.close()


@app.get("/projection", response_class=HTMLResponse)
def projection_page(request: Request):
    conn = get_conn()
    try:
        base = settings.base_currency
        assets_cfg = load_assets(settings.config_dir)
        out = projection.run(conn, base, assets_cfg)
        bands = out["monte_carlo"]["bands"]
        det = out["deterministic"]
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)

        total = asset_metrics.combine(out["deterministic_by_asset"])
        total_start = total[0] or 1
        asset_bars = bar_rows([
            {"label": a.label, "value": a.value_base, "pct": a.value_base / total_start * 100}
            for a in out["assets"] if a.value_base
        ])
        asset_rows = [
            {"label": a.label, "category": a.category, "rate": a.yield_pct,
             "contribution": a.monthly_contribution,
             "start": a.value_base, "end": out["deterministic_by_asset"][a.key][-1]}
            for a in out["assets"]
        ]

        return templates.TemplateResponse(
            request, "projection.html",
            {"base": base,
             "assumptions": out["assumptions"].as_display(),
             "weighted_yield_pct": out["weighted_yield_pct"],
             "total_monthly_contribution": out["total_monthly_contribution"],
             "bands": bands,
             "terminal": out["monte_carlo"]["terminal"],
             "monthly_contribution": out["monte_carlo"]["monthly_contribution"],
             "start": out["monte_carlo"]["start"],
             "chart": band_chart(bands, det, width=1040, height=260),
             "note": out["monte_carlo"]["note"],
             "asset_bars": asset_bars,
             "asset_rows": asset_rows,
             "asset_years": out["assumptions"].years,
             "asset_total_start": total[0],
             "asset_total_end": total[-1],
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
        base = settings.base_currency
        assets_cfg = load_assets(settings.config_dir)
        fallback_pct = float(assets_cfg.projection.get("expected_real_return_pct", 5.0))
        categories = asset_metrics.load_categories(assets_cfg.saxo_projection_categories, fallback_pct)
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)
        return templates.TemplateResponse(
            request, "settings.html",
            {"base": base,
             "categories": categories,
             "manual_assets": assets_cfg.assets,
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

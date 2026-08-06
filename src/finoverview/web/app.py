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
from ..metrics import allocation, expenses, health, networth, projection, returns
from ..metrics import assets as asset_metrics
from .charts import area_chart, bar_rows, band_chart, column_chart, pie_chart
from .settings_store import (
    parse_assets_form,
    parse_categories_form,
    parse_projection_form,
    save_assets,
    save_categories,
    save_projection,
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


@app.on_event("startup")
def _prepare_schema() -> None:
    """Bring the schema up to date once, at startup, on a single thread.

    Not per request: init_db recreates the views, and requests are served from a
    threadpool, so a rebuild racing a live query makes pages fail intermittently
    with "no such table". Startup is the only moment nothing else is reading.
    """
    conn = db.connect(settings.db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()


def get_conn() -> sqlite3.Connection:
    return db.connect(settings.db_path)


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


@app.get("/expenses", response_class=HTMLResponse)
def expenses_page(request: Request, months: int = Query(12, ge=1, le=60),
                  payee: str | None = None):
    conn = get_conn()
    try:
        base = settings.base_currency
        summary = expenses.summary(conn, base, months=months)
        payees = expenses.top_payees(conn, base, months=months)
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)

        payee_label = next(
            (p["label"] for p in payees if p["payee_key"] == payee), payee
        )
        return templates.TemplateResponse(
            request, "expenses.html",
            {"base": base,
             "months": months,
             "summary": summary,
             "payees": payees,
             "payee": payee,
             "payee_label": payee_label,
             "txs": expenses.transactions(conn, base, payee_key=payee, months=months,
                                          limit=200 if payee else 60),
             "chart": column_chart(summary["series"], width=1040, height=190,
                                   average=summary["average"], currency=base),
             "collectors": collector_rows,
             "consents": consent_rows,
             "overall": health.overall(collector_rows, consent_rows)},
        )
    finally:
        conn.close()


@app.get("/projection", response_class=HTMLResponse)
def projection_page(request: Request, error: str | None = None):
    conn = get_conn()
    try:
        base = settings.base_currency
        assets_cfg = load_assets(settings.config_dir)
        out = projection.run(conn, base, assets_cfg)
        bands = out["monte_carlo"]["bands"]
        det = out["deterministic"]
        a = out["assumptions"]
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)

        return templates.TemplateResponse(
            request, "projection.html",
            {"base": base,
             "bands": bands,
             "terminal": out["monte_carlo"]["terminal"],
             "monthly_contribution": out["monte_carlo"]["monthly_contribution"],
             "start": out["monte_carlo"]["start"],
             "chart": band_chart(bands, det, width=1040, height=260),
             "asset_rows": out["asset_rows"],
             "totals": out["totals"],
             # Simulation parameters, for the caption under the chart.
             "years": a.years,
             "paths": a.paths,
             "volatility_pct": a.volatility_pct,
             "inflation_pct": a.inflation_pct,
             "contribution_growth_pct": a.contribution_growth_pct,
             "collectors": collector_rows,
             "consents": consent_rows,
             "overall": health.overall(collector_rows, consent_rows),
             "error": error},
        )
    finally:
        conn.close()


@app.post("/projection/horizon")
async def save_projection_horizon(request: Request):
    try:
        form = await request.form()
        save_projection(settings.config_dir, parse_projection_form(form))
    except Exception as exc:  # noqa: BLE001 - surface it to the projection page, not a 500
        return RedirectResponse(
            f"/projection?error={quote(f'{type(exc).__name__}: {exc}')}", status_code=303
        )
    return RedirectResponse("/projection", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, error: str | None = None):
    conn = get_conn()
    try:
        base = settings.base_currency
        assets_cfg = load_assets(settings.config_dir)
        fallback_pct = float(assets_cfg.projection.get("expected_real_return_pct", 5.0))
        _proj = projection.Assumptions.from_config(assets_cfg.projection)
        categories = asset_metrics.load_categories(assets_cfg.saxo_projection_categories, fallback_pct)
        collector_rows = health.collectors(conn, settings.stale_after_hours)
        consent_rows = health.consents(conn)
        return templates.TemplateResponse(
            request, "settings.html",
            {"base": base,
             "categories": categories,
             "manual_assets": assets_cfg.assets,
             "projection_years": _proj.years,
             "projection_inflation": _proj.inflation_pct,
             "error": error,
             "collectors": collector_rows,
             "consents": consent_rows,
             "overall": health.overall(collector_rows, consent_rows)},
        )
    finally:
        conn.close()


@app.post("/settings/projection")
async def save_projection_settings(request: Request):
    try:
        form = await request.form()
        save_projection(settings.config_dir, parse_projection_form(form))
    except Exception as exc:  # noqa: BLE001 - surface it to the settings page, not a 500
        return RedirectResponse(
            f"/settings?error={quote(f'{type(exc).__name__}: {exc}')}", status_code=303
        )
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/saxo-projection")
async def save_saxo_projection_settings(request: Request):
    try:
        form = await request.form()
        save_categories(settings.config_dir, parse_categories_form(form))
    except Exception as exc:  # noqa: BLE001 - surface it to the settings page, not a 500
        return RedirectResponse(
            f"/settings?error={quote(f'{type(exc).__name__}: {exc}')}", status_code=303
        )
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
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }, media_type="application/manifest+json")

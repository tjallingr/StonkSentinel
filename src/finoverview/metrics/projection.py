"""Projections from config/assets.toml — deterministic + Monte Carlo bands."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from . import assets as asset_metrics
from ..config import AssetsConfig


@dataclass
class Assumptions:
    years: int
    fallback_expected_real_return_pct: float
    volatility_pct: float
    monthly_contribution: float | None
    contribution_growth_pct: float
    inflation_pct: float
    paths: int
    seed: int | None

    @classmethod
    def from_config(cls, cfg: dict) -> "Assumptions":
        return cls(
            years=int(cfg.get("years", 20)),
            fallback_expected_real_return_pct=float(cfg.get("expected_real_return_pct", 5.0)),
            volatility_pct=float(cfg.get("volatility_pct", 15.0)),
            monthly_contribution=(
                float(cfg["monthly_contribution"]) if "monthly_contribution" in cfg else None
            ),
            contribution_growth_pct=float(cfg.get("contribution_growth_pct", 0.0)),
            # Only used to restate real figures in nominal terms. It cannot be
            # derived from fx_rates: those are ECB currency rates, which convert
            # between currencies, not between purchasing power in different years.
            # A 25-year projection needs a forward assumption regardless, so this
            # is a stated number rather than a measurement. 2% is the ECB target.
            inflation_pct=float(cfg.get("inflation_pct", 2.0)),
            paths=int(cfg.get("paths", 10000)),
            seed=int(cfg["seed"]) if "seed" in cfg else None,
        )


def monte_carlo(capital: float, monthly_contribution: float,
                expected_real_return_pct: float, assumptions: Assumptions) -> dict:
    """Lognormal annual returns. 10k paths x 30 years runs in well under a second
    on a Pi 4, so there's no reason to cheap out on path count."""
    rng = np.random.default_rng(assumptions.seed)
    n, years = assumptions.paths, assumptions.years

    mu = expected_real_return_pct / 100
    sigma = assumptions.volatility_pct / 100
    # Convert arithmetic mean + vol into lognormal parameters so the *mean*
    # outcome matches the stated expected return rather than the median.
    sigma_log = np.sqrt(np.log(1 + (sigma**2) / ((1 + mu) ** 2)))
    mu_log = np.log(1 + mu) - 0.5 * sigma_log**2

    values = np.full(n, capital, dtype=np.float64)
    bands: list[dict] = [{
        "year": 0, "p10": capital, "p25": capital, "p50": capital,
        "p75": capital, "p90": capital, "mean": capital,
    }]

    for year in range(1, years + 1):
        annual_contrib = monthly_contribution * 12 * (
            (1 + assumptions.contribution_growth_pct / 100) ** (year - 1)
        )
        growth = np.exp(rng.normal(mu_log, sigma_log, n))
        # Contributions spread through the year: approximate with half-year growth.
        values = values * growth + annual_contrib * np.sqrt(growth)
        values = np.maximum(values, 0.0)
        p10, p25, p50, p75, p90 = np.percentile(values, [10, 25, 50, 75, 90])
        bands.append({
            "year": year, "p10": float(p10), "p25": float(p25), "p50": float(p50),
            "p75": float(p75), "p90": float(p90), "mean": float(values.mean()),
        })

    return {
        "bands": bands,
        "start": capital,
        "monthly_contribution": monthly_contribution,
        "terminal": {
            "p10": bands[-1]["p10"], "p50": bands[-1]["p50"], "p90": bands[-1]["p90"],
            "mean": bands[-1]["mean"],
        },
    }


def breakdown(assets: list, det_by_asset: dict, a: Assumptions) -> tuple[list[dict], dict]:
    """Per-asset rows splitting the projected value into money paid in vs growth,
    in both real and nominal terms, plus the totals row.

    `invested` is the flat line: today's value plus every future contribution with
    no return applied. `growth` is everything the projection puts above it, which
    is the number the old global assumptions table obscured — each asset compounds
    at its own rate, so a single "expected return" said nothing useful.

    Real figures are in today's purchasing power because every asset's yield is a
    real (after-inflation) rate. Nominal restates them at the assumed inflation,
    which is valid for the value line because contributions are themselves real
    amounts here, so the whole cash-flow stream scales by the same factor.
    """
    inflator = (1 + a.inflation_pct / 100) ** a.years
    rows = []
    for asset in assets:
        series = det_by_asset.get(asset.key) or [asset.value_base]
        paid_in = asset_metrics.contributed(
            asset.monthly_contribution, a.years, a.contribution_growth_pct
        )
        invested = asset.value_base + paid_in
        real = series[-1]
        # An empty account contributes a row of zeros to every column. Nothing to
        # compare, so it only makes the real rows harder to scan.
        if not invested and not real:
            continue
        rows.append({
            "key": asset.key,
            "label": asset.label,
            "category": asset.category,
            "rate": asset.yield_pct,
            "monthly": asset.monthly_contribution,
            "today": asset.value_base,
            "contributed": paid_in,
            "invested": invested,
            "growth": real - invested,
            "real": real,
            "nominal": real * inflator,
        })
    rows.sort(key=lambda r: -r["real"])

    real_total = sum(r["real"] for r in rows)
    invested_total = sum(r["invested"] for r in rows)
    totals = {
        "rate": asset_metrics.weighted_average_yield_pct(assets),
        "monthly": asset_metrics.total_monthly_contribution(assets),
        "today": sum(r["today"] for r in rows),
        "contributed": sum(r["contributed"] for r in rows),
        "invested": invested_total,
        "growth": real_total - invested_total,
        "real": real_total,
        "nominal": real_total * inflator,
    }
    return rows, totals


def run(conn: sqlite3.Connection, base: str, assets_cfg: AssetsConfig) -> dict:
    assumptions = Assumptions.from_config(assets_cfg.projection)
    gathered = asset_metrics.gather(conn, base, assets_cfg)

    det_by_asset = asset_metrics.deterministic_by_asset(
        gathered, assumptions.years, assumptions.contribution_growth_pct
    )
    # combine() folds an empty dict to [], so keep a zero series for the no-asset
    # case rather than letting every [-1] downstream raise IndexError.
    det_total = asset_metrics.combine(det_by_asset) or [0.0] * (assumptions.years + 1)
    capital = sum(a.value_base for a in gathered)
    avg_yield = asset_metrics.weighted_average_yield_pct(gathered)
    contribution = asset_metrics.total_monthly_contribution(gathered)
    rows, totals = breakdown(gathered, det_by_asset, assumptions)

    return {
        "assumptions": assumptions,
        "assets": gathered,
        "deterministic_by_asset": det_by_asset,
        "deterministic": [{"year": i, "value": v} for i, v in enumerate(det_total)],
        "monte_carlo": monte_carlo(capital, contribution, avg_yield, assumptions),
        "weighted_yield_pct": avg_yield,
        "total_monthly_contribution": contribution,
        "asset_rows": rows,
        "totals": totals,
    }

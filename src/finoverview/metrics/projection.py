"""Projections.

Every assumption comes from [projection] in config/assets.toml. None are
hardcoded, deliberately: a projection is an assumption engine with arithmetic
attached, and if the assumptions aren't visible and version-controlled the output
is decoration. The dashboard renders the assumptions next to the chart for the
same reason.

Two models:
  deterministic  - each asset compounds at its own yield_pct and contribution
                   (metrics.assets), summed. No volatility, so it never shows
                   sequence risk.
  monte_carlo    - lognormal annual returns around the value-weighted average
                   yield across all assets, N paths, percentile bands. Shows the
                   spread, still assumes iid returns and one blended volatility
                   across every asset (no per-category volatility modelling).
"""

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
    inflation_pct: float
    monthly_contribution: float | None
    contribution_growth_pct: float
    paths: int
    seed: int | None

    @classmethod
    def from_config(cls, cfg: dict) -> "Assumptions":
        return cls(
            years=int(cfg.get("years", 20)),
            fallback_expected_real_return_pct=float(cfg.get("expected_real_return_pct", 5.0)),
            volatility_pct=float(cfg.get("volatility_pct", 15.0)),
            inflation_pct=float(cfg.get("inflation_pct", 2.0)),
            monthly_contribution=(
                float(cfg["monthly_contribution"]) if "monthly_contribution" in cfg else None
            ),
            contribution_growth_pct=float(cfg.get("contribution_growth_pct", 0.0)),
            paths=int(cfg.get("paths", 10000)),
            seed=int(cfg["seed"]) if "seed" in cfg else None,
        )

    def as_display(self) -> list[tuple[str, str]]:
        return [
            ("Horizon", f"{self.years} years"),
            ("Fallback / unallocated return", f"{self.fallback_expected_real_return_pct:.1f}% / yr"),
            ("Volatility", f"{self.volatility_pct:.1f}%"),
            ("Inflation", f"{self.inflation_pct:.1f}%"),
            ("Monthly contribution",
             f"{self.monthly_contribution:,.0f}" if self.monthly_contribution is not None
             else "from recurring net"),
            ("Contribution growth", f"{self.contribution_growth_pct:.1f}% / yr"),
            ("Simulated paths", f"{self.paths:,}"),
        ]


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
        "note": "Real terms: figures are in today's purchasing power, so no "
                "inflation adjustment is applied on top.",
    }


def run(conn: sqlite3.Connection, base: str, assets_cfg: AssetsConfig) -> dict:
    assumptions = Assumptions.from_config(assets_cfg.projection)
    gathered = asset_metrics.gather(conn, base, assets_cfg)

    det_by_asset = asset_metrics.deterministic_by_asset(
        gathered, assumptions.years, assumptions.contribution_growth_pct
    )
    det_total = asset_metrics.combine(det_by_asset)
    capital = sum(a.value_base for a in gathered)
    avg_yield = asset_metrics.weighted_average_yield_pct(gathered)
    contribution = asset_metrics.total_monthly_contribution(gathered)

    return {
        "assumptions": assumptions,
        "assets": gathered,
        "deterministic_by_asset": det_by_asset,
        "deterministic": [{"year": i, "value": v} for i, v in enumerate(det_total)],
        "monte_carlo": monte_carlo(capital, contribution, avg_yield, assumptions),
        "weighted_yield_pct": avg_yield,
        "total_monthly_contribution": contribution,
    }

"""Projections.

Every assumption comes from [projection] in config/assets.toml. None are
hardcoded, deliberately: a projection is an assumption engine with arithmetic
attached, and if the assumptions aren't visible and version-controlled the output
is decoration. The dashboard renders the assumptions next to the chart for the
same reason.

Two models:
  deterministic  - constant real return, no volatility. Easy to reason about,
                   and wrong in a specific known way: it never shows sequence risk.
  monte_carlo    - lognormal annual returns, N paths, percentile bands. Shows the
                   spread. Still assumes iid returns, which understates the odds
                   of prolonged drawdowns in real markets.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from . import allocation, networth


@dataclass
class Assumptions:
    years: int
    expected_real_return_pct: float
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
            expected_real_return_pct=float(cfg.get("expected_real_return_pct", 5.0)),
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
            ("Expected real return", f"{self.expected_real_return_pct:.1f}% / yr"),
            ("Volatility", f"{self.volatility_pct:.1f}%"),
            ("Inflation", f"{self.inflation_pct:.1f}%"),
            ("Monthly contribution",
             f"{self.monthly_contribution:,.0f}" if self.monthly_contribution is not None
             else "from recurring net"),
            ("Contribution growth", f"{self.contribution_growth_pct:.1f}% / yr"),
            ("Simulated paths", f"{self.paths:,}"),
        ]


def _starting_capital(conn: sqlite3.Connection, base: str) -> float:
    """Invested capital plus freely available cash. Encumbered balances are
    excluded: they're real net worth but not available to compound."""
    s = networth.summary(conn, base)
    return s["available"]


def _monthly_contribution(conn: sqlite3.Connection, base: str,
                          assumptions: Assumptions) -> float:
    if assumptions.monthly_contribution is not None:
        return assumptions.monthly_contribution
    rec = allocation.recurring_summary(conn, base)
    return max(0.0, rec["monthly_net"])


def deterministic(conn: sqlite3.Connection, base: str, assumptions: Assumptions) -> list[dict]:
    capital = _starting_capital(conn, base)
    contribution = _monthly_contribution(conn, base, assumptions)
    monthly_return = (1 + assumptions.expected_real_return_pct / 100) ** (1 / 12) - 1

    out = [{"year": 0, "value": capital, "contributed": 0.0}]
    value = capital
    contributed = 0.0
    for year in range(1, assumptions.years + 1):
        annual_contrib = contribution * 12 * (
            (1 + assumptions.contribution_growth_pct / 100) ** (year - 1)
        )
        monthly = annual_contrib / 12
        for _ in range(12):
            value = value * (1 + monthly_return) + monthly
            contributed += monthly
        out.append({"year": year, "value": value, "contributed": contributed})
    return out


def monte_carlo(conn: sqlite3.Connection, base: str, assumptions: Assumptions) -> dict:
    """Lognormal annual returns. 10k paths x 30 years runs in well under a second
    on a Pi 4, so there's no reason to cheap out on path count."""
    capital = _starting_capital(conn, base)
    contribution = _monthly_contribution(conn, base, assumptions)

    rng = np.random.default_rng(assumptions.seed)
    n, years = assumptions.paths, assumptions.years

    mu = assumptions.expected_real_return_pct / 100
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
        annual_contrib = contribution * 12 * (
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
        "monthly_contribution": contribution,
        "terminal": {
            "p10": bands[-1]["p10"], "p50": bands[-1]["p50"], "p90": bands[-1]["p90"],
            "mean": bands[-1]["mean"],
        },
        "note": "Real terms: figures are in today's purchasing power, so no "
                "inflation adjustment is applied on top.",
    }


def run(conn: sqlite3.Connection, base: str, cfg: dict) -> dict:
    assumptions = Assumptions.from_config(cfg)
    return {
        "assumptions": assumptions,
        "deterministic": deterministic(conn, base, assumptions),
        "monte_carlo": monte_carlo(conn, base, assumptions),
    }


def target_year(bands: list[dict], target: float, percentile: str = "p50") -> int | None:
    """First projection year where the given percentile path reaches a target."""
    for band in bands:
        if band[percentile] >= target:
            return band["year"]
    return None

"""Portfolio returns: TWR, MWR (money-weighted / IRR), and drawdown.

Why this module exists at all: balance snapshots alone cannot tell you your
return. A portfolio going 100k -> 110k might be 10% growth, or 5% growth plus a
5k deposit. Separating the two requires cash flows. Everything here reads
portfolio_cash_flows; if that table is empty the numbers below are wrong and the
functions say so rather than quietly returning a plausible-looking figure.

TWR   = compounded sub-period returns, bracketed by cash flows. Measures the
        performance of the assets. This is what you compare against an index.
MWR   = internal rate of return on the actual cash flow timeline. Measures your
        outcome as an investor, including whether your timing helped.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from dataclasses import dataclass

from ..collectors.fx import convert_minor
from ..db import from_minor


@dataclass
class ReturnResult:
    twr: float | None
    mwr: float | None
    start: str | None
    end: str | None
    start_value: float
    end_value: float
    net_contributions: float
    gain: float
    warnings: list[str]


def _brokerage_series(conn: sqlite3.Connection, base: str, days: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        """
        WITH daily AS (
            SELECT substr(b.ts,1,10) AS day, b.account_id, b.balance_minor,
                   b.currency, b.ts,
                   ROW_NUMBER() OVER (PARTITION BY b.account_id, substr(b.ts,1,10)
                                      ORDER BY b.ts DESC) AS rn
            FROM balance_snapshots b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.kind = 'brokerage'
              AND b.balance_type IN ('TotalValue','manual','default')
              AND b.ts >= date('now', ?)
        )
        SELECT day, account_id, balance_minor, currency, ts FROM daily
        WHERE rn = 1 ORDER BY day
        """,
        (f"-{days} days",),
    ).fetchall()

    carried: dict[int, tuple[int, str, str]] = {}
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    series: list[tuple[str, int]] = []
    for day in sorted(by_day):
        for r in by_day[day]:
            carried[r["account_id"]] = (r["balance_minor"], r["currency"], r["ts"])
        total = sum(convert_minor(conn, m, c, base, t) for m, c, t in carried.values())
        series.append((day, total))
    return series


def _flows(conn: sqlite3.Connection, base: str, days: int) -> dict[str, int]:
    """External flows per day, in base-currency minor units.

    Only deposits and withdrawals count as external flows. Dividends and interest
    earned inside the portfolio are returns, not contributions — counting them as
    flows would understate your performance.
    """
    rows = conn.execute(
        """
        SELECT substr(f.ts,1,10) AS day, f.amount_minor, f.currency, f.ts
        FROM portfolio_cash_flows f
        JOIN accounts a ON a.id = f.account_id
        WHERE f.kind IN ('deposit','withdrawal')
          AND a.kind = 'brokerage'
          AND f.ts >= date('now', ?)
        """,
        (f"-{days} days",),
    ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        out[r["day"]] = out.get(r["day"], 0) + convert_minor(
            conn, r["amount_minor"], r["currency"], base, r["ts"]
        )
    return out


def compute(conn: sqlite3.Connection, base: str = "EUR", days: int = 365) -> ReturnResult:
    warnings: list[str] = []
    series = _brokerage_series(conn, base, days)

    if len(series) < 2:
        return ReturnResult(None, None, None, None, 0.0, 0.0, 0.0, 0.0,
                            ["Not enough balance history yet. Returns need at least "
                             "two days of snapshots."])

    flows = _flows(conn, base, days)
    if not flows:
        warnings.append(
            "No deposits or withdrawals recorded, so TWR and MWR assume none "
            "occurred. If you funded the account in this window the figures are "
            "wrong — add [[cash_flow]] entries to config/assets.toml."
        )

    # --- TWR: chain daily returns, removing the effect of same-day flows ----
    twr_factor = 1.0
    skipped = 0
    for i in range(1, len(series)):
        prev_day, prev_val = series[i - 1]
        day, val = series[i]
        flow = flows.get(day, 0)
        denom = prev_val + flow  # assume flow lands at start of day
        if denom <= 0:
            skipped += 1
            continue
        twr_factor *= val / denom
    twr = (twr_factor - 1.0) * 100
    if skipped:
        warnings.append(f"{skipped} day(s) skipped in TWR (non-positive base value).")

    # --- MWR: IRR over flows plus terminal value ---------------------------
    start_day, start_val = series[0]
    end_day, end_val = series[-1]

    cashflows: list[tuple[date, float]] = [(_d(start_day), -from_minor(start_val))]
    for day, minor in sorted(flows.items()):
        if day <= start_day or day > end_day:
            continue
        cashflows.append((_d(day), -from_minor(minor)))
    cashflows.append((_d(end_day), from_minor(end_val)))

    mwr = _xirr(cashflows)
    mwr_pct = mwr * 100 if mwr is not None else None
    if mwr is None:
        warnings.append("MWR did not converge — usually means very short history "
                        "or flows that dominate the portfolio value.")

    net_contrib = sum(from_minor(m) for d, m in flows.items() if start_day < d <= end_day)
    gain = from_minor(end_val) - from_minor(start_val) - net_contrib

    return ReturnResult(
        twr=twr,
        mwr=mwr_pct,
        start=start_day,
        end=end_day,
        start_value=from_minor(start_val),
        end_value=from_minor(end_val),
        net_contributions=net_contrib,
        gain=gain,
        warnings=warnings,
    )


def max_drawdown(series: list[dict]) -> dict | None:
    """Largest peak-to-trough decline in the value series."""
    if len(series) < 2:
        return None
    peak = series[0]["value"]
    peak_date = series[0]["date"]
    worst = 0.0
    result = None
    for point in series[1:]:
        if point["value"] > peak:
            peak, peak_date = point["value"], point["date"]
            continue
        if peak <= 0:
            continue
        dd = (point["value"] - peak) / peak
        if dd < worst:
            worst = dd
            result = {"pct": dd * 100, "peak_date": peak_date, "trough_date": point["date"],
                      "peak": peak, "trough": point["value"]}
    return result


# --------------------------------------------------------------------------
# XIRR by bisection. No scipy: it's a heavy dependency for one root-find, and
# bisection on a bracketed monotonic-enough function is plenty here.
# --------------------------------------------------------------------------

def _d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _npv(rate: float, flows: list[tuple[date, float]]) -> float:
    t0 = flows[0][0]
    total = 0.0
    for when, amount in flows:
        years = (when - t0).days / 365.0
        total += amount / ((1.0 + rate) ** years)
    return total


def _xirr(flows: list[tuple[date, float]], lo: float = -0.9999, hi: float = 10.0,
          tol: float = 1e-7, max_iter: int = 200) -> float | None:
    flows = sorted(flows, key=lambda f: f[0])
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    f_lo, f_hi = _npv(lo, flows), _npv(hi, flows)
    if f_lo * f_hi > 0:
        return None

    for _ in range(max_iter):
        mid = (lo + hi) / 2
        f_mid = _npv(mid, flows)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2

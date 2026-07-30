"""Allocation breakdowns from the latest position snapshot, plus concentration."""

from __future__ import annotations

import sqlite3

from ..collectors.fx import convert_minor
from ..db import from_minor
from . import networth

CLASS_ORDER = ["Equity", "Bonds", "Cash", "Savings", "Other"]

_ASSET_CLASS_TO_CLASS = {"equity": "Equity", "etf": "Equity", "fund": "Equity", "bond": "Bonds"}
_ACCOUNT_KIND_TO_CLASS = {"savings": "Savings", "checking": "Cash"}


def _latest_positions(conn: sqlite3.Connection, base: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.*, a.label AS account_label, a.institution
        FROM position_snapshots p
        JOIN accounts a ON a.id = p.account_id
        JOIN (SELECT account_id, MAX(ts) AS ts FROM position_snapshots GROUP BY account_id) m
             ON m.account_id = p.account_id AND m.ts = p.ts
        """
    ).fetchall()
    out = []
    for r in rows:
        value = convert_minor(conn, r["market_value_minor"], r["currency"], base, r["ts"])
        out.append({
            "symbol": r["symbol"] or r["instrument_id"],
            "name": r["name"],
            "isin": r["isin"],
            "asset_class": r["asset_class"] or "other",
            "quantity": r["quantity"],
            "last_price": r["last_price"],
            "currency": r["currency"],
            "value": from_minor(value),
            "value_minor": value,
            "unrealized_pl": from_minor(r["unrealized_pl_minor"] or 0),
            "exchange": r["exchange"],
            "country": r["country"],
            "account": r["account_label"],
            "ts": r["ts"],
        })
    return sorted(out, key=lambda p: -p["value_minor"])


def positions(conn: sqlite3.Connection, base: str = "EUR") -> list[dict]:
    return _latest_positions(conn, base)


def _group(rows: list[dict], key: str) -> list[dict]:
    total = sum(r["value_minor"] for r in rows) or 1
    buckets: dict[str, int] = {}
    for r in rows:
        k = r.get(key) or "unknown"
        buckets[k] = buckets.get(k, 0) + r["value_minor"]
    return sorted(
        [{"label": k, "value": from_minor(v), "pct": v / total * 100}
         for k, v in buckets.items()],
        key=lambda b: -b["value"],
    )


def breakdown(conn: sqlite3.Connection, base: str = "EUR") -> dict:
    rows = _latest_positions(conn, base)
    return {
        "by_asset_class": _group(rows, "asset_class"),
        "by_currency": _group(rows, "currency"),
        "by_country": _group(rows, "country"),
        "by_account": _group(rows, "account"),
        "position_count": len(rows),
    }


def net_worth_by_class(conn: sqlite3.Connection, base: str = "EUR") -> list[dict]:
    """Every account and position counting toward net worth, bucketed into a
    small fixed taxonomy (Equity, Bonds, Cash, Savings, Other) instead of the
    raw provider asset_class or account kind. Brokerage accounts are split into
    their own cash balance plus their positions' asset classes, rather than
    counted as one lump so their internal composition isn't lost."""
    buckets: dict[str, int] = {c: 0 for c in CLASS_ORDER}

    brokerage_ids = {
        r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE include_in_networth = 1 AND kind = 'brokerage'"
        ).fetchall()
    }

    for b in networth.current_balances(conn, base):
        if b.account_id in brokerage_ids:
            continue
        label = _ACCOUNT_KIND_TO_CLASS.get(b.kind, "Other")
        buckets[label] += b.amount_base_minor

    for account_id in brokerage_ids:
        row = conn.execute(
            "SELECT balance_minor, currency, ts FROM balance_snapshots "
            "WHERE account_id = ? AND balance_type = 'CashBalance' "
            "ORDER BY ts DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if row:
            buckets["Cash"] += convert_minor(conn, row["balance_minor"], row["currency"], base, row["ts"])

    if brokerage_ids:
        placeholders = ",".join("?" * len(brokerage_ids))
        positions = conn.execute(
            f"SELECT asset_class, market_value_minor, currency, ts FROM latest_positions "
            f"WHERE account_id IN ({placeholders})",
            tuple(brokerage_ids),
        ).fetchall()
        for p in positions:
            value = convert_minor(conn, p["market_value_minor"], p["currency"], base, p["ts"])
            buckets[_ASSET_CLASS_TO_CLASS.get(p["asset_class"] or "", "Other")] += value

    total = sum(buckets.values()) or 1
    return [
        {"label": label, "value": from_minor(buckets[label]), "pct": buckets[label] / total * 100}
        for label in CLASS_ORDER if buckets[label]
    ]


def concentration(conn: sqlite3.Connection, base: str = "EUR", top: int = 5) -> dict:
    """Top-N share of the portfolio. The single most useful risk number for a
    small portfolio, and the one people are most surprised by."""
    rows = _latest_positions(conn, base)
    total = sum(r["value_minor"] for r in rows)
    if not total:
        return {"top": [], "top_n_pct": None, "hhi": None}
    top_rows = rows[:top]
    hhi = sum((r["value_minor"] / total) ** 2 for r in rows)
    return {
        "top": [{"symbol": r["symbol"], "name": r["name"], "value": r["value"],
                 "pct": r["value_minor"] / total * 100} for r in top_rows],
        "top_n_pct": sum(r["value_minor"] for r in top_rows) / total * 100,
        # Herfindahl index: 1.0 = one holding, 1/n = perfectly equal weights.
        "hhi": hhi,
        "effective_holdings": 1 / hhi if hhi else None,
    }


def recurring_summary(conn: sqlite3.Connection, base: str = "EUR") -> dict:
    """Monthly-normalised recurring income and cost."""
    rows = conn.execute("SELECT * FROM recurring WHERE active = 1").fetchall()
    divisor = {"monthly": 1.0, "quarterly": 3.0, "yearly": 12.0}
    income = cost = 0
    lines = []
    for r in rows:
        monthly_minor = round(r["amount_minor"] / divisor[r["period"]])
        monthly_base = convert_minor(conn, monthly_minor, r["currency"], base)
        if r["kind"] == "income":
            income += monthly_base
        else:
            cost += monthly_base
        lines.append({"label": r["label"], "kind": r["kind"],
                      "monthly": from_minor(monthly_base),
                      "category": r["category"], "period": r["period"]})

    net = income - cost
    return {
        "monthly_income": from_minor(income),
        "monthly_cost": from_minor(cost),
        "monthly_net": from_minor(net),
        "savings_rate_pct": (net / income * 100) if income else None,
        "lines": sorted(lines, key=lambda i: -i["monthly"]),
    }

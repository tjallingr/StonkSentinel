"""Net worth: current composition and the historical series.

Design note: net worth is derived from balance snapshots only, never from
positions. Positions are for allocation and performance. Deriving net worth from
both would double-count, since a Saxo TotalValue balance already includes the
market value of every position in that account.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..collectors.fx import convert_minor
from ..db import from_minor

# For Saxo accounts prefer TotalValue (cash + positions) over CashBalance.
BALANCE_TYPE_PRIORITY = {
    "TotalValue": 100,
    "manual": 90,
    "CLBD": 80, "closingBooked": 80,
    "ITAV": 70, "interimAvailable": 70,
    "XPCD": 60, "expected": 60,
    "ITBD": 50, "interimBooked": 50,
    "default": 40,
    "OTHR": 30, "other": 30,
    "CashBalance": 10,
}


@dataclass
class AccountBalance:
    account_id: int
    label: str
    institution: str | None
    provider: str
    kind: str
    liquid: bool
    encumbered: bool
    amount_minor: int
    currency: str
    amount_base_minor: int
    ts: str

    @property
    def amount_base(self) -> float:
        return from_minor(self.amount_base_minor)


def _best_balance_type(rows: list[sqlite3.Row]) -> sqlite3.Row:
    return max(rows, key=lambda r: BALANCE_TYPE_PRIORITY.get(r["balance_type"], 0))


def current_balances(conn: sqlite3.Connection, base: str = "EUR") -> list[AccountBalance]:
    """One balance per account: latest timestamp, best balance type."""
    rows = conn.execute(
        """
        SELECT b.account_id, b.balance_minor, b.currency, b.balance_type, b.ts,
               a.label, a.institution, a.provider, a.kind, a.liquid, a.encumbered
        FROM balance_snapshots b
        JOIN accounts a ON a.id = b.account_id
        JOIN (SELECT account_id, MAX(ts) AS ts FROM balance_snapshots GROUP BY account_id) m
             ON m.account_id = b.account_id AND m.ts = b.ts
        WHERE a.include_in_networth = 1
        """
    ).fetchall()

    by_account: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_account.setdefault(r["account_id"], []).append(r)

    out: list[AccountBalance] = []
    for account_id, candidates in by_account.items():
        r = _best_balance_type(candidates)
        out.append(AccountBalance(
            account_id=account_id,
            label=r["label"],
            institution=r["institution"],
            provider=r["provider"],
            kind=r["kind"],
            liquid=bool(r["liquid"]),
            encumbered=bool(r["encumbered"]),
            amount_minor=r["balance_minor"],
            currency=r["currency"],
            amount_base_minor=convert_minor(conn, r["balance_minor"], r["currency"], base, r["ts"]),
            ts=r["ts"],
        ))
    return sorted(out, key=lambda a: -a.amount_base_minor)


def summary(conn: sqlite3.Connection, base: str = "EUR") -> dict:
    """Headline figures.

    'available' excludes encumbered accounts — your KBC collateral savings count
    toward net worth but you cannot spend them, and a dashboard that conflates the
    two is lying to you about the number you'd actually act on.
    """
    balances = current_balances(conn, base)
    total = sum(b.amount_base_minor for b in balances)
    liquid = sum(b.amount_base_minor for b in balances if b.liquid)
    available = sum(b.amount_base_minor for b in balances if b.liquid and not b.encumbered)
    encumbered = sum(b.amount_base_minor for b in balances if b.encumbered)
    invested = sum(b.amount_base_minor for b in balances if b.kind == "brokerage")

    return {
        "net_worth": from_minor(total),
        "liquid": from_minor(liquid),
        "available": from_minor(available),
        "encumbered": from_minor(encumbered),
        "invested": from_minor(invested),
        "cash": from_minor(liquid - invested),
        "currency": base,
        "accounts": balances,
    }


def history(conn: sqlite3.Connection, base: str = "EUR", days: int = 730) -> list[dict]:
    """Daily net worth series.

    Uses each account's last snapshot on or before each day, carried forward. An
    account with no snapshot yet on a given day contributes nothing — so the early
    part of the series is thin until every collector has run a few times. That is
    honest: don't backfill guesses into history.
    """
    rows = conn.execute(
        """
        WITH daily AS (
            SELECT b.account_id,
                   substr(b.ts, 1, 10) AS day,
                   b.balance_minor,
                   b.currency,
                   b.balance_type,
                   b.ts,
                   ROW_NUMBER() OVER (
                       PARTITION BY b.account_id, substr(b.ts, 1, 10)
                       ORDER BY b.ts DESC
                   ) AS rn
            FROM balance_snapshots b
            JOIN accounts a ON a.id = b.account_id
            WHERE a.include_in_networth = 1
              AND b.balance_type != 'CashBalance'
              AND b.ts >= date('now', ?)
        )
        SELECT account_id, day, balance_minor, currency, ts
        FROM daily WHERE rn = 1
        ORDER BY day
        """,
        (f"-{days} days",),
    ).fetchall()

    if not rows:
        return []

    days_sorted = sorted({r["day"] for r in rows})
    carried: dict[int, tuple[int, str, str]] = {}
    by_day: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    series: list[dict] = []
    for day in days_sorted:
        for r in by_day.get(day, []):
            carried[r["account_id"]] = (r["balance_minor"], r["currency"], r["ts"])
        total = sum(
            convert_minor(conn, minor, ccy, base, ts)
            for minor, ccy, ts in carried.values()
        )
        series.append({"date": day, "value": from_minor(total)})
    return series


def change(series: list[dict], days: int) -> dict | None:
    """Absolute and relative change over the trailing window of the series."""
    if len(series) < 2:
        return None
    latest = series[-1]
    window = series[-(days + 1):] if len(series) > days else series
    first = window[0]
    delta = latest["value"] - first["value"]
    pct = (delta / first["value"] * 100) if first["value"] else None
    return {"from": first["date"], "abs": delta, "pct": pct}

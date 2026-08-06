"""Expense analysis over bank transactions.

Grouped by payee, where a payee is the counterparty IBAN when the bank gives one
and a folded version of the counterparty name when it doesn't. IBAN first because
it survives the bank rewriting the display name ("ALBERT HEIJN 1234" one month,
"AH TO GO AMSTERDAM" the next) and because "which account numbers do I pay the
most" is the question this module exists to answer.

All aggregation happens in SQL. On a Pi it matters that a year of transactions is
summed in C and only a handful of per-currency totals cross into Python for FX
conversion — not that we pull ten thousand rows into a list of dicts.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from ..collectors.fx import convert_minor
from ..db import from_minor


def _months_ago(months: int) -> str:
    """First day of the month `months` back. Month boundaries, not 30-day windows:
    a month-over-month comparison against a rolling window is meaningless."""
    today = date.today()
    year, month = divmod((today.year * 12 + today.month - 1) - months, 12)
    return f"{year:04d}-{month + 1:02d}-01"


def monthly(conn: sqlite3.Connection, base: str = "EUR", months: int = 12) -> list[dict]:
    """Spend and income per calendar month, oldest first. Includes empty months so
    the chart doesn't silently compress a gap in collection into a straight line."""
    since = _months_ago(months)
    rows = conn.execute(
        """
        SELECT substr(booking_date, 1, 7) AS month,
               currency,
               SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END) AS out_minor,
               SUM(CASE WHEN amount_minor > 0 THEN  amount_minor ELSE 0 END) AS in_minor,
               SUM(CASE WHEN amount_minor < 0 THEN 1 ELSE 0 END)             AS n
        FROM external_transactions
        WHERE booking_date >= ?
        GROUP BY month, currency
        """,
        (since,),
    ).fetchall()

    buckets: dict[str, dict] = {}
    for r in rows:
        b = buckets.setdefault(r["month"], {"month": r["month"], "out": 0, "in": 0, "n": 0})
        as_of = f"{r['month']}-15"
        b["out"] += convert_minor(conn, int(r["out_minor"]), r["currency"], base, as_of)
        b["in"] += convert_minor(conn, int(r["in_minor"]), r["currency"], base, as_of)
        b["n"] += int(r["n"])

    # Fill gaps between the first month we have and now.
    out: list[dict] = []
    cursor = date.fromisoformat(since)
    end = date.today().replace(day=1)
    while cursor <= end:
        key = cursor.strftime("%Y-%m")
        b = buckets.get(key, {"month": key, "out": 0, "in": 0, "n": 0})
        out.append({
            "month": key,
            "spend": from_minor(b["out"]),
            "income": from_minor(b["in"]),
            "net": from_minor(b["in"] - b["out"]),
            "count": b["n"],
            # The running month is only part-filled. Flagged so the chart can dim it
            # and the average can leave it out, rather than reading as a big drop.
            "partial": cursor == end,
        })
        cursor = (cursor + timedelta(days=31)).replace(day=1)
    return out


def top_payees(conn: sqlite3.Connection, base: str = "EUR", months: int = 12,
               limit: int = 25) -> list[dict]:
    """Biggest recipients of your money over the window.

    `label` is the most recent name the bank used for that payee rather than the
    most common one: when a company rebrands you want to recognise it today.
    """
    since = _months_ago(months)
    rows = conn.execute(
        """
        SELECT payee_key,
               currency,
               SUM(-amount_minor) AS total_minor,
               COUNT(*)           AS n,
               MIN(booking_date)  AS first_seen,
               MAX(booking_date)  AS last_seen
        FROM expenses
        WHERE booking_date >= ? AND payee_key IS NOT NULL
        GROUP BY payee_key, currency
        """,
        (since,),
    ).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["payee_key"], {
            "payee_key": r["payee_key"], "total_minor": 0, "count": 0,
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
        })
        a["total_minor"] += convert_minor(
            conn, int(r["total_minor"]), r["currency"], base, r["last_seen"]
        )
        a["count"] += int(r["n"])
        a["first_seen"] = min(a["first_seen"], r["first_seen"])
        a["last_seen"] = max(a["last_seen"], r["last_seen"])

    if not agg:
        return []

    # One query for the display names instead of one per payee. With MAX() as the
    # only aggregate, SQLite guarantees the bare columns come from the row that
    # matched it — so this is the counterparty name as of the latest payment.
    placeholders = ",".join("?" * len(agg))
    names = conn.execute(
        f"""
        SELECT payee_key, counterparty, counterparty_iban, MAX(booking_date)
        FROM expenses
        WHERE payee_key IN ({placeholders})
        GROUP BY payee_key
        """,
        list(agg),
    ).fetchall()
    lookup = {r["payee_key"]: r for r in names}

    total = sum(a["total_minor"] for a in agg.values()) or 1
    span_months = max(1, months)
    out = []
    for a in agg.values():
        meta = lookup.get(a["payee_key"])
        out.append({
            "payee_key": a["payee_key"],
            "label": (meta["counterparty"] if meta else None) or a["payee_key"],
            "iban": meta["counterparty_iban"] if meta else None,
            "total": from_minor(a["total_minor"]),
            "monthly": from_minor(round(a["total_minor"] / span_months)),
            "count": a["count"],
            "avg": from_minor(round(a["total_minor"] / a["count"])) if a["count"] else 0.0,
            "pct": a["total_minor"] / total * 100,
            "first_seen": a["first_seen"],
            "last_seen": a["last_seen"],
        })
    out.sort(key=lambda p: -p["total"])
    return out[:limit]


def internal(conn: sqlite3.Connection, base: str = "EUR", months: int = 12) -> dict:
    """What the own-account filter took out of the figures above.

    Only the outgoing legs are summed. A transfer between two accounts we collect
    is booked twice — once leaving, once arriving — so adding both would report a
    €500 sweep as €1000 moved. `accounts` counts the IBANs currently treated as
    mine, which is the number to check when a transfer you expected to vanish
    didn't: the filter can only neutralize an account it knows about.
    """
    since = _months_ago(months)
    rows = conn.execute(
        """
        SELECT currency,
               SUM(CASE WHEN amount_minor < 0 THEN -amount_minor ELSE 0 END) AS moved_minor,
               COUNT(*)                                                      AS n
        FROM internal_transfers
        WHERE booking_date >= ?
        GROUP BY currency
        """,
        (since,),
    ).fetchall()

    today = date.today().isoformat()
    moved = sum(
        convert_minor(conn, int(r["moved_minor"]), r["currency"], base, today)
        for r in rows
    )
    return {
        "moved": from_minor(moved),
        "count": sum(int(r["n"]) for r in rows),
        "accounts": conn.execute(
            "SELECT COUNT(*) AS n FROM own_iban_registry"
        ).fetchone()["n"],
    }


def summary(conn: sqlite3.Connection, base: str = "EUR", months: int = 12) -> dict:
    """Headline figures. `average` deliberately excludes the current month, which
    is always partial and would drag the comparison down every time you look."""
    series = monthly(conn, base, months=months)
    this_month = series[-1] if series else None
    complete = [m for m in series[:-1] if m["count"]]

    average = sum(m["spend"] for m in complete) / len(complete) if complete else None
    return {
        "months": months,
        "series": series,
        "this_month": this_month,
        "last_month": complete[-1] if complete else None,
        "average": average,
        "vs_average_pct": (
            (this_month["spend"] / average - 1) * 100
            if this_month and average else None
        ),
        "total": sum(m["spend"] for m in series),
        "payee_count": conn.execute(
            "SELECT COUNT(DISTINCT payee_key) AS n FROM expenses WHERE booking_date >= ?",
            (_months_ago(months),),
        ).fetchone()["n"],
        "tx_count": conn.execute(
            "SELECT COUNT(*) AS n FROM transactions"
        ).fetchone()["n"],
        "internal": internal(conn, base, months=months),
        "coverage": conn.execute(
            "SELECT MIN(booking_date) AS a, MAX(booking_date) AS b FROM transactions"
        ).fetchone(),
    }


def transactions(conn: sqlite3.Connection, base: str = "EUR", *,
                 payee_key: str | None = None, months: int = 12,
                 limit: int = 200) -> list[dict]:
    """Recent movements, newest first. Drill-down for one payee when given."""
    sql = [
        "SELECT * FROM external_transactions WHERE booking_date >= ?",
    ]
    params: list = [_months_ago(months)]
    if payee_key:
        sql.append("AND payee_key = ?")
        params.append(payee_key)
    sql.append("ORDER BY booking_date DESC, id DESC LIMIT ?")
    params.append(limit)

    return [
        {
            "date": r["booking_date"],
            "amount": from_minor(
                convert_minor(conn, r["amount_minor"], r["currency"], base, r["booking_date"])
            ),
            "currency": r["currency"],
            "counterparty": r["counterparty"] or r["payee_key"] or "—",
            "iban": r["counterparty_iban"],
            "description": r["description"],
            "account": r["account_label"],
            "payee_key": r["payee_key"],
        }
        for r in conn.execute(" ".join(sql), params).fetchall()
    ]

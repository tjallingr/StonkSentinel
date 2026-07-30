import sqlite3

from ...collectors.fx import convert_minor
from ...db import from_minor
from .categories import Category
from .classify import classify


def current_values(conn: sqlite3.Connection, base: str, categories: list[Category]) -> dict[str, float]:
    totals = {category.key: 0.0 for category in categories}

    positions = conn.execute(
        """
        SELECT p.symbol, p.market_value_minor, p.currency, p.ts
        FROM latest_positions p
        JOIN accounts a ON a.id = p.account_id
        WHERE a.provider = 'saxo'
        """
    ).fetchall()
    for p in positions:
        key = classify(p["symbol"], categories)
        value = convert_minor(conn, p["market_value_minor"], p["currency"], base, p["ts"])
        totals[key] = totals.get(key, 0.0) + from_minor(value)

    cash = conn.execute(
        """
        SELECT b.balance_minor, b.currency, b.ts
        FROM latest_balances b
        JOIN accounts a ON a.id = b.account_id
        WHERE a.provider = 'saxo' AND b.balance_type = 'CashBalance'
        """
    ).fetchall()
    for b in cash:
        value = convert_minor(conn, b["balance_minor"], b["currency"], base, b["ts"])
        totals["cash"] = totals.get("cash", 0.0) + from_minor(value)

    return totals
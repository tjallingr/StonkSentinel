import sqlite3

from ...collectors.fx import convert_minor
from ...db import from_minor
from .categories import Category
from .classify import classify
from .model import Asset

_ASSET_CLASS_TO_BUCKET = {"equity": "Equity", "etf": "Equity", "fund": "Equity", "bond": "Bonds"}


def _saxo_cash(conn: sqlite3.Connection, base: str) -> float:
    rows = conn.execute(
        """
        SELECT b.balance_minor, b.currency, b.ts
        FROM latest_balances b
        JOIN accounts a ON a.id = b.account_id
        WHERE a.provider = 'saxo' AND b.balance_type = 'CashBalance'
        """
    ).fetchall()
    total = 0
    for b in rows:
        total += convert_minor(conn, b["balance_minor"], b["currency"], base, b["ts"])
    return from_minor(total)


def position_assets(conn: sqlite3.Connection, base: str, categories: list[Category]) -> list[Asset]:
    """Saxo positions summed per matching category, plus Saxo's own cash balance
    under the auto-injected 'cash' category. A category without an explicit
    bucket takes one from the first matching position's own Saxo asset_class,
    so day-one behaviour is sensible before anyone configures `bucket` by hand."""
    values = {c.key: 0.0 for c in categories}
    buckets = {c.key: c.bucket for c in categories}

    positions = conn.execute(
        """
        SELECT p.symbol, p.asset_class, p.market_value_minor, p.currency, p.ts
        FROM latest_positions p
        JOIN accounts a ON a.id = p.account_id
        WHERE a.provider = 'saxo'
        """
    ).fetchall()
    for p in positions:
        key = classify(p["symbol"], categories)
        value = convert_minor(conn, p["market_value_minor"], p["currency"], base, p["ts"])
        values[key] = values.get(key, 0.0) + from_minor(value)
        if not buckets.get(key):
            buckets[key] = _ASSET_CLASS_TO_BUCKET.get(p["asset_class"] or "", "Other")

    values["cash"] = values.get("cash", 0.0) + _saxo_cash(conn, base)

    assets = []
    for c in categories:
        value = values.get(c.key, 0.0)
        if not value and not c.monthly_contribution:
            continue
        assets.append(Asset(
            key=c.key, label=c.label, category=buckets.get(c.key) or "Other",
            value_base=value, currency=base,
            yield_pct=c.expected_real_return_pct,
            monthly_contribution=c.monthly_contribution,
            provider="saxo",
        ))
    return assets

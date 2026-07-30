import sqlite3

from .categories import load_categories
from .combine import combine
from .growth import compound
from .holdings import current_values


def run(conn: sqlite3.Connection, base: str, assets, years: int) -> dict:
    fallback_pct = float(assets.projection.get("expected_real_return_pct", 5.0))
    categories = load_categories(assets.saxo_projection_categories, fallback_pct)
    start_values = current_values(conn, base, categories)

    series = {
        category.key: compound(start_values[category.key], category.expected_real_return_pct, years)
        for category in categories
    }
    total = combine(series)

    return {
        "categories": categories,
        "start_values": start_values,
        "series": series,
        "total": total,
    }
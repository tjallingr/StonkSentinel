import sqlite3

from ...config import AssetsConfig
from .. import allocation, networth
from .categories import load_categories
from .model import Asset
from .positions import position_assets
from .resolve import resolve_balance


def gather(conn: sqlite3.Connection, base: str, assets_cfg: AssetsConfig) -> list[Asset]:
    """Every account and position counting toward net worth, as a flat list of
    Assets, each carrying its own category/yield/contribution. The one pipeline
    everything else (composition, projection) is built on top of."""
    fallback_pct = float(assets_cfg.projection.get("expected_real_return_pct", 5.0))
    categories = load_categories(assets_cfg.saxo_projection_categories, fallback_pct)
    out = position_assets(conn, base, categories)

    manual_by_key = {a["key"]: a for a in assets_cfg.assets}
    for b in networth.current_balances(conn, base):
        if b.kind == "brokerage":
            continue
        category, yield_pct, contribution = resolve_balance(
            provider=b.provider, external_id=b.external_id, kind=b.kind,
            manual_assets_by_key=manual_by_key,
            account_overrides=assets_cfg.account_overrides,
        )
        out.append(Asset(
            key=f"{b.provider}:{b.account_id}", label=b.label, category=category,
            value_base=b.amount_base, currency=base,
            yield_pct=yield_pct, monthly_contribution=contribution,
            liquid=b.liquid, provider=b.provider, institution=b.institution,
            account_id=b.account_id,
        ))

    if not any(a.monthly_contribution for a in out):
        configured = assets_cfg.projection.get("monthly_contribution")
        amount = (float(configured) if configured is not None
                  else max(0.0, allocation.recurring_summary(conn, base)["monthly_net"]))
        if amount:
            out.append(Asset(
                key="unallocated-savings", label="Unallocated savings",
                category=assets_cfg.projection.get("unallocated_category", "Cash"),
                value_base=0.0, currency=base,
                yield_pct=fallback_pct, monthly_contribution=amount,
            ))
    return out

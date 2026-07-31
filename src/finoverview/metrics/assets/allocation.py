from .model import CLASS_ORDER, Asset


def by_category(assets: list[Asset]) -> list[dict]:
    totals: dict[str, float] = {}
    for a in assets:
        totals[a.category] = totals.get(a.category, 0.0) + a.value_base
    total = sum(totals.values()) or 1
    ordered = [c for c in CLASS_ORDER if c in totals] + [c for c in totals if c not in CLASS_ORDER]
    return [
        {"label": c, "value": totals[c], "pct": totals[c] / total * 100}
        for c in ordered if totals[c]
    ]

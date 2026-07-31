from .growth import compound
from .model import Asset


def deterministic_by_asset(assets: list[Asset], years: int,
                           contribution_growth_pct: float) -> dict[str, list[float]]:
    return {
        a.key: compound(a.value_base, a.yield_pct, years,
                        monthly_contribution=a.monthly_contribution,
                        contribution_growth_pct=contribution_growth_pct)
        for a in assets
    }


def weighted_average_yield_pct(assets: list[Asset]) -> float:
    total = sum(a.value_base for a in assets)
    if not total:
        return 0.0
    return sum(a.value_base * a.yield_pct for a in assets) / total


def total_monthly_contribution(assets: list[Asset]) -> float:
    return sum(a.monthly_contribution for a in assets)

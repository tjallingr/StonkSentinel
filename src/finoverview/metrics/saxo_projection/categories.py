from dataclasses import dataclass, field


@dataclass
class Category:
    key: str
    label: str
    expected_real_return_pct: float
    symbols: list[str] = field(default_factory=list)


def load_categories(raw: list[dict], fallback_return_pct: float) -> list[Category]:
    categories = [
        Category(
            key=c["key"],
            label=c.get("label", c["key"]),
            expected_real_return_pct=float(c["expected_real_return_pct"]),
            symbols=list(c.get("symbols", [])),
        )
        for c in raw
    ]
    keys = {c.key for c in categories}
    if "cash" not in keys:
        categories.append(Category("cash", "Cash", 0.0))
    if "other" not in keys:
        categories.append(Category("other", "Other", fallback_return_pct))
    return categories
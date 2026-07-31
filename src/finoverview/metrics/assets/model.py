from dataclasses import dataclass

CLASS_ORDER = ["Equity", "Bonds", "Cash", "Savings", "Other"]


@dataclass
class Asset:
    key: str
    label: str
    category: str
    value_base: float
    currency: str
    yield_pct: float = 0.0
    monthly_contribution: float = 0.0
    quantity: float | None = None
    liquid: bool = True
    provider: str | None = None
    institution: str | None = None
    account_id: int | None = None

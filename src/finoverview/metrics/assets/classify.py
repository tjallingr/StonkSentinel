from .categories import Category


def classify(symbol: str | None, categories: list[Category]) -> str:
    ticker = (symbol or "").split(":")[0]
    for category in categories:
        if ticker and ticker in category.symbols:
            return category.key
    return "other"

def combine(series_by_key: dict[str, list[float]]) -> list[float]:
    return [sum(values) for values in zip(*series_by_key.values())]

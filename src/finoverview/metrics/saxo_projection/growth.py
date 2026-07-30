def compound(value: float, rate_pct: float, years: int) -> list[float]:
    rate = rate_pct / 100
    series = [value]
    for _ in range(years):
        series.append(series[-1] * (1 + rate))
    return series
def compound(value: float, rate_pct: float, years: int, *,
             monthly_contribution: float = 0.0,
             contribution_growth_pct: float = 0.0) -> list[float]:
    monthly_rate = (1 + rate_pct / 100) ** (1 / 12) - 1
    series = [value]
    for year in range(1, years + 1):
        annual_contribution = monthly_contribution * 12 * (
            (1 + contribution_growth_pct / 100) ** (year - 1)
        )
        monthly = annual_contribution / 12
        for _ in range(12):
            value = value * (1 + monthly_rate) + monthly
        series.append(value)
    return series

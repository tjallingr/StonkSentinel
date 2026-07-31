KIND_TO_CATEGORY = {
    "checking": "Cash",
    "brokerage": "Cash",
    "savings": "Savings",
    "vehicle": "Other",
    "property": "Other",
    "other": "Other",
}


def resolve_balance(
    *, provider: str, external_id: str, kind: str,
    manual_assets_by_key: dict[str, dict],
    account_overrides: dict[str, dict],
) -> tuple[str, float, float]:
    """Category, yield_pct, monthly_contribution for one non-position account.
    Manual assets carry their own fields directly in [[asset]]; everything else
    is resolved from [account_override."provider:external_id"], falling back to
    a kind -> category default with yield/contribution at 0."""
    if provider == "manual":
        asset = manual_assets_by_key.get(external_id, {})
        category = asset.get("category") or KIND_TO_CATEGORY.get(kind, "Other")
        yield_pct = float(asset.get("expected_real_return_pct", 0.0))
        contribution = float(asset.get("monthly_contribution", 0.0))
        return category, yield_pct, contribution

    override = account_overrides.get(f"{provider}:{external_id}", {})
    category = override.get("category") or KIND_TO_CATEGORY.get(kind, "Other")
    yield_pct = float(override.get("yield_pct", 0.0))
    contribution = float(override.get("monthly_contribution", 0.0))
    return category, yield_pct, contribution

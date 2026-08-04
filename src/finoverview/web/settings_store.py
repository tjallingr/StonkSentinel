from pathlib import Path

import tomlkit


def _category_table(c: dict) -> tomlkit.items.Table:
    t = tomlkit.table()
    t["key"] = c["key"]
    t["label"] = c["label"]
    t["expected_real_return_pct"] = c["expected_real_return_pct"]
    if c.get("bucket"):
        t["bucket"] = c["bucket"]
    if c.get("monthly_contribution"):
        t["monthly_contribution"] = c["monthly_contribution"]
    t["symbols"] = c["symbols"]
    return t


def save_categories(config_dir: Path, categories: list[dict]) -> None:
    path = config_dir / "assets.toml"
    doc = tomlkit.parse(path.read_text())
    aot = tomlkit.aot()
    for c in categories:
        aot.append(_category_table(c))
    doc["saxo_projection_category"] = aot
    path.write_text(tomlkit.dumps(doc))


def parse_categories_form(form) -> list[dict]:
    # Same hazard as parse_assets_form: an empty body must not erase the table.
    if "key" not in form:
        raise ValueError("empty form submission — nothing saved")
    keys = form.getlist("key")
    labels = form.getlist("label")
    rates = form.getlist("rate")
    buckets = form.getlist("bucket")
    contributions = form.getlist("contribution")
    symbols = form.getlist("symbols")
    categories = []
    for key, label, rate, bucket, contribution, syms in zip(
        keys, labels, rates, buckets, contributions, symbols
    ):
        key = key.strip()
        if not key:
            continue
        category = {
            "key": key,
            "label": label.strip() or key,
            "expected_real_return_pct": float(rate),
            "symbols": [s.strip() for s in syms.split(",") if s.strip()],
        }
        if bucket.strip():
            category["bucket"] = bucket.strip()
        if contribution.strip():
            category["monthly_contribution"] = float(contribution)
        categories.append(category)
    return categories


def save_projection(config_dir: Path, values: dict) -> None:
    """Patch only the given keys in [projection], leaving the rest of the table and
    the file's comments where they are."""
    path = config_dir / "assets.toml"
    doc = tomlkit.parse(path.read_text())
    projection = doc.get("projection")
    if projection is None:
        projection = tomlkit.table()
        doc["projection"] = projection
    for key, value in values.items():
        projection[key] = value
    path.write_text(tomlkit.dumps(doc))


def parse_projection_form(form) -> dict:
    """Horizon and inflation. Inflation is optional in the form so the settings page
    and the projection page can each submit only the fields they show."""
    out: dict[str, int | float] = {}
    if "years" in form:
        years = int((form.get("years") or "").strip())
        if years <= 0:
            raise ValueError("horizon must be a positive number of years")
        out["years"] = years
    if "inflation" in form and (form.get("inflation") or "").strip():
        inflation = float(form.get("inflation").strip())
        if not -20.0 <= inflation <= 100.0:
            raise ValueError("inflation must be between -20 and 100 percent")
        out["inflation_pct"] = inflation
    if not out:
        raise ValueError("empty form submission — nothing saved")
    return out


def _asset_table(a: dict) -> tomlkit.items.Table:
    t = tomlkit.table()
    t["key"] = a["key"]
    t["label"] = a["label"]
    t["kind"] = a["kind"]
    t["value"] = a["value"]
    t["currency"] = a["currency"]
    t["liquid"] = a["liquid"]
    if a.get("iban"):
        t["iban"] = a["iban"]
    if a.get("as_of"):
        t["as_of"] = a["as_of"]
    if a.get("expected_real_return_pct") is not None:
        t["expected_real_return_pct"] = a["expected_real_return_pct"]
    if a.get("category"):
        t["category"] = a["category"]
    if a.get("monthly_contribution"):
        t["monthly_contribution"] = a["monthly_contribution"]
    return t


def save_assets(config_dir: Path, assets: list[dict]) -> None:
    path = config_dir / "assets.toml"
    doc = tomlkit.parse(path.read_text())
    aot = tomlkit.aot()
    for a in assets:
        aot.append(_asset_table(a))
    doc["asset"] = aot
    path.write_text(tomlkit.dumps(doc))


def parse_assets_form(form) -> list[dict]:
    # save_assets replaces the whole [[asset]] table, so a request that arrives with
    # no fields at all would silently delete every asset. That is exactly what a
    # truncated POST from a phone on a flaky link looks like, so refuse it: the
    # caller turns this into a visible error instead of a wiped config.
    if "key" not in form:
        raise ValueError("empty form submission — nothing saved")
    keys = form.getlist("key")
    labels = form.getlist("label")
    kinds = form.getlist("kind")
    values = form.getlist("value")
    currencies = form.getlist("currency")
    liquids = form.getlist("liquid")
    # Hidden field: not editable here, but it must survive a save.
    ibans = form.getlist("iban")
    as_ofs = form.getlist("as_of")
    yields_ = form.getlist("yield")
    categories = form.getlist("category")
    contributions = form.getlist("contribution")

    assets = []
    for key, label, kind, value, currency, liquid, iban, as_of, yld, category, contribution in zip(
        keys, labels, kinds, values, currencies, liquids, ibans, as_ofs, yields_,
        categories, contributions
    ):
        key = key.strip()
        if not key:
            continue
        asset = {
            "key": key,
            "label": label.strip() or key,
            "kind": kind.strip() or "other",
            "value": float(value),
            "currency": (currency.strip() or "EUR").upper(),
            "liquid": liquid == "true",
        }
        if iban.strip():
            asset["iban"] = iban.replace(" ", "").upper()
        if as_of.strip():
            asset["as_of"] = as_of.strip()
        if yld.strip():
            asset["expected_real_return_pct"] = float(yld)
        if category.strip():
            asset["category"] = category.strip()
        if contribution.strip():
            asset["monthly_contribution"] = float(contribution)
        assets.append(asset)
    return assets

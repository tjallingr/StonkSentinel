import json
from pathlib import Path

MARKER = "[[saxo_projection_category]]"
ASSET_MARKER = "[[asset]]"


def serialize_categories(categories: list[dict]) -> str:
    blocks = []
    for c in categories:
        symbols = ", ".join(json.dumps(s) for s in c["symbols"])
        blocks.append(
            f"{MARKER}\n"
            f'key                      = {json.dumps(c["key"])}\n'
            f'label                    = {json.dumps(c["label"])}\n'
            f'expected_real_return_pct = {c["expected_real_return_pct"]}\n'
            f"symbols                  = [{symbols}]\n"
        )
    return "\n".join(blocks) + "\n"


def save_categories(config_dir: Path, categories: list[dict]) -> None:
    path = config_dir / "assets.toml"
    text = path.read_text()
    head = text.split(MARKER)[0].rstrip("\n")
    path.write_text(f"{head}\n\n\n{serialize_categories(categories)}")


def parse_categories_form(form) -> list[dict]:
    keys = form.getlist("key")
    labels = form.getlist("label")
    rates = form.getlist("rate")
    symbols = form.getlist("symbols")
    categories = []
    for key, label, rate, syms in zip(keys, labels, rates, symbols):
        key = key.strip()
        if not key:
            continue
        categories.append({
            "key": key,
            "label": label.strip() or key,
            "expected_real_return_pct": float(rate),
            "symbols": [s.strip() for s in syms.split(",") if s.strip()],
        })
    return categories


def serialize_assets(assets: list[dict]) -> str:
    blocks = []
    for a in assets:
        lines = [
            ASSET_MARKER,
            f'key      = {json.dumps(a["key"])}',
            f'label    = {json.dumps(a["label"])}',
            f'kind     = {json.dumps(a["kind"])}',
            f'value    = {a["value"]}',
            f'currency = {json.dumps(a["currency"])}',
            f'liquid   = {"true" if a["liquid"] else "false"}',
        ]
        if a.get("as_of"):
            lines.append(f'as_of    = {json.dumps(a["as_of"])}')
        if a.get("expected_real_return_pct") is not None:
            lines.append(f'expected_real_return_pct = {a["expected_real_return_pct"]}')
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks) + "\n"


def save_assets(config_dir: Path, assets: list[dict]) -> None:
    path = config_dir / "assets.toml"
    text = path.read_text()
    lines = text.splitlines(keepends=True)

    start = next((i for i, line in enumerate(lines) if line.strip() == ASSET_MARKER), len(lines))
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("[") and lines[i].strip() != ASSET_MARKER:
            end = i
            break

    before = "".join(lines[:start]).rstrip("\n")
    after = "".join(lines[end:])
    new_text = (before + "\n\n" if before else "") + serialize_assets(assets)
    if after:
        new_text = new_text.rstrip("\n") + "\n\n\n" + after
    path.write_text(new_text)


def parse_assets_form(form) -> list[dict]:
    keys = form.getlist("key")
    labels = form.getlist("label")
    kinds = form.getlist("kind")
    values = form.getlist("value")
    currencies = form.getlist("currency")
    liquids = form.getlist("liquid")
    as_ofs = form.getlist("as_of")
    yields_ = form.getlist("yield")

    assets = []
    for key, label, kind, value, currency, liquid, as_of, yld in zip(
        keys, labels, kinds, values, currencies, liquids, as_ofs, yields_
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
        if as_of.strip():
            asset["as_of"] = as_of.strip()
        if yld.strip():
            asset["expected_real_return_pct"] = float(yld)
        assets.append(asset)
    return assets
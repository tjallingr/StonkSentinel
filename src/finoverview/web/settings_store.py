import json
from pathlib import Path

MARKER = "[[saxo_projection_category]]"


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
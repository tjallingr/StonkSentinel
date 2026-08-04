"""Server-rendered SVG charts.

A charting library would mean a CDN request or a build step, and neither earns its
keep for three charts on a LAN dashboard. These emit plain SVG that scales, prints,
and works with JavaScript off.

Colours come from CSS custom properties so the charts follow the light/dark theme
without re-rendering.
"""

from __future__ import annotations

import math
from html import escape

PAD_L, PAD_R, PAD_T, PAD_B = 8, 8, 10, 18

CLASS_COLOR_VAR = {
    "Equity": "--cat-equity",
    "Bonds": "--cat-bonds",
    "Cash": "--cat-cash",
    "Savings": "--cat-savings",
    "Other": "--cat-other",
}


def _nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / count
    mag = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 0.1
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw:
            break
    start = (lo // step) * step
    ticks = []
    v = start
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.01:
            ticks.append(round(v, 6))
        v += step
    return ticks


def _fmt_short(v: float) -> str:
    """Axis labels. Compact enough not to collide at small chart widths."""
    a = abs(v)
    if a >= 999_500:
        return f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if a >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:.0f}"


def area_chart(series: list[dict], width: int = 1000, height: int = 200) -> str:
    """Net worth over time. Filled area plus a baseline."""
    if len(series) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
            f'aria-label="Not enough history yet">'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" '
            f'class="chart-empty">Not enough history yet — run the collectors '
            f'for a few days</text></svg>'
        )

    values = [p["value"] for p in series]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1
    span = hi - lo
    lo -= span * 0.08
    hi += span * 0.08

    iw = width - PAD_L - PAD_R
    ih = height - PAD_T - PAD_B
    n = len(series)

    def x(i: int) -> float:
        return PAD_L + (iw * i / (n - 1))

    def y(v: float) -> float:
        return PAD_T + ih - (ih * (v - lo) / (hi - lo))

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{PAD_L},{PAD_T + ih:.1f} {line} {PAD_L + iw:.1f},{PAD_T + ih:.1f}"

    grid = []
    for t in _nice_ticks(lo, hi):
        gy = y(t)
        if not (PAD_T <= gy <= PAD_T + ih):
            continue
        grid.append(f'<line x1="{PAD_L}" y1="{gy:.1f}" x2="{PAD_L + iw}" y2="{gy:.1f}" '
                    f'class="grid"/>')
        grid.append(f'<text x="{PAD_L + 2}" y="{gy - 3:.1f}" class="tick">'
                    f'{escape(_fmt_short(t))}</text>')

    first, last = series[0]["date"], series[-1]["date"]
    rising = values[-1] >= values[0]
    cls = "up" if rising else "down"

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none" '
        f'role="img" aria-label="Net worth from {first} to {last}">'
        f'{"".join(grid)}'
        f'<polygon points="{area}" class="area {cls}"/>'
        f'<polyline points="{line}" class="line {cls}"/>'
        f'<text x="{PAD_L}" y="{height - 4}" class="tick">{escape(first)}</text>'
        f'<text x="{PAD_L + iw}" y="{height - 4}" text-anchor="end" class="tick">'
        f'{escape(last)}</text>'
        f'</svg>'
    )


def band_chart(bands: list[dict], deterministic: list[dict],
               width: int = 1000, height: int = 260) -> str:
    """Monte Carlo percentile fan plus the deterministic path."""
    if len(bands) < 2:
        return '<svg viewBox="0 0 10 10" class="chart"></svg>'

    hi = max(b["p90"] for b in bands)
    lo = 0.0
    iw = width - PAD_L - PAD_R - 34
    ih = height - PAD_T - PAD_B
    n = len(bands)

    def x(i: int) -> float:
        return PAD_L + (iw * i / (n - 1))

    def y(v: float) -> float:
        return PAD_T + ih - (ih * (v - lo) / (hi - lo)) if hi > lo else PAD_T + ih

    def poly(upper: str, lower: str) -> str:
        up = " ".join(f"{x(i):.1f},{y(b[upper]):.1f}" for i, b in enumerate(bands))
        down = " ".join(
            f"{x(i):.1f},{y(b[lower]):.1f}" for i, b in reversed(list(enumerate(bands)))
        )
        return f"{up} {down}"

    grid = []
    for t in _nice_ticks(lo, hi):
        gy = y(t)
        if not (PAD_T <= gy <= PAD_T + ih):
            continue
        grid.append(f'<line x1="{PAD_L}" y1="{gy:.1f}" x2="{PAD_L + iw}" y2="{gy:.1f}" '
                    f'class="grid"/>')
        grid.append(f'<text x="{PAD_L + iw + 4}" y="{gy + 3:.1f}" class="tick">'
                    f'{escape(_fmt_short(t))}</text>')

    median = " ".join(f"{x(i):.1f},{y(b['p50']):.1f}" for i, b in enumerate(bands))
    det_map = {d["year"]: d["value"] for d in deterministic}
    det = " ".join(
        f"{x(i):.1f},{y(det_map.get(b['year'], b['p50'])):.1f}"
        for i, b in enumerate(bands)
    )

    xlabels = []
    step = max(1, (n - 1) // 6)
    for i in range(0, n, step):
        xlabels.append(
            f'<text x="{x(i):.1f}" y="{height - 4}" text-anchor="middle" class="tick">'
            f'{bands[i]["year"]}y</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" preserveAspectRatio="none" '
        f'role="img" aria-label="Projected value percentile bands">'
        f'{"".join(grid)}'
        f'<polygon points="{poly("p90", "p10")}" class="band outer"/>'
        f'<polygon points="{poly("p75", "p25")}" class="band inner"/>'
        f'<polyline points="{median}" class="line median"/>'
        f'<polyline points="{det}" class="line deterministic"/>'
        f'{"".join(xlabels)}'
        f'</svg>'
    )


def _column_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """A column with a rounded cap and square feet. Rounding only the data end keeps
    the baseline reading as a single hard line across every column."""
    r = min(r, w / 2, h)
    if r <= 0:
        return f"M {x:.1f} {y:.1f} h {w:.1f} v {h:.1f} h {-w:.1f} Z"
    return (
        f"M {x:.1f} {y + h:.1f} "
        f"L {x:.1f} {y + r:.1f} "
        f"Q {x:.1f} {y:.1f} {x + r:.1f} {y:.1f} "
        f"L {x + w - r:.1f} {y:.1f} "
        f"Q {x + w:.1f} {y:.1f} {x + w:.1f} {y + r:.1f} "
        f"L {x + w:.1f} {y + h:.1f} Z"
    )


def column_chart(rows: list[dict], width: int = 1040, height: int = 190,
                 average: float | None = None, currency: str = "") -> str:
    """Monthly spend. Columns, not an area: monthly totals are discrete buckets and
    joining them with a slope implies a continuous quantity that doesn't exist.

    One series, so one colour and no legend — the heading says what is plotted. The
    current month is dimmed because it is still filling up, and comparing a
    part-month column against complete ones is the mistake this chart invites.

    No preserveAspectRatio override here (unlike the line charts): stretching would
    skew the rounded caps and eat the 2px gaps that separate the columns.
    """
    if not rows:
        return (f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
                f'aria-label="No transactions yet"><text x="{width/2}" y="{height/2}" '
                f'text-anchor="middle" class="chart-empty">No transactions yet — run '
                f'the enablebanking collector</text></svg>')

    values = [r["spend"] for r in rows]
    hi = max(max(values), average or 0) or 1
    ticks = _nice_ticks(0, hi)
    hi = max(hi, ticks[-1] if ticks else hi)

    pad_r = 40                      # room for the right-hand y tick labels
    iw = width - PAD_L - pad_r
    ih = height - PAD_T - PAD_B
    n = len(rows)
    band = iw / n
    # Cap the mark and let the leftover be air; the 2px is the surface gap.
    bar_w = min(24.0, max(3.0, band - 2))

    def y(v: float) -> float:
        return PAD_T + ih - (ih * v / hi)

    grid = []
    for t in ticks:
        gy = y(t)
        if not (PAD_T - 1 <= gy <= PAD_T + ih + 1):
            continue
        grid.append(f'<line x1="{PAD_L}" y1="{gy:.1f}" x2="{PAD_L + iw:.1f}" '
                    f'y2="{gy:.1f}" class="grid"/>')
        grid.append(f'<text x="{PAD_L + iw + 5:.1f}" y="{gy + 3.5:.1f}" class="tick">'
                    f'{escape(_fmt_short(t))}</text>')

    peak = max(range(n), key=lambda i: values[i])
    cols, labels = [], []
    for i, r in enumerate(rows):
        v = r["spend"]
        x = PAD_L + band * i + (band - bar_w) / 2
        h = max(0.0, ih - (y(v) - PAD_T))
        partial = r.get("partial")
        cls = "col partial" if partial else "col"
        tip = (f'{r["month"]} — {v:,.2f}{(" " + currency) if currency else ""}'
               f' · {r.get("count", 0)} payments'
               f'{" · month still in progress" if partial else ""}')
        cols.append(
            f'<path d="{_column_path(x, y(v), bar_w, h)}" class="{cls}">'
            f'<title>{escape(tip)}</title></path>'
        )
        # Label the peak only. A number on every column goes unread.
        if i == peak and v:
            labels.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y(v) - 5:.1f}" text-anchor="middle" '
                f'class="col-label">{escape(_fmt_short(v))}</text>'
            )

    ref = ""
    if average:
        ay = y(average)
        ref = (f'<line x1="{PAD_L}" y1="{ay:.1f}" x2="{PAD_L + iw:.1f}" y2="{ay:.1f}" '
               f'class="line ref"/>')

    # Thin out month labels until they stop colliding.
    step = max(1, math.ceil(n / 12))
    xlabels = [
        f'<text x="{PAD_L + band * i + band / 2:.1f}" y="{height - 4}" '
        f'text-anchor="middle" class="tick">{escape(rows[i]["month"][2:])}</text>'
        for i in range(n) if i % step == 0 or i == n - 1
    ]

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
        f'aria-label="Spend per month from {escape(rows[0]["month"])} to '
        f'{escape(rows[-1]["month"])}">'
        f'{"".join(grid)}{ref}{"".join(cols)}{"".join(labels)}{"".join(xlabels)}'
        f'</svg>'
    )


def _point(cx: float, cy: float, r: float, angle_deg: float) -> tuple[float, float]:
    a = math.radians(angle_deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def _wedge_path(cx: float, cy: float, r_outer: float, r_inner: float,
                theta1: float, theta2: float) -> str:
    large_arc = 1 if (theta2 - theta1) > 180 else 0
    x1o, y1o = _point(cx, cy, r_outer, theta1)
    x2o, y2o = _point(cx, cy, r_outer, theta2)
    x1i, y1i = _point(cx, cy, r_inner, theta1)
    x2i, y2i = _point(cx, cy, r_inner, theta2)
    return (
        f"M {x1o:.2f} {y1o:.2f} "
        f"A {r_outer:.2f} {r_outer:.2f} 0 {large_arc} 1 {x2o:.2f} {y2o:.2f} "
        f"L {x2i:.2f} {y2i:.2f} "
        f"A {r_inner:.2f} {r_inner:.2f} 0 {large_arc} 0 {x1i:.2f} {y1i:.2f} Z"
    )


def pie_chart(rows: list[dict], width: int = 200, height: int = 200) -> str:
    """Donut of a small fixed category set. Wedges render in the caller's given
    order, never re-sorted by value — the color-to-category mapping is fixed, and
    resorting would also shuffle which categories sit next to each other, which is
    the one thing validated against the palette's contrast checks."""
    if not rows:
        return (f'<svg viewBox="0 0 {width} {height}" class="chart pie" role="img" '
                f'aria-label="No holdings yet"></svg>')

    cx, cy = width / 2, height / 2
    r_outer = min(width, height) / 2 - 4
    r_inner = r_outer * 0.6
    total = sum(r["value"] for r in rows) or 1

    angle = 0.0
    wedges = []
    for r in rows:
        sweep = r["value"] / total * 360
        color = f"var({CLASS_COLOR_VAR.get(r['label'], '--faint')})"
        path = _wedge_path(cx, cy, r_outer, r_inner, angle, angle + sweep)
        wedges.append(
            f'<path d="{path}" fill="{color}" stroke="var(--surface)" stroke-width="2">'
            f'<title>{escape(r["label"])} — {r["pct"]:.1f}%</title></path>'
        )
        angle += sweep

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart pie" role="img" '
        f'aria-label="Composition by class">'
        + "".join(wedges) +
        f'<text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="middle" '
        f'class="pie-total">{total:,.0f}</text>'
        f'</svg>'
    )


def bar_rows(rows: list[dict], max_rows: int = 8) -> list[dict]:
    """Allocation rows with a width percentage for a pure-CSS bar."""
    shown = rows[:max_rows]
    top = max((r["pct"] for r in shown), default=1) or 1
    out = [{**r, "width": r["pct"] / top * 100} for r in shown]
    if len(rows) > max_rows:
        rest = rows[max_rows:]
        total = sum(r["pct"] for r in rest)
        out.append({"label": f"{len(rest)} more", "value": sum(r["value"] for r in rest),
                    "pct": total, "width": total / top * 100})
    return out

"""Server-rendered SVG charts.

A charting library would mean a CDN request or a build step, and neither earns its
keep for three charts on a LAN dashboard. These emit plain SVG that scales, prints,
and works with JavaScript off.

Colours come from CSS custom properties so the charts follow the light/dark theme
without re-rendering.
"""

from __future__ import annotations

from html import escape

PAD_L, PAD_R, PAD_T, PAD_B = 8, 8, 10, 18


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

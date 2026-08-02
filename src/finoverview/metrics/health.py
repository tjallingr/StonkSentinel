"""Collector freshness and bank consent expiry checks."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

DEFAULT_STALE_HOURS = {
    "saxo": 8.0,
    "enablebanking": 30.0,
    "manual": 24.0 * 40,   # only changes when you edit the TOML
    "fx": 30.0,
}

CONSENT_WARN_DAYS = 14


def _age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def humanise(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def collectors(conn: sqlite3.Connection, thresholds: dict[str, float] | None = None) -> list[dict]:
    limits = {**DEFAULT_STALE_HOURS, **(thresholds or {})}
    rows = conn.execute("SELECT * FROM collector_health ORDER BY collector").fetchall()
    seen = {r["collector"] for r in rows}

    out = []
    for r in rows:
        age = _age_hours(r["finished_at"] or r["started_at"])
        limit = limits.get(r["collector"], 24.0)
        if r["status"] == "error":
            status = "error"
        elif r["status"] == "running" and (age or 0) > 1:
            status = "error"        # a run stuck for over an hour is a failure
        elif age is None or age > limit:
            status = "stale"
        else:
            status = "ok"
        out.append({
            "name": r["collector"],
            "status": status,
            "age_hours": age,
            "age": humanise(age),
            "rows": r["rows_written"],
            "error": r["error"],
            "limit_hours": limit,
        })

    # A collector that has never run at all is the easiest failure to miss.
    for name in limits:
        if name not in seen:
            out.append({"name": name, "status": "never", "age_hours": None,
                        "age": "never", "rows": 0, "error": None,
                        "limit_hours": limits[name]})
    return sorted(out, key=lambda c: c["name"])


def consents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM provider_sessions ORDER BY institution"
    ).fetchall()
    out = []
    for r in rows:
        days = None
        if r["valid_until"]:
            try:
                dt = datetime.fromisoformat(r["valid_until"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
            except ValueError:
                days = None
        if days is None:
            status = "unknown"
        elif days <= 0:
            status = "expired"
        elif days <= CONSENT_WARN_DAYS:
            status = "expiring"
        else:
            status = "ok"
        out.append({
            "provider": r["provider"],
            "institution": r["institution"],
            "valid_until": r["valid_until"],
            "days_left": days,
            "status": status,
        })
    return out


def overall(collector_rows: list[dict], consent_rows: list[dict]) -> str:
    if any(c["status"] in ("error", "never") for c in collector_rows):
        return "error"
    if any(c["status"] == "expired" for c in consent_rows):
        return "error"
    if any(c["status"] == "stale" for c in collector_rows):
        return "stale"
    if any(c["status"] == "expiring" for c in consent_rows):
        return "warn"
    return "ok"

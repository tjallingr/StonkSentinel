"""SQLite access — append-only snapshots, plain sqlite3."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_minor(amount: float | int | str, exponent: int = 2) -> int:
    """Money -> integer minor units. Uses Decimal to avoid float drift."""
    from decimal import Decimal, ROUND_HALF_UP

    q = Decimal(str(amount)).quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_UP)
    return int(q.scaleb(exponent))


def from_minor(minor: int, exponent: int = 2) -> float:
    return minor / (10**exponent)


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def upsert_account(
    conn: sqlite3.Connection,
    *,
    provider: str,
    external_id: str,
    label: str,
    kind: str,
    currency: str,
    institution: str | None = None,
    liquid: bool = True,
    encumbered: bool = False,
    include_in_networth: bool = True,
) -> int:
    """Insert or update account metadata; returns the local account id.

    Note: label/kind/flags are updated on conflict so config changes propagate,
    but external_id is the stable key. Snapshots are never touched.
    """
    conn.execute(
        """
        INSERT INTO accounts (provider, external_id, institution, label, kind, currency,
                              liquid, encumbered, include_in_networth, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (provider, external_id) DO UPDATE SET
            institution = excluded.institution,
            label = excluded.label,
            kind = excluded.kind,
            currency = excluded.currency,
            liquid = excluded.liquid,
            encumbered = excluded.encumbered,
            include_in_networth = excluded.include_in_networth
        """,
        (provider, external_id, institution, label, kind, currency,
         int(liquid), int(encumbered), int(include_in_networth), utcnow()),
    )
    row = conn.execute(
        "SELECT id FROM accounts WHERE provider = ? AND external_id = ?",
        (provider, external_id),
    ).fetchone()
    return int(row["id"])


def insert_balance(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    ts: str,
    balance_minor: int,
    currency: str,
    as_of: str | None = None,
    balance_type: str = "default",
    run_id: int | None = None,
) -> bool:
    """Append a balance snapshot. Returns False if this exact snapshot already exists."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO balance_snapshots
            (account_id, ts, as_of, balance_minor, currency, balance_type, run_id)
        VALUES (?,?,?,?,?,?,?)
        """,
        (account_id, ts, as_of, balance_minor, currency, balance_type, run_id),
    )
    return cur.rowcount > 0


def insert_position(conn: sqlite3.Connection, run_id: int | None = None, **kw: Any) -> bool:
    cols = ("account_id", "ts", "instrument_id", "symbol", "isin", "name", "asset_class",
            "quantity", "avg_open_price", "last_price", "market_value_minor", "currency",
            "unrealized_pl_minor", "exchange", "country")
    values = [kw.get(c) for c in cols] + [run_id]
    placeholders = ",".join("?" * (len(cols) + 1))
    cur = conn.execute(
        f"INSERT OR IGNORE INTO position_snapshots ({','.join(cols)},run_id) "
        f"VALUES ({placeholders})",
        values,
    )
    return cur.rowcount > 0


def upsert_cash_flow(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    ts: str,
    amount_minor: int,
    currency: str,
    kind: str,
    external_id: str | None = None,
    note: str | None = None,
) -> bool:
    cur = conn.execute(
        """
        INSERT INTO portfolio_cash_flows
            (account_id, ts, amount_minor, currency, kind, external_id, note)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT (account_id, external_id) DO NOTHING
        """,
        (account_id, ts, amount_minor, currency, kind, external_id, note),
    )
    return cur.rowcount > 0


def insert_fx(conn: sqlite3.Connection, as_of: str, base: str, quote: str, rate: float) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO fx_rates (as_of, base, quote, rate) VALUES (?,?,?,?)",
        (as_of, base, quote, rate),
    )
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# collector run bookkeeping
# --------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, collector: str) -> int:
    cur = conn.execute(
        "INSERT INTO collector_runs (collector, started_at, status) VALUES (?,?,'running')",
        (collector, utcnow()),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, rows: int = 0,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE collector_runs SET finished_at = ?, status = ?, rows_written = ?, error = ? "
        "WHERE id = ?",
        (utcnow(), "error" if error else "ok", rows, error, run_id),
    )


def prune_runs(conn: sqlite3.Connection, keep: int = 500) -> None:
    """Run history is the only non-append-only table that grows without bound."""
    conn.execute(
        "DELETE FROM collector_runs WHERE id NOT IN "
        "(SELECT id FROM collector_runs ORDER BY id DESC LIMIT ?)",
        (keep,),
    )


# --------------------------------------------------------------------------
# secrets / sessions
# --------------------------------------------------------------------------

def save_tokens(conn: sqlite3.Connection, provider: str, *, access_token: str | None,
                refresh_token: str | None, access_expires_at: str | None,
                refresh_expires_at: str | None) -> None:
    conn.execute(
        """
        INSERT INTO oauth_tokens (provider, access_token, refresh_token,
                                  access_expires_at, refresh_expires_at, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT (provider) DO UPDATE SET
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            access_expires_at = excluded.access_expires_at,
            refresh_expires_at = excluded.refresh_expires_at,
            updated_at = excluded.updated_at
        """,
        (provider, access_token, refresh_token, access_expires_at,
         refresh_expires_at, utcnow()),
    )


def load_tokens(conn: sqlite3.Connection, provider: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM oauth_tokens WHERE provider = ?", (provider,)
    ).fetchone()


def save_session(conn: sqlite3.Connection, provider: str, institution: str,
                 session_id: str, valid_until: str | None) -> None:
    conn.execute(
        """
        INSERT INTO provider_sessions (provider, institution, session_id, valid_until, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT (provider, institution) DO UPDATE SET
            session_id = excluded.session_id,
            valid_until = excluded.valid_until,
            created_at = excluded.created_at
        """,
        (provider, institution, session_id, valid_until, utcnow()),
    )


def load_sessions(conn: sqlite3.Connection, provider: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM provider_sessions WHERE provider = ? ORDER BY institution",
        (provider,),
    ).fetchall()

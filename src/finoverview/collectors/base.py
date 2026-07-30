"""Collector base. Every collector writes into the same snapshot schema and
records its own run so the dashboard can show per-source freshness."""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod

from .. import db
from ..config import AssetsConfig, Settings

log = logging.getLogger(__name__)


class Collector(ABC):
    name: str = "base"

    def __init__(self, conn: sqlite3.Connection, settings: Settings,
                 assets: AssetsConfig | None = None) -> None:
        self.conn = conn
        self.settings = settings
        self.assets = assets

    @abstractmethod
    def collect(self, run_id: int) -> int:
        """Fetch and write snapshots. Return the number of rows written."""

    def run(self) -> int:
        run_id = db.start_run(self.conn, self.name)
        try:
            rows = self.collect(run_id)
        except Exception as exc:  # noqa: BLE001 - we want the message in the DB
            log.exception("%s failed", self.name)
            db.finish_run(self.conn, run_id, error=f"{type(exc).__name__}: {exc}"[:2000])
            raise
        db.finish_run(self.conn, run_id, rows=rows)
        db.prune_runs(self.conn)
        log.info("%s wrote %d rows", self.name, rows)
        return rows

    def apply_overrides(self, provider: str, external_id: str, defaults: dict) -> dict:
        """Merge [account_override."provider:id"] from assets.toml over collected
        account metadata. This is how you flag encumbered / illiquid accounts."""
        if not self.assets:
            return defaults
        override = self.assets.account_overrides.get(f"{provider}:{external_id}", {})
        merged = dict(defaults)
        for key in ("label", "kind", "liquid", "encumbered", "include_in_networth"):
            if key in override:
                merged[key] = override[key]
        return merged

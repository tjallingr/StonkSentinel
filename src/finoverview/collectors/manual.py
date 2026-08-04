"""Manual assets, recurring items and cash flows from config/assets.toml."""

from __future__ import annotations

import logging

from .. import db
from .base import Collector

log = logging.getLogger(__name__)

PERIODS = {"monthly", "quarterly", "yearly"}


class ManualCollector(Collector):
    name = "manual"

    def collect(self, run_id: int) -> int:
        if self.assets is None:
            raise RuntimeError("ManualCollector needs assets config")

        ts = db.utcnow()
        rows = 0

        # --- assets ------------------------------------------------------
        for asset in self.assets.assets:
            required = ("key", "label", "value", "currency")
            missing = [k for k in required if k not in asset]
            if missing:
                raise ValueError(f"[[asset]] missing keys {missing}: {asset}")

            account_id = db.upsert_account(
                self.conn,
                provider="manual",
                external_id=asset["key"],
                institution=asset.get("institution", "Manual"),
                # Optional. Giving a manual account its IBAN is what stops transfers
                # into it being counted as expenses — see the expenses view.
                iban=(asset.get("iban") or "").replace(" ", "").upper() or None,
                label=asset["label"],
                kind=asset.get("kind", "other"),
                currency=asset["currency"].upper(),
                liquid=bool(asset.get("liquid", False)),
                encumbered=bool(asset.get("encumbered", False)),
                include_in_networth=bool(asset.get("include_in_networth", True)),
            )
            if db.insert_balance(
                self.conn,
                account_id=account_id,
                ts=ts,
                as_of=asset.get("as_of"),
                balance_minor=db.to_minor(asset["value"]),
                currency=asset["currency"].upper(),
                balance_type="manual",
                run_id=run_id,
            ):
                rows += 1

        # --- recurring costs / incomes -----------------------------------
        seen: set[str] = set()
        for item in self.assets.recurring:
            for k in ("key", "label", "kind", "amount", "currency", "period"):
                if k not in item:
                    raise ValueError(f"[[recurring]] missing '{k}': {item}")
            if item["kind"] not in ("income", "cost"):
                raise ValueError(f"[[recurring]] kind must be income|cost: {item}")
            if item["period"] not in PERIODS:
                raise ValueError(f"[[recurring]] period must be one of {PERIODS}: {item}")

            seen.add(item["key"])
            self.conn.execute(
                """
                INSERT INTO recurring (key, label, kind, amount_minor, currency,
                                       period, category, active, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT (key) DO UPDATE SET
                    label = excluded.label, kind = excluded.kind,
                    amount_minor = excluded.amount_minor, currency = excluded.currency,
                    period = excluded.period, category = excluded.category,
                    active = excluded.active, updated_at = excluded.updated_at
                """,
                (item["key"], item["label"], item["kind"], db.to_minor(item["amount"]),
                 item["currency"].upper(), item["period"], item.get("category"),
                 int(item.get("active", True)), ts),
            )
            rows += 1

        # Deactivate rows removed from the TOML rather than deleting them.
        if seen:
            placeholders = ",".join("?" * len(seen))
            self.conn.execute(
                f"UPDATE recurring SET active = 0, updated_at = ? "
                f"WHERE key NOT IN ({placeholders})",
                [ts, *seen],
            )

        # --- hand-recorded portfolio cash flows ---------------------------
        for flow in self.assets.cash_flows:
            for k in ("account", "date", "amount", "currency", "kind"):
                if k not in flow:
                    raise ValueError(f"[[cash_flow]] missing '{k}': {flow}")
            acct = self.conn.execute(
                "SELECT id FROM accounts WHERE provider || ':' || external_id = ? "
                "OR label = ?",
                (flow["account"], flow["account"]),
            ).fetchone()
            if acct is None:
                log.warning("cash_flow references unknown account %r; run the "
                            "broker collector first", flow["account"])
                continue
            if db.upsert_cash_flow(
                self.conn,
                account_id=int(acct["id"]),
                ts=str(flow["date"]),
                amount_minor=db.to_minor(flow["amount"]),
                currency=flow["currency"].upper(),
                kind=flow["kind"],
                external_id=flow.get("id") or f"manual:{flow['date']}:{flow['amount']}",
                note=flow.get("note"),
            ):
                rows += 1

        return rows

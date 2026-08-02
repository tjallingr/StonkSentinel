"""Saxo OpenAPI collector. Refresh tokens rotate every ~24h — collector must run daily."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import db
from .base import Collector

log = logging.getLogger(__name__)

LIVE_AUTH_BASE = "https://live.logonvalidation.net"
LIVE_API_BASE = "https://gateway.saxobank.com/openapi"
SIM_AUTH_BASE = "https://sim.logonvalidation.net"
SIM_API_BASE = "https://gateway.saxobank.com/sim/openapi"

ASSET_CLASS_MAP = {
    "Stock": "equity",
    "StockIndex": "equity",
    "Etf": "etf",
    "Etc": "etf",
    "Etn": "etf",
    "MutualFund": "fund",
    "Bond": "bond",
    "FxSpot": "fx",
    "FxForwards": "fx",
    "CfdOnStock": "derivative",
    "CfdOnEtf": "derivative",
    "CfdOnIndex": "derivative",
    "CfdOnFutures": "derivative",
    "FuturesOption": "derivative",
    "StockOption": "derivative",
    "ContractFutures": "derivative",
}


class SaxoClient:
    def __init__(self, conn, app_key: str, app_secret: str, *, live: bool = True,
                 timeout: float = 30.0) -> None:
        self.conn = conn
        self.app_key = app_key
        self.app_secret = app_secret
        self.auth_base = LIVE_AUTH_BASE if live else SIM_AUTH_BASE
        self.api_base = LIVE_API_BASE if live else SIM_API_BASE
        self._client = httpx.Client(timeout=timeout)
        self._access_token: str | None = None

    # --- tokens ---------------------------------------------------------
    def ensure_token(self) -> str:
        row = db.load_tokens(self.conn, "saxo")
        if row is None or not row["refresh_token"]:
            raise SaxoAuthError(
                "No stored Saxo refresh token. Run: python -m finoverview.auth.saxo_link"
            )

        # Reuse a still-valid access token to avoid burning refreshes needlessly.
        if row["access_token"] and row["access_expires_at"]:
            expires = datetime.fromisoformat(row["access_expires_at"])
            if expires - timedelta(minutes=2) > datetime.now(timezone.utc):
                self._access_token = row["access_token"]
                return self._access_token

        return self.refresh(row["refresh_token"])

    def refresh(self, refresh_token: str) -> str:
        resp = self._client.post(
            f"{self.auth_base}/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(self.app_key, self.app_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise SaxoAuthError(
                f"Refresh failed ({resp.status_code}): {resp.text[:400]}. "
                "If the refresh token expired (Pi offline >24h), re-run "
                "python -m finoverview.auth.saxo_link"
            )
        data = resp.json()
        now = datetime.now(timezone.utc)
        db.save_tokens(
            self.conn,
            "saxo",
            access_token=data["access_token"],
            # CRITICAL: each refresh rotates the refresh token. Persist the new one.
            refresh_token=data.get("refresh_token", refresh_token),
            access_expires_at=(now + timedelta(seconds=int(data.get("expires_in", 1200)))).isoformat(),
            refresh_expires_at=(
                now + timedelta(seconds=int(data["refresh_token_expires_in"]))
            ).isoformat() if data.get("refresh_token_expires_in") else None,
        )
        self._access_token = data["access_token"]
        return self._access_token

    # --- requests -------------------------------------------------------
    def get(self, path: str, **params: Any) -> dict:
        token = self._access_token or self.ensure_token()
        resp = self._client.get(
            f"{self.api_base}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params={k: v for k, v in params.items() if v is not None} or None,
        )
        if resp.status_code == 401:
            token = self.ensure_token()
            resp = self._client.get(
                f"{self.api_base}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params={k: v for k, v in params.items() if v is not None} or None,
            )
        if resp.status_code >= 400:
            raise SaxoApiError(f"Saxo {resp.status_code} on {path}: {resp.text[:600]}")
        return resp.json() if resp.content else {}

    # --- portfolio ------------------------------------------------------
    def me(self) -> dict:
        return self.get("/port/v1/clients/me")

    def accounts(self) -> list[dict]:
        return self.get("/port/v1/accounts/me").get("Data", [])

    def balances(self, account_key: str | None = None, client_key: str | None = None) -> dict:
        if account_key:
            return self.get("/port/v1/balances", AccountKey=account_key, ClientKey=client_key)
        return self.get("/port/v1/balances/me")

    def net_positions(self) -> list[dict]:
        return self.get(
            "/port/v1/netpositions/me",
            FieldGroups="NetPositionBase,NetPositionView,DisplayAndFormat,ExchangeInfo",
        ).get("Data", [])

    def close(self) -> None:
        self._client.close()


class SaxoAuthError(RuntimeError):
    pass


class SaxoApiError(RuntimeError):
    pass


def _minor(value: Any, currency: str) -> int:
    return db.to_minor(value if value is not None else 0)


class SaxoCollector(Collector):
    name = "saxo"

    def collect(self, run_id: int) -> int:
        cfg = self.settings.saxo
        client = SaxoClient(
            self.conn,
            self.settings.saxo_app_key,
            self.settings.saxo_app_secret,
            live=bool(cfg.get("live", True)),
        )
        rows = 0
        try:
            client.ensure_token()
            me = client.me()
            client_key = me.get("ClientKey")
            accounts = client.accounts()
            ts = db.utcnow()

            account_ids: dict[str, int] = {}
            for acct in accounts:
                key = acct["AccountKey"]
                label = acct.get("AccountId") or acct.get("DisplayName") or "Saxo"
                meta = self.apply_overrides("saxo", key, {
                    "label": f"Saxo {label}",
                    "kind": "brokerage",
                    "liquid": True,
                    "encumbered": False,
                    "include_in_networth": True,
                })
                account_ids[key] = db.upsert_account(
                    self.conn,
                    provider="saxo",
                    external_id=key,
                    institution="Saxo",
                    currency=(acct.get("Currency") or "EUR").upper(),
                    **meta,
                )

            rows += self._write_balances(client, client_key, accounts, account_ids, ts, run_id)
            rows += self._write_positions(client, account_ids, ts, run_id)
        finally:
            client.close()
        return rows

    def _write_balances(self, client: SaxoClient, client_key: str | None,
                        accounts: list[dict], account_ids: dict[str, int],
                        ts: str, run_id: int) -> int:
        rows = 0
        for acct in accounts:
            key = acct["AccountKey"]
            try:
                bal = client.balances(account_key=key, client_key=client_key)
            except SaxoApiError as exc:
                log.warning("balance fetch failed for %s: %s", key, exc)
                continue

            currency = (bal.get("Currency") or acct.get("Currency") or "EUR").upper()
            # TotalValue = cash + market value of positions. This is the account's
            # contribution to net worth.
            total = bal.get("TotalValue")
            cash = bal.get("CashBalance")

            if total is not None and db.insert_balance(
                self.conn, account_id=account_ids[key], ts=ts,
                balance_minor=_minor(total, currency), currency=currency,
                balance_type="TotalValue", run_id=run_id,
            ):
                rows += 1
            if cash is not None and db.insert_balance(
                self.conn, account_id=account_ids[key], ts=ts,
                balance_minor=_minor(cash, currency), currency=currency,
                balance_type="CashBalance", run_id=run_id,
            ):
                rows += 1
        return rows

    def _write_positions(self, client: SaxoClient, account_ids: dict[str, int],
                         ts: str, run_id: int) -> int:
        rows = 0
        for pos in client.net_positions():
            base = pos.get("NetPositionBase", {})
            view = pos.get("NetPositionView", {})
            fmt = pos.get("DisplayAndFormat", {})
            # The API's own FieldGroups name is "ExchangeInfo" but the key it actually
            # returns in the payload is "Exchange". CountryCode doesn't exist in this
            # group at all, so `country` stays None until a /ref/v1/instruments lookup
            # is added.
            exch = pos.get("Exchange", {})

            account_key = base.get("AccountKey")
            account_id = account_ids.get(account_key)
            if account_id is None:
                # Position on an account we didn't enumerate; attach to the first.
                account_id = next(iter(account_ids.values()), None)
                if account_id is None:
                    continue

            currency = (base.get("Currency") or fmt.get("Currency") or "EUR").upper()
            uic = base.get("Uic")
            if uic is None:
                continue

            if db.insert_position(
                self.conn,
                run_id=run_id,
                account_id=account_id,
                ts=ts,
                instrument_id=str(uic),
                symbol=fmt.get("Symbol"),
                isin=fmt.get("Isin") or base.get("Isin"),
                name=fmt.get("Description"),
                asset_class=ASSET_CLASS_MAP.get(base.get("AssetType", ""), "other"),
                quantity=float(base.get("Amount") or 0),
                avg_open_price=_f(base.get("OpenPrice")),
                last_price=_f(view.get("CurrentPrice")),
                market_value_minor=db.to_minor(_market_value(view)),
                currency=currency,
                unrealized_pl_minor=db.to_minor(view.get("ProfitLossOnTrade") or 0),
                exchange=exch.get("ExchangeId"),
                country=exch.get("CountryCode"),
            ):
                rows += 1
        return rows


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _market_value(view: dict) -> float:
    """Without a market-data subscription for an instrument's exchange, Saxo omits
    MarketValue entirely rather than sending 0. Reconstruct it from cost basis plus
    unrealised P/L, which holds regardless of live pricing: MarketValueOpen is the
    signed cost basis (negative for a buy), so its magnitude plus the P/L is the
    current value."""
    if "MarketValue" in view:
        return view["MarketValue"] or 0
    return abs(view.get("MarketValueOpen") or 0) + (view.get("ProfitLossOnTrade") or 0)

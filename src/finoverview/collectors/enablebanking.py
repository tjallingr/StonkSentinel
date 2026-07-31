"""Enable Banking collector (Rabobank, KBC).

Auth is a JWT you sign with your own RSA private key — not a static API key.
Header: {"typ": "JWT", "alg": "RS256", "kid": <application id>}
Payload: {"iss": "enablebanking.com", "aud": "api.enablebanking.com", iat, exp}

Sessions carry a PSD2 consent that expires (90-180 days depending on the bank).
When a session expires the collector logs an error and the dashboard turns the
source red; you then re-run `python -m finoverview.auth.eb_link` to re-consent.
No aggregator can automate that step away — it's an SCA requirement.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt

from .. import db
from .base import Collector

log = logging.getLogger(__name__)

API_BASE = "https://api.enablebanking.com"

# Enable Banking normalises balance types across banks, but which ones a given
# bank actually populates varies. Preference order, best first.
BALANCE_PREFERENCE = [
    "CLBD",              # closing booked
    "closingBooked",
    "ITAV",              # interim available
    "interimAvailable",
    "XPCD",              # expected
    "expected",
    "ITBD",              # interim booked
    "interimBooked",
    "OTHR",
    "other",
]


class EnableBankingClient:
    def __init__(self, app_id: str, private_key_path: Path, timeout: float = 30.0) -> None:
        self.app_id = app_id
        self._key = Path(private_key_path).read_text()
        self._client = httpx.Client(base_url=API_BASE, timeout=timeout)

    def _jwt(self, ttl: int = 3600) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": "enablebanking.com",
                "aud": "api.enablebanking.com",
                "iat": now,
                "exp": now + ttl,
            },
            self._key,
            algorithm="RS256",
            headers={"typ": "JWT", "alg": "RS256", "kid": self.app_id},
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._jwt()}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kw: Any) -> dict:
        resp = self._client.request(method, path, headers=self._headers(), **kw)
        if resp.status_code >= 400:
            raise EnableBankingError(resp.status_code, resp.text[:1000], path)
        return resp.json() if resp.content else {}

    # --- discovery ------------------------------------------------------
    def application(self) -> dict:
        """Sanity check: confirms your key signs valid JWTs and the app is Active."""
        return self._request("GET", "/application")

    def aspsps(self, country: str | None = None) -> dict:
        params = {"country": country} if country else None
        return self._request("GET", "/aspsps", params=params)

    # --- consent flow ---------------------------------------------------
    def start_auth(self, *, bank_name: str, country: str, redirect_url: str,
                   state: str, valid_until: str, psu_type: str = "personal") -> dict:
        """Returns {"url": ..., "authorization_id": ...}. Send the user to url."""
        payload = {
            "access": {"balances": True, "transactions": True, "valid_until": valid_until},
            "aspsp": {"name": bank_name, "country": country},
            "psu_type": psu_type,
            "redirect_url": redirect_url,
            "state": state,
        }
        return self._request("POST", "/auth", json=payload)

    def create_session(self, code: str) -> dict:
        """Exchange the redirect code for a session. Response contains
        session_id, access.valid_until, and the list of linked accounts."""
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> dict:
        return self._request("DELETE", f"/sessions/{session_id}")

    # --- data -----------------------------------------------------------
    def details(self, account_uid: str) -> dict:
        return self._request("GET", f"/accounts/{account_uid}/details")

    def balances(self, account_uid: str) -> dict:
        return self._request("GET", f"/accounts/{account_uid}/balances")

    def close(self) -> None:
        self._client.close()


class EnableBankingError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Enable Banking {status} on {path}: {body}")


def _amount_to_minor(amount: dict) -> tuple[int, str]:
    """Enable Banking amounts look like {"currency": "EUR", "amount": "1234.56"}."""
    return db.to_minor(amount["amount"]), amount["currency"].upper()


def _pick_balance(balances: list[dict]) -> dict | None:
    for wanted in BALANCE_PREFERENCE:
        for b in balances:
            if (b.get("balance_type") or b.get("balanceType") or "") == wanted:
                return b
    return balances[0] if balances else None


class EnableBankingCollector(Collector):
    name = "enablebanking"

    def collect(self, run_id: int) -> int:
        client = EnableBankingClient(
            self.settings.eb_app_id, self.settings.eb_private_key
        )
        rows = 0
        errors: list[str] = []
        try:
            sessions = db.load_sessions(self.conn, "enablebanking")
            if not sessions:
                raise RuntimeError(
                    "No Enable Banking sessions. Run: python -m finoverview.auth.eb_link"
                )
            for sess in sessions:
                try:
                    rows += self._collect_session(client, sess, run_id)
                except Exception as exc:  # noqa: BLE001
                    # One dead bank must not stop the other.
                    msg = f"{sess['institution']}: {type(exc).__name__}: {exc}"
                    log.error(msg)
                    errors.append(msg)
        finally:
            client.close()

        if errors and rows == 0:
            raise RuntimeError("; ".join(errors))
        if errors:
            log.warning("partial collection: %s", "; ".join(errors))
        return rows

    def _collect_session(self, client: EnableBankingClient, sess, run_id: int) -> int:
        institution = sess["institution"]
        info = client.get_session(sess["session_id"])

        # Keep the stored consent expiry current so the dashboard can warn early.
        valid_until = (info.get("access") or {}).get("valid_until") or sess["valid_until"]
        db.save_session(self.conn, "enablebanking", institution,
                        sess["session_id"], valid_until)

        ts = db.utcnow()
        rows = 0
        for uid in info.get("accounts", []):
            acct = client.details(uid)
            iban = (acct.get("account_id") or {}).get("iban") or ""
            name = acct.get("name") or acct.get("product") or "Account"
            label = f"{name} {iban[-4:]}" if iban else name
            kind = _guess_kind(acct)

            meta = self.apply_overrides("enablebanking", uid, {
                "label": label,
                "kind": kind,
                "liquid": True,
                "encumbered": False,
                "include_in_networth": True,
            })
            account_id = db.upsert_account(
                self.conn,
                provider="enablebanking",
                external_id=uid,
                institution=institution,
                currency=(acct.get("currency") or "EUR").upper(),
                **meta,
            )

            payload = client.balances(uid)
            balances = payload.get("balances", [])
            if not balances:
                log.warning("%s/%s: no balances returned", institution, label)
                continue

            chosen = _pick_balance(balances)
            minor, ccy = _amount_to_minor(chosen["balance_amount"])
            btype = chosen.get("balance_type") or chosen.get("balanceType") or "default"
            as_of = chosen.get("reference_date") or chosen.get("last_change_date_time")

            if db.insert_balance(
                self.conn,
                account_id=account_id,
                ts=ts,
                as_of=as_of,
                balance_minor=minor,
                currency=ccy,
                balance_type=btype,
                run_id=run_id,
            ):
                rows += 1
        return rows


def _guess_kind(acct: dict) -> str:
    if (acct.get("cash_account_type") or "").upper() == "SVGS":
        return "savings"
    text = " ".join(
        str(acct.get(k, "")) for k in ("name", "product", "cash_account_type", "usage")
    ).lower()
    if any(w in text for w in ("saving", "spaar", "depot", "deposit")):
        return "savings"
    return "checking"


def valid_until_default(days: int = 180) -> str:
    """PSD2 consent horizon. Banks may grant less than you ask for."""
    from datetime import timedelta

    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

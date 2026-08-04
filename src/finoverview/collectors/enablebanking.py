"""Enable Banking collector (Rabobank, KBC). PSD2 consent expires — re-run eb_link."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import jwt

from .. import db
from .base import Collector

log = logging.getLogger(__name__)

API_BASE = "https://api.enablebanking.com"

# Most ASPSPs allow only 4 unattended data fetches per account per day, and
# balances and transactions draw on the same budget. Two balance runs a day plus
# one transaction run leaves headroom; asking for more earns a 429 that takes the
# balances down with it. This is also why account /details is fetched once and
# then cached in the accounts row rather than on every run.
TX_MIN_INTERVAL_HOURS = 20

# Re-fetch a little history every time: banks book card payments days late, and a
# strictly-forward window would miss them permanently.
TX_OVERLAP_DAYS = 10

# First run only. Full history is available for roughly an hour after consent and
# then most banks clamp to 90 days, so the deep backfill has to happen right after
# eb_link runs — see the note at the bottom of that command's output.
TX_BACKFILL_DAYS = 730

# No documented page cap; this only stops a malformed continuation_key looping.
TX_MAX_PAGES = 50

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

    def transactions(self, account_uid: str, *, date_from: str | None = None,
                     date_to: str | None = None, strategy: str = "default",
                     status: str = "BOOK") -> Iterator[dict]:
        """Yield booked transactions, following continuation keys.

        Two API rules make the obvious loop wrong:

        1. A page can come back with an empty list AND a continuation key. So the
           loop condition is the key, never whether the page had rows.
        2. Every other query parameter must be byte-identical across the pages of
           one walk, so params is built once and only the key changes.

        `status` values are the ISO20022 short codes, not words: BOOK, PDNG, CNCL,
        HOLD, OTHR, RJCT, SCHD. 'BOOKED' is silently wrong.
        """
        params: dict[str, str] = {"transaction_status": status, "strategy": strategy}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to

        key: str | None = None
        for page in range(TX_MAX_PAGES):
            query = dict(params)
            if key:
                query["continuation_key"] = key
            payload = self._request(
                "GET", f"/accounts/{account_uid}/transactions", params=query
            )
            yield from payload.get("transactions") or []
            key = payload.get("continuation_key")
            if not key:
                return
        log.warning("stopped paginating %s after %d pages", account_uid, TX_MAX_PAGES)

    def close(self) -> None:
        self._client.close()


class EnableBankingError(RuntimeError):
    """API error. `code` is the machine-readable string from the body, which is what
    you branch on: ASPSP_RATE_LIMIT_EXCEEDED means back off for six hours, while
    EXPIRED_SESSION means the consent is dead and only a human can fix it."""

    def __init__(self, status: int, body: str, path: str) -> None:
        self.status = status
        self.body = body
        self.code = ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                self.code = str(parsed.get("error") or "")
        except (ValueError, TypeError):
            pass
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


def _iban_of(acct: dict | None) -> str | None:
    """IBAN, or the bank's local account number when there is no IBAN."""
    if not acct:
        return None
    iban = (acct.get("iban") or "").strip()
    if iban:
        return iban.upper()
    other = acct.get("other")
    if isinstance(other, list):
        other = other[0] if other else None
    if isinstance(other, dict):
        return (other.get("identification") or "").strip().upper() or None
    return None


def _counterparty(tx: dict, outgoing: bool) -> tuple[str | None, str | None]:
    """Who the money went to (outgoing) or came from (incoming).

    Falls back to the opposite side because some banks fill only one of the two
    regardless of direction, and a missing name here shows up on the dashboard as
    an anonymous row you can't act on.
    """
    order = ("creditor", "debtor") if outgoing else ("debtor", "creditor")
    name = iban = None
    for side in order:
        name = name or ((tx.get(side) or {}).get("name") or "").strip() or None
        iban = iban or _iban_of(tx.get(f"{side}_account"))
    return name, iban


def _description(tx: dict) -> str | None:
    """remittance_information is a list of strings in the spec, but be tolerant:
    a bare string from any one bank would otherwise be exploded into characters."""
    info = tx.get("remittance_information")
    if isinstance(info, str):
        parts = [info]
    elif isinstance(info, list):
        parts = [str(p) for p in info if p]
    else:
        parts = []
    if not parts:
        code = tx.get("bank_transaction_code") or {}
        parts = [str(code.get("description") or "")]
    text = " ".join(p.strip() for p in parts if p and p.strip())
    return text[:500] or None


_TRAILING_DIGITS = re.compile(r"[\s\-#]*\d+\s*$")


def _payee_key(name: str | None, iban: str | None) -> str | None:
    """The grouping key for "who do I pay the most".

    IBAN when there is one: it survives the bank rewriting the display name. When
    there isn't (cash machines, some card rails), fold the name — upper-case,
    collapse whitespace, and drop a trailing store/terminal number so that
    'ALBERT HEIJN 1234' and 'ALBERT HEIJN 5678' land on one payee.
    """
    if iban:
        return iban
    if not name:
        return None
    folded = " ".join(name.upper().split())
    folded = _TRAILING_DIGITS.sub("", folded).strip()
    return folded or None


def _booking_date(tx: dict) -> str | None:
    """booking_date is optional in the schema even for booked rows. Fall back rather
    than drop the transaction, but never invent today's date — a wrong date silently
    lands the payment in the wrong month."""
    for field in ("booking_date", "value_date", "transaction_date"):
        value = (tx.get(field) or "").strip()
        if value:
            return value[:10]
    return None


def _signed_minor(tx: dict) -> tuple[int, str]:
    """Signed minor units: negative means money left the account. Storing the sign
    lets a plain SUM() be a net figure instead of needing a CASE everywhere."""
    minor, ccy = _amount_to_minor(tx["transaction_amount"])
    outgoing = (tx.get("credit_debit_indicator") or "").upper() == "DBIT"
    return (-abs(minor) if outgoing else abs(minor)), ccy


def _external_id(tx: dict, *, booking_date: str, minor: int, ccy: str,
                 iban: str | None, description: str | None,
                 seen: dict[str, int]) -> str:
    """Dedup key within one account.

    entry_reference is the bank's own stable id, but the docs warn some ASPSPs omit
    it and some emit duplicates. When it's missing, hash the content instead and
    append an occurrence counter so two genuinely identical payments on the same day
    stay two rows. The counter is stable across re-fetches because the digest
    includes the date, so every row sharing a digest falls inside the same window.
    """
    ref = (tx.get("entry_reference") or "").strip()
    if ref:
        return ref
    basis = f"{booking_date}|{minor}|{ccy}|{iban or ''}|{(description or '')[:120]}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
    index = seen[digest]
    seen[digest] += 1
    return f"syn:{digest}:{index}"


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
            # /details costs one of the four daily unattended fetches, and account
            # name, IBAN and type do not change. Fetch it once, then read it back
            # from our own row so the budget is free for transactions.
            known = self.conn.execute(
                "SELECT iban, label, kind, currency FROM accounts "
                "WHERE provider = 'enablebanking' AND external_id = ?",
                (uid,),
            ).fetchone()

            if known and known["iban"]:
                iban = known["iban"]
                label, kind = known["label"], known["kind"]
                currency = known["currency"]
            else:
                acct = client.details(uid)
                iban = ((acct.get("account_id") or {}).get("iban") or "").upper()
                name = acct.get("name") or acct.get("product") or "Account"
                label = f"{name} {iban[-4:]}" if iban else name
                kind = _guess_kind(acct)
                currency = (acct.get("currency") or "EUR").upper()

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
                iban=iban or None,
                currency=currency,
                **meta,
            )

            payload = client.balances(uid)
            balances = payload.get("balances", [])
            if balances:
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
            else:
                log.warning("%s/%s: no balances returned", institution, label)

            # Transactions are a separate concern from balances: a bank that stops
            # serving one often still serves the other, and this must not undo the
            # balance row we just wrote.
            try:
                rows += self._collect_transactions(client, account_id, uid, run_id)
            except EnableBankingError as exc:
                if exc.code == "ASPSP_RATE_LIMIT_EXCEEDED":
                    # Expected, not a fault: we are inside the bank's 4-a-day budget
                    # for balances and simply have no call left for transactions.
                    log.info("%s/%s: transaction budget spent, next run will retry",
                             institution, label)
                else:
                    log.warning("%s/%s: transactions failed (%s): %s",
                                institution, label, exc.code or exc.status, exc)
        return rows

    def _collect_transactions(self, client: EnableBankingClient, account_id: int,
                              uid: str, run_id: int) -> int:
        """Fetch booked transactions for one account, incrementally.

        Skips entirely unless TX_MIN_INTERVAL_HOURS have passed, because the balance
        collector runs twice a day and the bank's unattended budget cannot carry a
        transaction fetch on every run.
        """
        row = self.conn.execute(
            "SELECT tx_fetched_at FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row and row["tx_fetched_at"]:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(row["tx_fetched_at"])).total_seconds() / 3600
            if age < TX_MIN_INTERVAL_HOURS:
                log.debug("account %s: transactions fetched %.1fh ago, skipping",
                          account_id, age)
                return 0

        # Claimed before the call, not after: if the fetch dies halfway we must not
        # retry immediately and burn the rest of the daily budget on a broken bank.
        db.mark_tx_fetched(self.conn, account_id)

        watermark = db.latest_transaction_date(self.conn, account_id)
        if watermark:
            start = date.fromisoformat(watermark) - timedelta(days=TX_OVERLAP_DAYS)
            strategy = "default"
        else:
            # First ever fetch. `longest` never raises WRONG_TRANSACTIONS_PERIOD when
            # the bank can't serve the range — it just returns what it has — at the
            # cost of possibly more ASPSP calls, which is the right trade once.
            start = date.today() - timedelta(days=TX_BACKFILL_DAYS)
            strategy = "longest"

        seen: dict[str, int] = defaultdict(int)
        rows = skipped = 0
        for tx in client.transactions(uid, date_from=start.isoformat(),
                                      strategy=strategy):
            booking_date = _booking_date(tx)
            # A direction we can't read is worse than a missing row: defaulting it
            # either way silently corrupts the spend total. Same for a missing
            # amount or date. Skip and count, don't guess.
            if (not booking_date
                    or not tx.get("transaction_amount")
                    or (tx.get("credit_debit_indicator") or "").upper()
                    not in ("CRDT", "DBIT")):
                skipped += 1
                continue

            try:
                minor, ccy = _signed_minor(tx)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                # One unparseable row must not abort a nightly unattended run and
                # leave the rest of the month uncollected.
                log.warning("account %s: unparseable amount, skipping row", account_id)
                skipped += 1
                continue

            name, iban = _counterparty(tx, outgoing=minor < 0)
            description = _description(tx)
            code = tx.get("bank_transaction_code") or {}

            if db.upsert_transaction(
                self.conn,
                run_id=run_id,
                account_id=account_id,
                external_id=_external_id(
                    tx, booking_date=booking_date, minor=minor, ccy=ccy,
                    iban=iban, description=description, seen=seen,
                ),
                booking_date=booking_date,
                value_date=(tx.get("value_date") or None),
                amount_minor=minor,
                currency=ccy,
                counterparty=name,
                counterparty_iban=iban,
                payee_key=_payee_key(name, iban),
                description=description,
                bank_code="-".join(
                    p for p in (code.get("code"), code.get("sub_code")) if p
                ) or None,
            ):
                rows += 1

        if skipped:
            log.warning("account %s: skipped %d transactions missing a date, amount "
                        "or direction", account_id, skipped)
        log.info("account %s: %d new transactions from %s (%s)",
                 account_id, rows, start, strategy)
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

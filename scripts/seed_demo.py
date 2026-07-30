"""Fill the database with synthetic data so you can see the dashboard working
before any real credential exists.

Run:  python scripts/seed_demo.py
Wipe: rm data/finance.db*

Nothing in here touches a real API. It's a wiring test: if the numbers render,
your schema, metrics and templates are sound and the only remaining unknown is
the shape of the live API payloads.
"""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finoverview import db  # noqa: E402
from finoverview.config import load_settings  # noqa: E402

random.seed(7)

ACCOUNTS = [
    dict(provider="enablebanking", external_id="demo-rabo-checking", institution="Rabobank",
         label="Rabobank checking 1234", kind="checking", currency="EUR", start=3200.0,
         drift=0.0, noise=450.0),
    dict(provider="enablebanking", external_id="demo-rabo-savings", institution="Rabobank",
         label="Rabobank savings", kind="savings", currency="EUR", start=21000.0,
         drift=250.0, noise=20.0),
    dict(provider="enablebanking", external_id="demo-kbc-checking", institution="KBC",
         label="KBC checking 5678", kind="checking", currency="EUR", start=1400.0,
         drift=0.0, noise=300.0),
    dict(provider="enablebanking", external_id="demo-kbc-savings", institution="KBC",
         label="KBC savings (collateral)", kind="savings", currency="EUR", start=35000.0,
         drift=0.0, noise=5.0, encumbered=True),
    dict(provider="manual", external_id="car", institution="Manual",
         label="Car", kind="vehicle", currency="EUR", start=14500.0,
         drift=-120.0, noise=0.0, liquid=False),
]

POSITIONS = [
    ("211", "IWDA", "IE00B4L5Y983", "iShares Core MSCI World", "etf", 82.0, 88.40, "EUR", "AEX", "NL"),
    ("212", "EMIM", "IE00BKM4GZ66", "iShares Core MSCI EM IMI", "etf", 140.0, 31.15, "EUR", "AEX", "NL"),
    ("213", "AGGH", "IE00BDBRDM35", "iShares Core Global Aggregate Bond", "bond", 95.0, 4.62, "EUR", "AEX", "NL"),
    ("214", "ASML", "NL0010273215", "ASML Holding", "equity", 4.0, 742.30, "EUR", "AEX", "NL"),
    ("215", "MSFT", "US5949181045", "Microsoft Corp", "equity", 6.0, 431.80, "USD", "NASDAQ", "US"),
    ("216", "VWCE", "IE00BK5BQT80", "Vanguard FTSE All-World", "etf", 30.0, 128.90, "EUR", "XETR", "DE"),
]

DAYS = 400


def main() -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)

    now = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)

    # FX: EUR/USD wandering around 1.08
    for i in range(DAYS + 1):
        day = (now - timedelta(days=DAYS - i)).date().isoformat()
        usd = 1.08 + 0.05 * math.sin(i / 47) + random.gauss(0, 0.002)
        db.insert_fx(conn, day, "EUR", "USD", round(usd, 5))
        db.insert_fx(conn, day, "USD", "EUR", round(1 / usd, 5))
        db.insert_fx(conn, day, "EUR", "EUR", 1.0)

    # Cash and manual accounts
    account_ids = {}
    for spec in ACCOUNTS:
        aid = db.upsert_account(
            conn,
            provider=spec["provider"],
            external_id=spec["external_id"],
            institution=spec["institution"],
            label=spec["label"],
            kind=spec["kind"],
            currency=spec["currency"],
            liquid=spec.get("liquid", True),
            encumbered=spec.get("encumbered", False),
        )
        account_ids[spec["external_id"]] = aid
        value = spec["start"]
        for i in range(DAYS + 1):
            ts = (now - timedelta(days=DAYS - i)).isoformat()
            value += spec["drift"] / 30 + random.gauss(0, spec["noise"] / 8)
            db.insert_balance(
                conn, account_id=aid, ts=ts,
                balance_minor=db.to_minor(round(max(value, 0), 2)),
                currency=spec["currency"],
                balance_type="manual" if spec["provider"] == "manual" else "CLBD",
            )

    # Brokerage: a random walk with a positive real drift, plus monthly deposits
    saxo_id = db.upsert_account(
        conn, provider="saxo", external_id="demo-saxo-acct", institution="Saxo",
        label="Saxo 12345678", kind="brokerage", currency="EUR",
    )
    account_ids["saxo"] = saxo_id

    index = 1.0
    portfolio = 42000.0
    for i in range(DAYS + 1):
        d = now - timedelta(days=DAYS - i)
        ts = d.isoformat()
        # ~7% nominal annual drift, ~16% annual vol
        index *= math.exp(random.gauss(0.07 / 252, 0.16 / math.sqrt(252)))
        portfolio *= math.exp(random.gauss(0.07 / 252, 0.16 / math.sqrt(252)))

        if d.day == 5:
            portfolio += 750.0
            db.upsert_cash_flow(
                conn, account_id=saxo_id, ts=d.date().isoformat(),
                amount_minor=db.to_minor(750.0), currency="EUR", kind="deposit",
                external_id=f"demo-{d.date().isoformat()}", note="monthly",
            )

        db.insert_balance(conn, account_id=saxo_id, ts=ts,
                          balance_minor=db.to_minor(round(portfolio, 2)),
                          currency="EUR", balance_type="TotalValue")
        db.insert_balance(conn, account_id=saxo_id, ts=ts,
                          balance_minor=db.to_minor(round(portfolio * 0.03, 2)),
                          currency="EUR", balance_type="CashBalance")

    # Latest position set, scaled so the values sum to the final portfolio value
    raw = [(q * p) for *_, q, p, _c, _e, _co in
           [(a, b, c, d, e, f, g, h, i, j) for a, b, c, d, e, f, g, h, i, j in POSITIONS]]
    scale = (portfolio * 0.97) / sum(raw)
    ts = now.isoformat()
    for uic, sym, isin, name, cls, qty, price, ccy, exch, country in POSITIONS:
        value = qty * price * scale
        db.insert_position(
            conn, account_id=saxo_id, ts=ts, instrument_id=uic, symbol=sym, isin=isin,
            name=name, asset_class=cls, quantity=round(qty * scale, 4),
            avg_open_price=price * 0.86, last_price=price,
            market_value_minor=db.to_minor(round(value, 2)), currency=ccy,
            unrealized_pl_minor=db.to_minor(round(value * 0.14, 2)),
            exchange=exch, country=country,
        )

    # Recurring items
    items = [
        ("salary", "Salary, net", "income", 3400.0, "monthly", None),
        ("rent", "Rent", "cost", 1150.0, "monthly", "housing"),
        ("groceries", "Groceries", "cost", 420.0, "monthly", "living"),
        ("health", "Health insurance", "cost", 149.0, "monthly", "insurance"),
        ("car-ins", "Car insurance", "cost", 520.0, "yearly", "insurance"),
        ("utilities", "Utilities", "cost", 165.0, "monthly", "housing"),
        ("subs", "Subscriptions", "cost", 46.0, "monthly", "living"),
    ]
    for key, label, kind, amount, period, cat in items:
        conn.execute(
            "INSERT OR REPLACE INTO recurring "
            "(key,label,kind,amount_minor,currency,period,category,active,updated_at) "
            "VALUES (?,?,?,?,?,?,?,1,?)",
            (key, label, kind, db.to_minor(amount), "EUR", period, cat, db.utcnow()),
        )

    # Collector runs, so the health strip has something to show
    for name in ("fx", "manual", "enablebanking", "saxo"):
        rid = db.start_run(conn, name)
        db.finish_run(conn, rid, rows=random.randint(2, 12))

    db.save_session(conn, "enablebanking", "Rabobank", "demo-session-rabo",
                    (now + timedelta(days=142)).isoformat().replace("+00:00", "Z"))
    db.save_session(conn, "enablebanking", "KBC", "demo-session-kbc",
                    (now + timedelta(days=9)).isoformat().replace("+00:00", "Z"))

    counts = {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("accounts", "balance_snapshots", "position_snapshots",
                  "portfolio_cash_flows", "fx_rates", "recurring")
    }
    print("seeded:", counts)
    print(f"db: {settings.db_path}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

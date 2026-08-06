# StonkSentinel

Personal finance dashboard that runs on a Raspberry Pi. Pulls balances from my banks (Enable Banking / PSD2) and Saxo, stores snapshots in SQLite, and shows net worth, returns, and some rough projections in a small FastAPI web UI.

Also logs bank transactions and breaks down where the money goes — `/expenses`, or `finoverview expenses`. Grouped by counterparty IBAN, so "who do I pay the most" is answered by account number rather than by whatever name the bank printed that month. Transfers between my own accounts don't count as spending: both legs are dropped, so only money entering or leaving the network as a whole registers as income or expense. Collectors register their own IBAN where the API exposes one; the rest go under `[[own_iban]]` in `config/assets.toml` and take effect on the next `make collect`.


Reach it on the LAN or from my phone over Tailscale.

## Stack

Python 3.11, SQLite, FastAPI + Jinja templates, systemd timers for collectors, httpx for API calls. Charts are server-rendered SVG, basically no JS.

## Setup (roughly)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp config/settings.example.toml config/settings.toml
cp config/assets.example.toml config/assets.toml
```

Try it with fake data first:

```bash
PYTHONPATH=src python scripts/seed_demo.py
PYTHONPATH=src uvicorn finoverview.web.app:app --port 8080
```

For real data you need Enable Banking credentials + bank consent, and Saxo OAuth. See `config/settings.example.toml`. Link commands:

```bash
PYTHONPATH=src python -m finoverview.auth.eb_link
PYTHONPATH=src python -m finoverview.auth.saxo_link
```

On the Pi: `sudo ./scripts/install.sh`, enable the systemd units, point Tailscale at port 8080. Details in `scripts/install.sh` and `systemd/`.

## Notes

- Bank PSD2 consent expires every few months: have to re-auth by hand (one-command job).
- **Run `collect --only enablebanking` immediately after `eb_link`.** Banks serve their
  full transaction history for only about an hour after consent, then clamp to 90 days.
  Wait and that history is gone until the next re-consent.
- Banks allow ~4 unattended data fetches per account per day, and balances and
  transactions share that budget. So account details are fetched once and cached, and
  transactions at most once per 20h, while balances stay twice daily. A 429 from the
  bank is logged and skipped, not treated as a failure.
- Saxo refresh tokens need the collector running regularly or you re-auth.
- Back up `data/finance.db` and `secrets/`. losing the Enable Banking key means starting over with the banks.

Private project, not for redistribution.

# stonksentinel

Self-hosted balances, net worth, portfolio performance and projections. Runs on a
Raspberry Pi 4, reachable over LAN and from a phone via Tailscale.

No transaction ingestion, deliberately. No reconciliation, no categorisation
rules, no double-entry ledger. This is a **snapshot store with a metrics layer**,
which is roughly an order of magnitude less work than a budgeting app and answers
the questions actually being asked: what do I own, what is it worth, how is it
performing, and where does it go from here.

```
collectors (systemd timers) -> SQLite (append-only) -> metrics -> FastAPI dashboard -> Tailscale
```

## Two things you cannot automate away

1. **PSD2 consent expires.** Bank account access must be re-authorised by you via
   Strong Customer Authentication every 90–180 days, per bank. No aggregator
   bypasses this. "Fully automatic" means automatic *between* re-consents.
2. **Saxo refresh tokens live ~24 hours.** The collector refreshes on every run and
   persists the rotated token, so the connection survives indefinitely while the Pi
   is up. If the Pi is offline longer than the refresh window, you re-auth by hand.

The dashboard surfaces both states before they bite. That is what the health strip
at the top of every page is for.

## Cost

Free. Enable Banking's **Restricted Production** mode is free for individual
non-commercial use on accounts you personally link. The paid tier only applies when
you "unrestrict" the app to reach other people's accounts, which requires a company
and a KYB process. You will never touch it.

Do not start on GoCardless Bank Account Data (ex-Nordigen) — it stopped onboarding
new customers in July 2025. Most self-hosting tutorials are stale on this point.

## Quick start

```bash
git clone <your repo> && cd stonksentinel
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp config/settings.example.toml config/settings.toml
cp config/assets.example.toml   config/assets.toml
```

### See it working before touching any credential

```bash
PYTHONPATH=src python scripts/seed_demo.py
PYTHONPATH=src uvicorn finoverview.web.app:app --port 8080
# http://localhost:8080
```

Synthetic data, no API calls. If the numbers render, your schema, metrics and
templates are sound and the only remaining unknown is the shape of the live
payloads. Decide what you want changed *now*, while it costs nothing.

```bash
rm data/finance.db*    # wipe the demo before going live
```

### Go live

Fill in `config/settings.toml`, put the Enable Banking RSA private key at
`secrets/enablebanking.pem` (`chmod 600`), then:

```bash
PYTHONPATH=src python -m finoverview.auth.eb_link --check              # key signs valid JWTs?
PYTHONPATH=src python -m finoverview.auth.eb_link --list --country NL  # exact ASPSP names
PYTHONPATH=src python -m finoverview.auth.eb_link                      # link Rabobank + KBC
PYTHONPATH=src python -m finoverview.auth.saxo_link                    # authorise Saxo
PYTHONPATH=src python -m finoverview.cli collect
PYTHONPATH=src python -m finoverview.cli status
```

Both link commands print an authorisation URL, you complete the bank login and SCA
in a browser, and then paste the redirect URL back in. There is no callback server:
the code is in the address bar, and building an HTTP endpoint to receive a value you
can read off the screen is work for no benefit.

`eb_link` prints each account's override key. Use them in `config/assets.toml` to
mark the KBC collateral savings:

```toml
[account_override."enablebanking:<uid>"]
label      = "KBC savings (collateral)"
kind       = "savings"
encumbered = true
```

Encumbered accounts count toward net worth but not toward **available**. Without
this the dashboard implies you can spend your collateral.

## Install on the Pi

```bash
sudo ./scripts/install.sh
sudo cp .env.example /etc/finoverview/env && sudo chmod 600 /etc/finoverview/env
sudo systemctl enable --now finoverview-web.service
sudo systemctl enable --now finoverview-collect@{fx,manual,enablebanking,saxo}.timer
sudo systemctl enable --now finoverview-backup.timer
systemctl list-timers 'finoverview*'
```

Per-collector schedules are in `systemd/drop-ins/` — see the README there. systemd
rather than cron for three reasons: `OnFailure=` hooks so a failure can page you,
journald logs you can grep, and `Persistent=true` catch-up after downtime. Cron
gives you silent failure, which here means quietly stale numbers you still trust.

**Wire up `finoverview-alert@.service` to something you actually read** (ntfy,
Gotify, Telegram). The default logs to the journal. A silent collector failure is
this project's real failure mode.

## Remote access

```bash
sudo tailscale serve --bg 8080
```

Real TLS cert on `pi.<tailnet>.ts.net`, works identically on LAN and from your
phone anywhere, zero ports open. Install Tailscale on the phone and add the site to
your home screen — it ships a web manifest, so it installs as a PWA.

Do not expose this to the public internet. It holds every balance you own plus live
bank read tokens. The web unit binds to `127.0.0.1` for that reason.

A consumer outbound VPN (Surfshark et al.) cannot give you remote access — no
inbound port forwarding — and routing the Pi through one adds breakage for no
benefit. If you insist, exclude the `tailscale0` interface from its routing.

## Storage

Boot from a USB SSD if you have one. But be honest about the reason: this workload
writes a few hundred KB a day, so SD-card wear is a *longevity* concern, not an
imminent one. What protects your financial history is a **tested restore**, not the
medium.

Avoid USB thumb drives — no TRIM, poor controllers, and they tend to fail by
silently corrupting rather than going read-only. A high-endurance microSD beats a
cheap flash drive.

Reduce writes regardless:

```
/etc/systemd/journald.conf   SystemMaxUse=50M
/etc/fstab                   noatime
sudo systemctl disable dphys-swapfile
```

## Backups

```bash
sudo -u finoverview ./scripts/backup.sh
sudo -u finoverview ./scripts/restore-test.sh    # run this now, not after a failure
```

Backs up the database via `VACUUM INTO` (never copy a live SQLite file — you can
capture a torn WAL), plus config and the Enable Banking private key. Losing that key
means re-registering the application and re-consenting at both banks.

## Data model

Money is stored as **integer minor units**, never floats. Snapshot tables are
**append-only** — nothing overwrites history.

| Table | Purpose |
|---|---|
| `accounts` | provider, kind, currency, `liquid`, `encumbered` |
| `balance_snapshots` | append-only balances, per balance type |
| `position_snapshots` | holdings with quantity, price, market value |
| `portfolio_cash_flows` | deposits/withdrawals — **required for correct returns** |
| `fx_rates` | ECB daily reference rates, both directions |
| `recurring` | costs and incomes, mirrored from `assets.toml` |
| `collector_runs` | run history; drives the freshness indicators |
| `provider_sessions` | Enable Banking sessions and consent expiry |
| `oauth_tokens` | Saxo access + rotating refresh token |

Net worth derives from `balance_snapshots` only, never from positions. A Saxo
`TotalValue` balance already includes the market value of every position in that
account; using both would double-count.

## Returns

Balance snapshots alone **cannot** tell you your return. A portfolio going
100k → 110k might be 10% growth, or 5% growth plus a 5k deposit. Separating the two
requires cash flows.

- **TWR** — compounded sub-period returns. Performance of the assets. Compare to an index.
- **MWR** — IRR over your actual cash flow timeline. Your outcome, including timing.

If `portfolio_cash_flows` is empty, both figures are wrong and the dashboard says so
rather than showing a plausible-looking number. Record deposits under `[[cash_flow]]`
in `assets.toml` until you've verified Saxo's cash-transfer endpoints yourself.

## Projections

Every assumption lives in `[projection]` in `assets.toml` and is rendered next to
the chart. A projection is an assumption engine with arithmetic attached; hiding the
inputs makes the output look like a forecast.

Monte Carlo uses independent lognormal annual draws. That understates the chance of
long consecutive drawdowns, so treat the lower percentile band as optimistic rather
than a floor.

## The Excel sheet

`config/assets.toml` replaces it. Commit the file. `git log -p config/assets.toml`
then tells you when you revalued the car and what from — history Excel never gave
you. Liabilities are negative `value`s.

## Dashboard design constraints

- **Zero external requests.** No CDN, no webfonts, no analytics. A page listing
  every balance you own should not phone anywhere. It also means it works offline.
- **Charts are server-rendered SVG.** No charting library, no build step, works with
  JavaScript disabled.
- **The only JavaScript** is a refresh timer that pauses when the tab is hidden.
- **Tabular figures everywhere**, so columns of numbers actually align.

## Verify these against your own accounts

Three things in the code are marked as unverified because I could not test them
against live credentials:

1. **`saxo.fetch_cash_flows`** — marked EXPERIMENTAL. Entitlements and payload shape
   vary by account type. Run it by hand and inspect the output before wiring it in.
2. **`enablebanking.BALANCE_PREFERENCE`** — a best-guess ordering of balance types.
   Rabobank and KBC will populate different subsets. Check the first real payload.
3. **PSD2 rate limits** — the shipped schedule polls banks twice daily to stay inside
   the common four-calls-per-account-per-day unattended cap. Confirm for your banks.

## Commands

```
finoverview init                 create the database
finoverview collect [--only X]   run collectors
finoverview status               freshness and consent state (exits non-zero if stale)
finoverview summary [--days N]   net worth, returns, allocation
finoverview project              projection table
finoverview backup PATH          consistent database copy
```

`GET /health` returns 503 when a source is stale — point an uptime check at it.

## Licence

Private personal project. `PRIVACY.md` and `TERMS.md` exist to satisfy the Enable
Banking application registration and describe single-user, non-commercial use.

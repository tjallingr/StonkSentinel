"""CLI: `python -m finoverview.cli <command>`

Commands:
  init                 create the database
  collect              run collectors (all, or --only NAME)
  status               print freshness and consent state
  summary              print net worth, returns, allocation
  expenses             print spend per month and biggest payees
  backup               vacuum the DB into a consistent single file
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import db
from .collectors import ALL, get_collector
from .config import load_assets, load_settings
from .metrics import allocation, expenses, health, networth, projection, returns
from .metrics import assets as asset_metrics


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_init(args, settings, conn) -> int:
    db.init_db(conn)
    print(f"Database ready at {settings.db_path}")
    return 0


def cmd_collect(args, settings, conn) -> int:
    db.init_db(conn)
    assets_cfg = load_assets(settings.config_dir)
    names = [args.only] if args.only else list(ALL)

    failures = []
    for name in names:
        cls = get_collector(name)
        try:
            rows = cls(conn, settings, assets_cfg).run()
            print(f"{name:<16} ok      {rows} rows")
        except Exception as exc:  # noqa: BLE001
            print(f"{name:<16} FAILED  {type(exc).__name__}: {exc}", file=sys.stderr)
            failures.append(name)

    # Exit non-zero so systemd marks the unit failed and OnFailure= fires.
    return 1 if failures else 0


def cmd_status(args, settings, conn) -> int:
    db.init_db(conn)
    rows = health.collectors(conn, settings.stale_after_hours)
    consents = health.consents(conn)

    print("SOURCES")
    for c in rows:
        flag = {"ok": "  ok  ", "stale": " STALE", "error": " ERROR",
                "never": " NEVER"}[c["status"]]
        print(f"  {flag}  {c['name']:<16} {c['age']:<12} {c['rows']} rows")
        if c["error"]:
            print(f"          {c['error'][:150]}")

    if consents:
        print("\nCONSENTS")
        for c in consents:
            left = f"{c['days_left']:.0f}d left" if c["days_left"] is not None else "unknown"
            print(f"  {c['status']:<9} {c['institution']:<16} {left:<12} {c['valid_until'] or ''}")

    print(f"\nOVERALL: {health.overall(rows, consents).upper()}")
    return 0 if health.overall(rows, consents) in ("ok", "warn") else 1


def cmd_summary(args, settings, conn) -> int:
    db.init_db(conn)
    base = settings.base_currency
    assets_cfg = load_assets(settings.config_dir)
    s = networth.summary(conn, base)

    print(f"NET WORTH  {s['net_worth']:>14,.2f} {base}")
    print(f"  invested {s['invested']:>14,.2f}")
    print(f"  cash     {s['cash']:>14,.2f}")
    print(f"  available{s['available']:>14,.2f}   (liquid, unencumbered)")
    if s["encumbered"]:
        print(f"  encumbered{s['encumbered']:>13,.2f}   (collateral, not spendable)")

    print("\nACCOUNTS")
    for a in s["accounts"]:
        flags = "".join(["E" if a.encumbered else "", "I" if not a.liquid else ""])
        print(f"  {a.label:<28} {a.amount_base:>14,.2f} {base}  {flags}")

    r = returns.compute(conn, base, days=args.days)
    print(f"\nRETURNS ({r.start} -> {r.end})")
    print(f"  TWR   {r.twr:>8.2f}%" if r.twr is not None else "  TWR      n/a")
    print(f"  MWR   {r.mwr:>8.2f}%" if r.mwr is not None else "  MWR      n/a")
    print(f"  gain  {r.gain:>12,.2f} {base} on {r.net_contributions:,.2f} contributed")
    for w in r.warnings:
        print(f"  ! {w}")

    br = allocation.breakdown(conn, base)
    rows = asset_metrics.allocation.by_category(asset_metrics.gather(conn, base, assets_cfg))
    if rows:
        print("\nALLOCATION")
        for b in rows:
            print(f"  {b['label']:<14} {b['pct']:>6.1f}%  {b['value']:>12,.2f}")
    if br["position_count"]:
        conc = allocation.concentration(conn, base)
        print(f"  top-5 share  {conc['top_n_pct']:.1f}%   "
              f"effective holdings {conc['effective_holdings']:.1f}")

    rec = allocation.recurring_summary(conn, base)
    if rec["lines"]:
        print("\nRECURRING (monthly)")
        print(f"  income {rec['monthly_income']:>12,.2f}")
        print(f"  cost   {rec['monthly_cost']:>12,.2f}")
        print(f"  net    {rec['monthly_net']:>12,.2f}", end="")
        if rec["savings_rate_pct"] is not None:
            print(f"   savings rate {rec['savings_rate_pct']:.1f}%")
        else:
            print()
    return 0


def cmd_project(args, settings, conn) -> int:
    db.init_db(conn)
    assets_cfg = load_assets(settings.config_dir)
    out = projection.run(conn, settings.base_currency, assets_cfg)
    a = out["assumptions"]

    print(f"PROJECTION ({settings.base_currency}, real terms)")
    print(f"  {'year':>5} {'p10':>14} {'p50':>14} {'p90':>14} {'deterministic':>14}")
    det = {d["year"]: d["value"] for d in out["deterministic"]}
    for band in out["monte_carlo"]["bands"]:
        if band["year"] % max(1, a.years // 10) and band["year"] != a.years:
            continue
        print(f"  {band['year']:>5} {band['p10']:>14,.0f} {band['p50']:>14,.0f} "
              f"{band['p90']:>14,.0f} {det.get(band['year'], 0):>14,.0f}")
    print(f"  {a.paths:,} paths, {a.volatility_pct:.1f}% volatility, "
          f"{a.years}y horizon")

    print(f"\nPER ASSET, IN {a.years} YEARS ({settings.base_currency})")
    print(f"  {'asset':<24} {'rate':>6} {'monthly':>10} {'invested':>13} "
          f"{'growth':>13} {'real':>13} {'nominal':>13}")
    for r in out["asset_rows"]:
        print(f"  {r['label'][:24]:<24} {r['rate']:>5.1f}% "
              f"{(r['monthly'] or 0):>10,.0f} {r['invested']:>13,.0f} "
              f"{r['growth']:>+13,.0f} {r['real']:>13,.0f} {r['nominal']:>13,.0f}")
    t = out["totals"]
    print(f"  {'Total':<24} {t['rate']:>5.1f}% {t['monthly']:>10,.0f} "
          f"{t['invested']:>13,.0f} {t['growth']:>+13,.0f} {t['real']:>13,.0f} "
          f"{t['nominal']:>13,.0f}")
    print(f"\n  invested = {t['today']:,.0f} today + {t['contributed']:,.0f} paid in, "
          f"no return applied")
    print(f"  nominal restates real at {a.inflation_pct:.1f}%/yr assumed inflation")
    return 0


def cmd_expenses(args, settings, conn) -> int:
    db.init_db(conn)
    base = settings.base_currency
    s = expenses.summary(conn, base, months=args.months)
    if not s["tx_count"]:
        print("No transactions stored. Run: finoverview collect --only enablebanking")
        return 1

    print(f"SPEND, LAST {args.months} MONTHS ({base})")
    for m in s["series"]:
        flag = " (partial)" if m["partial"] else ""
        print(f"  {m['month']}  {m['spend']:>12,.2f}  in {m['income']:>12,.2f}  "
              f"net {m['net']:>+12,.2f}  {m['count']:>4} payments{flag}")
    if s["average"]:
        print(f"  {'average':<9} {s['average']:>12,.2f}   (complete months only)")

    print(f"\nBIGGEST PAYEES ({s['payee_count']} total)")
    print(f"  {'payee':<30} {'account':<20} {'total':>12} {'/mo':>10} {'n':>5}")
    for p in expenses.top_payees(conn, base, months=args.months, limit=args.top):
        print(f"  {p['label'][:30]:<30} {(p['iban'] or '—')[:20]:<20} "
              f"{p['total']:>12,.2f} {p['monthly']:>10,.2f} {p['count']:>5}")

    cov = s["coverage"]
    if cov and cov["a"]:
        print(f"\n{s['tx_count']} transactions, {cov['a']} → {cov['b']}")
    itr = s["internal"]
    print(f"{itr['count']} transfers between your own accounts excluded "
          f"({itr['moved']:,.2f} {base} moved), {itr['accounts']} own IBANs registered.")
    return 0


def cmd_backup(args, settings, conn) -> int:
    dest = Path(args.dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # VACUUM INTO gives a consistent copy without stopping the web process.
    conn.execute("VACUUM INTO ?", (str(dest),))
    print(f"Wrote {dest} ({dest.stat().st_size / 1024:.0f} KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="finoverview")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--only", choices=ALL)

    sub.add_parser("status")

    p_sum = sub.add_parser("summary")
    p_sum.add_argument("--days", type=int, default=365)

    sub.add_parser("project")

    p_exp = sub.add_parser("expenses")
    p_exp.add_argument("--months", type=int, default=12)
    p_exp.add_argument("--top", type=int, default=15)

    p_bak = sub.add_parser("backup")
    p_bak.add_argument("dest")

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)

    settings = load_settings()
    conn = db.connect(settings.db_path)

    handlers = {
        "init": cmd_init, "collect": cmd_collect, "status": cmd_status,
        "summary": cmd_summary, "project": cmd_project, "backup": cmd_backup,
        "expenses": cmd_expenses,
    }
    try:
        return handlers[args.cmd](args, settings, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

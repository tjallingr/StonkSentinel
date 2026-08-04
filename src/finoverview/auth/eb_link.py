"""Enable Banking consent flow. Run: python -m finoverview.auth.eb_link"""

from __future__ import annotations

import argparse
import secrets
import sys
from urllib.parse import parse_qs, urlparse

from .. import db
from ..collectors.enablebanking import EnableBankingClient, valid_until_default
from ..config import load_settings


def extract_code(pasted: str) -> str:
    """Accept either a bare code or the whole redirect URL."""
    pasted = pasted.strip()
    if pasted.startswith("http"):
        qs = parse_qs(urlparse(pasted).query)
        for key in ("code", "authorization_code"):
            if key in qs:
                return qs[key][0]
        raise SystemExit(f"No ?code= parameter found in that URL:\n  {pasted}")
    return pasted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Link a bank via Enable Banking")
    ap.add_argument("--bank", help="ASPSP name exactly as Enable Banking lists it")
    ap.add_argument("--country", help="Two-letter country code, e.g. NL or BE")
    ap.add_argument("--days", type=int, default=180,
                    help="Requested consent validity (banks may grant less)")
    ap.add_argument("--list", action="store_true",
                    help="List available banks for --country and exit")
    ap.add_argument("--check", action="store_true",
                    help="Verify the app is active and your key signs valid JWTs")
    args = ap.parse_args(argv)

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)

    client = EnableBankingClient(settings.eb_app_id, settings.eb_private_key)

    if args.check:
        info = client.application()
        print("Application OK")
        print(f"  name        : {info.get('name')}")
        print(f"  environment : {info.get('environment')}")
        print(f"  active      : {info.get('active')}")
        print(f"  countries   : {', '.join(info.get('countries', []) or [])}")
        if info.get("active") is False:
            print("\nApp is INACTIVE. In the Enable Banking control panel, click "
                  "'Activate by linking accounts' before using the API.")
        return 0

    if args.list:
        if not args.country:
            raise SystemExit("--list requires --country")
        data = client.aspsps(country=args.country.upper())
        for a in data.get("aspsps", []):
            print(f"{a.get('name'):<40} {a.get('country')}  {a.get('psu_types')}")
        return 0

    banks = ([{"name": args.bank, "country": args.country}]
             if args.bank else settings.eb_banks)
    if not banks:
        raise SystemExit(
            "No banks given. Use --bank/--country, or add [[enablebanking.banks]] "
            "entries to config/settings.toml"
        )

    for bank in banks:
        name, country = bank["name"], bank["country"].upper()
        print(f"\n=== {name} ({country}) ===")
        state = secrets.token_urlsafe(16)
        auth = client.start_auth(
            bank_name=name,
            country=country,
            redirect_url=settings.eb_redirect_url,
            state=state,
            valid_until=valid_until_default(args.days),
            psu_type=bank.get("psu_type", "personal"),
        )
        print("\n1. Open this URL and complete the bank login + SCA:\n")
        print(f"   {auth['url']}\n")
        print("2. After approving, your browser will be redirected to")
        print(f"   {settings.eb_redirect_url}?code=...")
        print("   It will probably show a connection error. That is expected.")
        print("3. Copy the full URL from the address bar (or just the code) and paste below.\n")

        pasted = input("   code or redirect URL > ")
        code = extract_code(pasted)

        session = client.create_session(code)
        session_id = session.get("session_id") or session.get("sessionId")
        if not session_id:
            print(f"Unexpected /sessions response: {session}", file=sys.stderr)
            return 1

        valid_until = (session.get("access") or {}).get("valid_until")
        db.save_session(conn, "enablebanking", name, session_id, valid_until)

        accounts = session.get("accounts", [])
        print(f"\n   Linked. session_id={session_id}")
        print(f"   consent valid until: {valid_until}")
        print(f"   {len(accounts)} account(s):")
        for acct in accounts:
            iban = (acct.get("account_id") or {}).get("iban", "")
            print(f"     - uid={acct.get('uid')}  {acct.get('name') or ''} {iban}")
            print(f"       override key: enablebanking:{acct.get('uid')}")

        print("\n   Add [account_override.\"enablebanking:<uid>\"] blocks to "
              "config/assets.toml to mark accounts as savings/encumbered.")

    client.close()
    print("\nDone. Now run: python -m finoverview.cli collect --only enablebanking")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

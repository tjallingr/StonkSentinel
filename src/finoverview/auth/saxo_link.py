"""One-time Saxo OAuth authorisation code flow.

Run:  python -m finoverview.auth.saxo_link

Same approach as eb_link: no callback server, copy the code out of the browser
address bar. After this the collector keeps the connection alive by refreshing on
every run. You only come back here if the Pi is offline long enough for the
refresh token (~24h) to expire.
"""

from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .. import db
from ..collectors.saxo import LIVE_AUTH_BASE, SIM_AUTH_BASE
from ..config import load_settings


def extract_code(pasted: str) -> str:
    pasted = pasted.strip()
    if pasted.startswith("http"):
        qs = parse_qs(urlparse(pasted).query)
        if "error" in qs:
            raise SystemExit(f"Saxo returned an error: {qs['error']} "
                             f"{qs.get('error_description', [''])[0]}")
        if "code" not in qs:
            raise SystemExit("No ?code= parameter in that URL.")
        return qs["code"][0]
    return pasted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Authorise Saxo OpenAPI access")
    ap.add_argument("--sim", action="store_true",
                    help="Use the simulation environment instead of live")
    args = ap.parse_args(argv)

    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)

    live = not args.sim and bool(settings.saxo.get("live", True))
    auth_base = LIVE_AUTH_BASE if live else SIM_AUTH_BASE
    redirect_uri = settings.saxo["redirect_url"]
    app_key = settings.saxo_app_key
    app_secret = settings.saxo_app_secret

    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": app_key,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    url = f"{auth_base}/authorize?{urlencode(params)}"

    print(f"Environment: {'LIVE' if live else 'SIMULATION'}")
    print("\n1. Open this URL and log in to Saxo:\n")
    print(f"   {url}\n")
    print("2. You'll be redirected to")
    print(f"   {redirect_uri}?code=...&state=...")
    print("   A connection error page is expected — the code is in the address bar.")
    print("3. Paste the full URL (or just the code) below.\n")

    pasted = input("   code or redirect URL > ")
    if pasted.strip().startswith("http"):
        qs = parse_qs(urlparse(pasted.strip()).query)
        returned_state = qs.get("state", [None])[0]
        if returned_state and returned_state != state:
            raise SystemExit("State mismatch — start over.")
    code = extract_code(pasted)

    resp = httpx.post(
        f"{auth_base}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(app_key, app_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Token exchange failed ({resp.status_code}): {resp.text[:800]}")

    data = resp.json()
    now = datetime.now(timezone.utc)
    refresh_expires = (
        (now + timedelta(seconds=int(data["refresh_token_expires_in"]))).isoformat()
        if data.get("refresh_token_expires_in") else None
    )
    db.save_tokens(
        conn,
        "saxo",
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        access_expires_at=(now + timedelta(seconds=int(data.get("expires_in", 1200)))).isoformat(),
        refresh_expires_at=refresh_expires,
    )

    print("\nTokens stored.")
    print(f"  access token expires in : {data.get('expires_in')}s")
    print(f"  refresh token expires in: {data.get('refresh_token_expires_in')}s")
    print("\nThe collector must run at least once inside the refresh window to stay")
    print("connected. The systemd timer at every 4h gives ample margin.")
    print("\nNow run: python -m finoverview.cli collect --only saxo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

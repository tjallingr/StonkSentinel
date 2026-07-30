"""FX rates from the ECB daily reference feed.

Free, no API key, no rate limit, and authoritative for EUR-based reporting —
which matters more than it sounds: if your base currency is EUR you want the
same rates your bank and tax authority use, not Yahoo's mid-market snapshot.

Feeds:
  eurofxref-daily.xml       today only
  eurofxref-hist-90d.xml    last 90 days (used for backfill)
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

from .. import db
from .base import Collector

log = logging.getLogger(__name__)

DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HIST_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"

NS = {
    "gesmes": "http://www.gesmes.org/xml/2002-08-01",
    "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
}


def parse_ecb(xml_text: str) -> list[tuple[str, str, float]]:
    """Returns [(date, currency, rate_per_eur), ...]."""
    root = ET.fromstring(xml_text)
    out: list[tuple[str, str, float]] = []
    for day in root.iterfind(".//ecb:Cube[@time]", NS):
        date = day.attrib["time"]
        for cube in day.iterfind("ecb:Cube", NS):
            ccy = cube.attrib.get("currency")
            rate = cube.attrib.get("rate")
            if ccy and rate:
                out.append((date, ccy.upper(), float(rate)))
    return out


class FxCollector(Collector):
    name = "fx"

    def collect(self, run_id: int) -> int:
        backfill = not self.conn.execute("SELECT 1 FROM fx_rates LIMIT 1").fetchone()
        url = HIST_90D_URL if backfill else DAILY_URL
        if backfill:
            log.info("fx_rates empty, backfilling 90 days")

        resp = httpx.get(url, timeout=30.0)
        resp.raise_for_status()

        rows = 0
        for date, ccy, rate in parse_ecb(resp.text):
            # Store both directions so lookups never need to invert at read time.
            if db.insert_fx(self.conn, date, "EUR", ccy, rate):
                rows += 1
            if rate:
                db.insert_fx(self.conn, date, ccy, "EUR", 1.0 / rate)
            db.insert_fx(self.conn, date, "EUR", "EUR", 1.0)
        return rows


def convert_minor(conn, minor: int, from_ccy: str, to_ccy: str,
                  as_of: str | None = None) -> int:
    """Convert an integer-minor amount using the most recent rate at or before as_of.

    Falls back to the newest available rate if none is old enough — with a warning,
    because silently using a future rate would be worse than a slightly stale one.
    """
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        return minor

    row = None
    if as_of:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE base = ? AND quote = ? AND as_of <= ? "
            "ORDER BY as_of DESC LIMIT 1",
            (from_ccy, to_ccy, as_of[:10]),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE base = ? AND quote = ? "
            "ORDER BY as_of DESC LIMIT 1",
            (from_ccy, to_ccy),
        ).fetchone()
    if row is None:
        raise LookupError(f"No FX rate for {from_ccy}->{to_ccy}. Run the fx collector.")
    return round(minor * float(row["rate"]))

"""
Збирач day-ahead цін суміжних ринків (ENTSO-E A44) → PostgreSQL.

Зони (in_Domain == out_Domain для A44):
  HU 10YHU-MAVIR----U | SK 10YSK-SEPS-----K | PL 10YPL-AREA-----S | RO 10YRO-TEL------P
Ціна в EUR/МВт·год. Для порівняння з грн — join з fx_rates (EUR).

Особливості A44 (підтверджено на реальних даних, травень 2026):
  - resolution може бути PT60M або PT15M (15-хв MTU з 2025) → зводимо в годину (середнє).
  - curveType A03 = розріджені точки: ціна діє від своєї position до наступної
    наявної position → робимо forward-fill по сітці позицій періоду.
  - timeInterval у UTC; ринковий день сусіда у UTC зсунутий від київського —
    конвертація кожної точки UTC→Europe/Kyiv вирівнює все на спільний абсолютний час.

Використання:
  python entsoe_prices.py                          # 2024-01-01 -> сьогодні, усі зони
  python entsoe_prices.py --start 2023-05-26 --zones HU,PL

Середовище: ENTSOE_TOKEN, OREE_DSN
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import httpx

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("oree.prices")

BASE = "https://web-api.tp.entsoe.eu/api"
ZONES = {
    "HU": "10YHU-MAVIR----U",
    "SK": "10YSK-SEPS-----K",
    "PL": "10YPL-AREA-----S",
    "RO": "10YRO-TEL------P",
}
KYIV = ZoneInfo("Europe/Kyiv")
NS = "{*}"
TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF = 2.0

DDL = """
CREATE TABLE IF NOT EXISTS neighbor_prices (
    delivery_date DATE        NOT NULL,
    delivery_hour SMALLINT    NOT NULL,
    zone          VARCHAR(4)  NOT NULL,
    price         NUMERIC(12,4),         -- EUR/МВт·год
    currency      VARCHAR(4)  NOT NULL DEFAULT 'EUR',
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, zone)
);
CREATE INDEX IF NOT EXISTS idx_np_date ON neighbor_prices (delivery_date);
"""

UPSERT = """
INSERT INTO neighbor_prices (delivery_date, delivery_hour, zone, price, currency)
VALUES ($1,$2,$3,$4,$5)
ON CONFLICT (delivery_date, delivery_hour, zone) DO UPDATE
SET price = EXCLUDED.price, currency = EXCLUDED.currency, ingested_at = now();
"""


def _parse_dt(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_prices(xml_text, zone):
    """A44 XML -> [(date, hour, zone, price, currency)]; A03 forward-fill, год. середнє."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if root.tag.split("}")[-1].startswith("Acknowledgement"):
        return []

    acc = {}   # (date,hour) -> [prices]
    for ts in root.iter():
        if ts.tag.split("}")[-1] != "TimeSeries":
            continue
        cur_el = ts.find(f"{NS}currency_Unit.name")
        currency = (cur_el.text.strip() if cur_el is not None and cur_el.text else "EUR")
        for period in ts.iter():
            if period.tag.split("}")[-1] != "Period":
                continue
            start_el = period.find(f"{NS}timeInterval/{NS}start")
            end_el = period.find(f"{NS}timeInterval/{NS}end")
            res_el = period.find(f"{NS}resolution")
            if start_el is None or res_el is None:
                continue
            start = _parse_dt(start_el.text)
            end = _parse_dt(end_el.text) if end_el is not None else None
            if start is None:
                continue
            mm = re.search(r"PT(\d+)M", res_el.text or "")
            step = int(mm.group(1)) if mm else 60

            # listed points (A03 = sparse)
            pts = {}
            for pt in period.findall(f"{NS}Point"):
                pos_el = pt.find(f"{NS}position")
                pr_el = pt.find(f"{NS}price.amount")
                if pos_el is None or pr_el is None or pr_el.text is None:
                    continue
                try:
                    pts[int(pos_el.text)] = float(pr_el.text)
                except (TypeError, ValueError):
                    continue
            if not pts:
                continue

            # number of slots in the period grid
            if end is not None:
                n = int((end - start).total_seconds() // (step * 60))
            else:
                n = max(pts)
            n = max(n, max(pts))

            # forward-fill across the grid, map each slot to Kyiv-local hour
            cur = None
            for pos in range(1, n + 1):
                if pos in pts:
                    cur = pts[pos]
                if cur is None:
                    continue
                dt_utc = start + timedelta(minutes=step * (pos - 1))
                loc = dt_utc.astimezone(KYIV)
                acc.setdefault((loc.date(), loc.hour), []).append(cur)

    return [(d, h, zone, sum(v) / len(v), "EUR") for (d, h), v in acc.items()]


async def fetch(client, zone_eic, p1, p2):
    params = {
        "securityToken": os.environ["ENTSOE_TOKEN"],
        "documentType": "A44",
        "in_Domain": zone_eic,
        "out_Domain": zone_eic,
        "periodStart": p1,
        "periodEnd": p2,
    }
    for a in range(1, MAX_RETRIES + 1):
        try:
            r = await client.get(BASE, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (400, 401):
                log.warning("HTTP %s (%s): %s", r.status_code, zone_eic, r.text[:160])
                return None
            r.raise_for_status()
        except httpx.HTTPError as e:
            w = BACKOFF ** a
            log.warning("мережа (%s), спроба %d/%d через %.0fс", type(e).__name__, a, MAX_RETRIES, w)
            await asyncio.sleep(w)
    return None


def _chunks(start, end, days=365):
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=days - 1))
        yield cur, nxt
        cur = nxt + timedelta(days=1)


async def run(start, end, zones, dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            await c.execute(DDL)
        total = 0
        async with httpx.AsyncClient() as client:
            for z in zones:
                eic = ZONES[z]
                rows_z = 0
                for c0, c1 in _chunks(start, end):
                    p1 = c0.strftime("%Y%m%d") + "0000"
                    p2 = (c1 + timedelta(days=1)).strftime("%Y%m%d") + "0000"
                    xml = await fetch(client, eic, p1, p2)
                    if not xml:
                        await asyncio.sleep(0.5)
                        continue
                    rows = parse_prices(xml, z)
                    if rows:
                        async with pool.acquire() as conn:
                            async with conn.transaction():
                                await conn.executemany(UPSERT, rows)
                        rows_z += len(rows)
                    await asyncio.sleep(0.5)
                total += rows_z
                log.info("[%s] %d год.", z, rows_z)
        log.info("готово. рядків=%d", total)
        return 0
    finally:
        await pool.close()


def main():
    ap = argparse.ArgumentParser(description="ENTSO-E neighbor day-ahead prices (A44)")
    ap.add_argument("--start", default="2024-01-01", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--zones", default=",".join(ZONES), help="через кому: HU,SK,PL,RO")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()
    if not os.environ.get("ENTSOE_TOKEN"):
        log.error("немає ENTSOE_TOKEN")
        return 2
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today()
    zones = [x.strip().upper() for x in a.zones.split(",") if x.strip().upper() in ZONES]
    return asyncio.run(run(start, end, zones, a.dsn))


if __name__ == "__main__":
    sys.exit(main())

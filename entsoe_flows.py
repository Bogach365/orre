"""
Збирач транскордонних фізичних перетоків ENTSO-E (A11) → PostgreSQL.

По Україні ENTSO-E публікує перетоки (A11), але НЕ генерацію (A75) і НЕ
навантаження (A65) — воєнні обмеження. Тягнемо потоки по 5 кордонах
(HU/SK/PL/RO/MD) в обидва напрями.

Document A11 (Publication_MarketDocument):
  TimeSeries > Period > timeInterval(start,end, UTC) + resolution(PT60M)
             + Point(position, quantity[МВт])
Напрям визначається запитом:
  експорт (UA->N): out_Domain=UA, in_Domain=N
  імпорт  (N->UA): out_Domain=N,  in_Domain=UA
Час конвертуємо UTC -> Europe/Kyiv, щоб join з рештою таблиць по
(delivery_date, delivery_hour).

Використання:
  python entsoe_flows.py                         # 2024-01-01 -> сьогодні, усі кордони
  python entsoe_flows.py --start 2024-01-01 --end 2026-05-27
  python entsoe_flows.py --borders HU,PL

Середовище: ENTSOE_TOKEN, OREE_DSN
Залежності: httpx, asyncpg
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
log = logging.getLogger("oree.entsoe")

BASE = "https://web-api.tp.entsoe.eu/api"
UA = "10Y1001C--000182"           # Ukraine IPS CTA
BORDERS = {
    "HU": "10YHU-MAVIR----U",
    "SK": "10YSK-SEPS-----K",
    "PL": "10YPL-AREA-----S",
    "RO": "10YRO-TEL------P",
    "MD": "10Y1001A1001A990",
}
KYIV = ZoneInfo("Europe/Kyiv")
NS = "{*}"
TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF = 2.0

DDL = """
CREATE TABLE IF NOT EXISTS cross_border_flows (
    delivery_date DATE        NOT NULL,
    delivery_hour SMALLINT    NOT NULL,
    border        VARCHAR(4)  NOT NULL,
    direction     VARCHAR(6)  NOT NULL,   -- 'import' (->UA) | 'export' (UA->)
    mw            NUMERIC(12,4),
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, border, direction)
);
CREATE INDEX IF NOT EXISTS idx_cbf_date ON cross_border_flows (delivery_date);
"""

UPSERT = """
INSERT INTO cross_border_flows (delivery_date, delivery_hour, border, direction, mw)
VALUES ($1,$2,$3,$4,$5)
ON CONFLICT (delivery_date, delivery_hour, border, direction) DO UPDATE
SET mw = EXCLUDED.mw, ingested_at = now();
"""


def _parse_dt(s):
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_flows(xml_text, border, direction):
    """A11 XML -> [(date, hour, border, direction, mw)], час у Києві, год. середнє."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if root.tag.split("}")[-1].startswith("Acknowledgement"):
        return []
    acc = {}  # (date,hour) -> [values]
    for period in root.iter():
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

        # listed points (A03 = sparse: value holds from its position until the next)
        pts = {}
        for pt in period.findall(f"{NS}Point"):
            pos_el = pt.find(f"{NS}position")
            q_el = pt.find(f"{NS}quantity")
            if pos_el is None or q_el is None or q_el.text is None:
                continue
            try:
                pts[int(pos_el.text)] = float(q_el.text)
            except (TypeError, ValueError):
                continue
        if not pts:
            continue

        # number of slots in the period grid (so a single A03 point fills the whole span)
        if end is not None:
            n = int((end - start).total_seconds() // (step * 60))
        else:
            n = max(pts)
        n = max(n, max(pts))

        cur = None
        for pos in range(1, n + 1):
            if pos in pts:
                cur = pts[pos]
            if cur is None:
                continue
            dt_utc = start + timedelta(minutes=step * (pos - 1))
            loc = dt_utc.astimezone(KYIV)
            acc.setdefault((loc.date(), loc.hour), []).append(cur)
    return [(d, h, border, direction, sum(v) / len(v)) for (d, h), v in acc.items()]


async def fetch(client, out_dom, in_dom, p1, p2):
    params = {
        "securityToken": os.environ["ENTSOE_TOKEN"],
        "documentType": "A11",
        "out_Domain": out_dom,
        "in_Domain": in_dom,
        "periodStart": p1,
        "periodEnd": p2,
    }
    for a in range(1, MAX_RETRIES + 1):
        try:
            r = await client.get(BASE, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code in (400, 401):
                log.warning("HTTP %s (%s->%s): %s", r.status_code, out_dom, in_dom, r.text[:160])
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


async def run(start, end, borders, dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            await c.execute(DDL)
        total = 0
        async with httpx.AsyncClient() as client:
            for b in borders:
                ncode = BORDERS[b]
                for direction, out_dom, in_dom in (
                    ("export", UA, ncode),   # UA -> N
                    ("import", ncode, UA),   # N -> UA
                ):
                    rows_b = 0
                    for c0, c1 in _chunks(start, end):
                        p1 = c0.strftime("%Y%m%d") + "0000"
                        p2 = (c1 + timedelta(days=1)).strftime("%Y%m%d") + "0000"
                        xml = await fetch(client, out_dom, in_dom, p1, p2)
                        if not xml:
                            await asyncio.sleep(0.5)
                            continue
                        rows = parse_flows(xml, b, direction)
                        if rows:
                            async with pool.acquire() as conn:
                                async with conn.transaction():
                                    await conn.executemany(UPSERT, rows)
                            rows_b += len(rows)
                        await asyncio.sleep(0.5)  # ввічливо до ENTSO-E
                    total += rows_b
                    log.info("[%s %s] %d год.", b, direction, rows_b)
        log.info("готово. рядків=%d", total)
        return 0
    finally:
        await pool.close()


def main():
    ap = argparse.ArgumentParser(description="ENTSO-E cross-border flows (A11) collector")
    ap.add_argument("--start", default="2024-01-01", help="YYYY-MM-DD (UA в ENTSO-E з 2024-01)")
    ap.add_argument("--end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--borders", default=",".join(BORDERS), help="через кому: HU,SK,PL,RO,MD")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()
    if not os.environ.get("ENTSOE_TOKEN"):
        log.error("немає ENTSOE_TOKEN у середовищі")
        return 2
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today()
    borders = [x.strip().upper() for x in a.borders.split(",") if x.strip().upper() in BORDERS]
    return asyncio.run(run(start, end, borders, a.dsn))


if __name__ == "__main__":
    sys.exit(main())

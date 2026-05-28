"""
Збирач макростатистики НБУ (OpenData REST API) → PostgreSQL.
Універсальний: блоки задаються списком (--blocks), формат довгий.

API:
  дані:   GET https://bank.gov.ua/NBUStatService/v1/statdirectory/{apikod}
          ?start=YYYYMMDD&end=YYYYMMDD&period={m|q|y}&json
  список: GET https://bank.gov.ua/NBUStatService/v1/statdirectory?json

Підтверджені apikod (із точки входу):
  inflation         — «Ціни» (ІСЦ), періоди m,y
  balanceofpayments — «Платіжний баланс», періоди m,q,y
  res, res1, irad   — міжнародні резерви
  grossextdebt      — валовий зовнішній борг (q)
  interinvestpos    — міжнародна інвестиційна позиція (q)

Кожен запис: dt (dd.mm.yyyy), txt/txten (назва серії), id_api, freq,
value та довільні виміри (mcrd081, s181, k076, ...). Стандартні поля
відокремлюємо, решту складаємо у стабільний ключ dims — щоб різні
розрізи однієї серії не накладались.

Використання:
  python nbu_macro_collector.py                       # inflation+BoP, period=m, 2023→сьогодні
  python nbu_macro_collector.py --blocks inflation,balanceofpayments --period m --start 2023-01-01
  python nbu_macro_collector.py --blocks balanceofpayments --period q --start 2020-01-01

Середовище: OREE_DSN
Залежності: httpx, asyncpg
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("oree.nbu")

BASE = "https://bank.gov.ua/NBUStatService/v1/statdirectory"
UA = "oree-research-collector/0.1 (market research)"
TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF = 2.0

# поля, які НЕ є вимірами (решту складаємо в dims)
STD = {"dt", "txt", "txten", "id_api", "leveli", "parent", "freq", "value", "tzep"}

DDL = """
CREATE TABLE IF NOT EXISTS nbu_macro (
    block       VARCHAR(40) NOT NULL,
    dt          DATE        NOT NULL,
    series_id   TEXT        NOT NULL,   -- id_api або txt
    dims        TEXT        NOT NULL DEFAULT '',  -- стабільний ключ вимірів
    series_txt  TEXT,
    series_en   TEXT,
    freq        VARCHAR(4),
    value       NUMERIC,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (block, dt, series_id, dims)
);
"""

UPSERT = """
INSERT INTO nbu_macro (block, dt, series_id, dims, series_txt, series_en, freq, value)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (block, dt, series_id, dims) DO UPDATE
SET series_txt = EXCLUDED.series_txt, series_en = EXCLUDED.series_en,
    freq = EXCLUDED.freq, value = EXCLUDED.value, ingested_at = now();
"""


def _f(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.upper() == "NULL":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_block(block, payload):
    if not isinstance(payload, list):
        return []
    rows = []
    for it in payload:
        if not isinstance(it, dict):
            continue
        try:
            d = datetime.strptime(it.get("dt"), "%d.%m.%Y").date()
        except (TypeError, ValueError):
            continue
        txt = it.get("txt")
        en = it.get("txten")
        sid = it.get("id_api") or txt or ""
        dims = ";".join(f"{k}={it[k]}" for k in sorted(it) if k not in STD)
        rows.append((block, d, str(sid), dims, txt, en, it.get("freq"), _f(it.get("value"))))
    return rows


async def fetch(client, block, start, end, period):
    params = {
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "period": period,
        "json": "",
    }
    for a in range(1, MAX_RETRIES + 1):
        try:
            r = await client.get(f"{BASE}/{block}", params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            w = BACKOFF ** a
            log.warning("[%s] %s, спроба %d/%d через %.0fс",
                        block, type(e).__name__, a, MAX_RETRIES, w)
            await asyncio.sleep(w)
    log.error("[%s] не вдалося", block)
    return None


async def run(blocks, start, end, period, dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            await c.execute(DDL)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        total = 0
        async with httpx.AsyncClient(headers=headers) as client:
            for b in blocks:
                payload = await fetch(client, b, start, end, period)
                rows = parse_block(b, payload)
                if not rows:
                    log.warning("[%s] порожньо (можливо, інша періодичність — спробуйте --period q/y)", b)
                    continue
                async with pool.acquire() as c:
                    async with c.transaction():
                        await c.executemany(UPSERT, rows)
                total += len(rows)
                dts = sorted({r[1] for r in rows})
                log.info("[%s] %d записів, %d серій (%s → %s)",
                         b, len(rows), len({r[2] for r in rows}), dts[0], dts[-1])
                await asyncio.sleep(1.0)
        log.info("готово. рядків=%d", total)
        return 0
    finally:
        await pool.close()


def main():
    ap = argparse.ArgumentParser(description="NBU macro collector")
    ap.add_argument("--blocks", default="inflation,balanceofpayments",
                    help="comma-separated apikod")
    ap.add_argument("--period", default="m", help="m | q | y")
    ap.add_argument("--start", default="2023-01-01", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()
    blocks = [x.strip() for x in a.blocks.split(",") if x.strip()]
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today()
    return asyncio.run(run(blocks, start, end, a.period, a.dsn))


if __name__ == "__main__":
    sys.exit(main())

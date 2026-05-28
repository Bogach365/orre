"""
Збирач курсів НБУ (EUR, USD за замовчуванням) → PostgreSQL.

API НБУ (відкритий, без ключа, не за Cloudflare):
  https://bank.gov.ua/NBU_Exchange/exchange_site?start=YYYYMMDD&end=YYYYMMDD&valcode=EUR&json
Повертає масив, по рядку на робочий день:
  {"exchangedate":"01.04.2026","cc":"EUR","rate":50.4546, ...}
rate — грн за 1 одиницю валюти.

Використання:
    python fx_collector.py                                # 2023-01-01 → сьогодні
    python fx_collector.py --start 2023-01-01 --end 2026-05-28
    python fx_collector.py --currencies EUR,USD
    # daily (cron): python fx_collector.py --start "$(date -d '-7 days' +%F)"

Середовище:
    OREE_DSN — DSN PostgreSQL

Залежності: httpx, asyncpg

Примітка: НБУ публікує курс лише по робочих днях. У вихідні діє курс
останнього робочого дня — у запитах за потреби робимо forward-fill.
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
log = logging.getLogger("oree.fx")

NBU_URL = "https://bank.gov.ua/NBU_Exchange/exchange_site"
USER_AGENT = "oree-research-collector/0.1 (market research)"
TIMEOUT = 30.0
MAX_RETRIES = 4
BACKOFF = 2.0

DDL = """
CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date   DATE        NOT NULL,
    currency    VARCHAR(3)  NOT NULL,
    rate        NUMERIC(14,6),   -- грн за 1 одиницю валюти
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_date, currency)
);
"""

UPSERT = """
INSERT INTO fx_rates (rate_date, currency, rate)
VALUES ($1, $2, $3)
ON CONFLICT (rate_date, currency) DO UPDATE
SET rate = EXCLUDED.rate, ingested_at = now();
"""


def parse_rows(payload, currency):
    """NBU JSON → список кортежів (date, currency, rate)."""
    out = []
    if not isinstance(payload, list):
        return out
    for it in payload:
        try:
            d = datetime.strptime(it["exchangedate"], "%d.%m.%Y").date()
            rate = float(it["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        cc = it.get("cc") or currency
        out.append((d, cc, rate))
    return out


async def fetch_currency(client, currency, start, end):
    params = {
        "start": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "valcode": currency,
        "sort": "exchangedate",
        "order": "asc",
        "json": "",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = await client.get(NBU_URL, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            wait = BACKOFF ** attempt
            log.warning("[%s] помилка (%s), спроба %d/%d через %.0fс",
                        currency, type(e).__name__, attempt, MAX_RETRIES, wait)
            await asyncio.sleep(wait)
    log.error("[%s] не вдалося після %d спроб", currency, MAX_RETRIES)
    return None


async def run(start, end, currencies, dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        total = 0
        async with httpx.AsyncClient(headers=headers) as client:
            for cc in currencies:
                payload = await fetch_currency(client, cc, start, end)
                if payload is None:
                    continue
                rows = parse_rows(payload, cc)
                if not rows:
                    log.warning("[%s] порожньо", cc)
                    continue
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(UPSERT, rows)
                total += len(rows)
                log.info("[%s] завантажено %d днів (%s → %s)",
                         cc, len(rows), rows[0][0], rows[-1][0])
        log.info("готово. рядків=%d", total)
        return 0
    finally:
        await pool.close()


def main():
    ap = argparse.ArgumentParser(description="NBU FX collector")
    ap.add_argument("--start", default="2023-01-01", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--currencies", default="EUR,USD",
                    help="comma-separated valcodes (default: EUR,USD)")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today()
    currencies = [c.strip().upper() for c in a.currencies.split(",") if c.strip()]
    return asyncio.run(run(start, end, currencies, a.dsn))


if __name__ == "__main__":
    sys.exit(main())

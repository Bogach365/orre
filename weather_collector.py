"""
Збирач погоди (Open-Meteo Archive / ERA5) → PostgreSQL.

Тягне погодинну сонячну радіацію (GHI, shortwave_radiation, W/m²) і
температуру для заданої точки. Час — локальний київський (timezone=
Europe/Kyiv), тому ключі (delivery_date, delivery_hour) збігаються з
енергетичними таблицями (dam_clearing, imbalance_prices, ...).

API (відкритий, без ключа):
  https://archive-api.open-meteo.com/v1/archive
  ?latitude=..&longitude=..&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
  &hourly=shortwave_radiation,temperature_2m&timezone=Europe/Kyiv
Структура: {"hourly":{"time":[...],"shortwave_radiation":[...],
                      "temperature_2m":[...]}}

Використання:
    python weather_collector.py                          # Львів, 2023-01-01 → сьогодні
    python weather_collector.py --start 2023-01-01 --end 2026-05-31
    python weather_collector.py --lat 49.84 --lon 24.03 --location lviv

Середовище:
    OREE_DSN — DSN PostgreSQL
Залежності: httpx, asyncpg

Примітка: архів ERA5 має лаг ~5 днів (фінальний реаналіз). Для історії
capture price це не важливо; у добовий cron — не обов'язково.
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
log = logging.getLogger("oree.wx")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "oree-research-collector/0.1 (market research)"
TIMEOUT = 60.0
MAX_RETRIES = 4
BACKOFF = 2.0

DDL = """
CREATE TABLE IF NOT EXISTS weather_hourly (
    delivery_date DATE        NOT NULL,
    delivery_hour SMALLINT    NOT NULL,
    location      VARCHAR(40) NOT NULL,
    ghi           NUMERIC(8,2),   -- shortwave_radiation, W/m²
    temp_c        NUMERIC(6,2),   -- temperature_2m, °C
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, location)
);
"""

UPSERT = """
INSERT INTO weather_hourly (delivery_date, delivery_hour, location, ghi, temp_c)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (delivery_date, delivery_hour, location) DO UPDATE
SET ghi = EXCLUDED.ghi, temp_c = EXCLUDED.temp_c, ingested_at = now();
"""


def parse_payload(payload, location):
    """Open-Meteo JSON → список кортежів (date, hour, location, ghi, temp)."""
    h = (payload or {}).get("hourly") or {}
    times = h.get("time") or []
    ghi = h.get("shortwave_radiation") or []
    temp = h.get("temperature_2m") or []
    out = []
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except (TypeError, ValueError):
            continue
        g = ghi[i] if i < len(ghi) else None
        c = temp[i] if i < len(temp) else None
        out.append((dt.date(), dt.hour, location,
                    float(g) if g is not None else None,
                    float(c) if c is not None else None))
    return out


async def fetch_segment(client, lat, lon, seg_start, seg_end):
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": seg_start.isoformat(),
        "end_date": seg_end.isoformat(),
        "hourly": "shortwave_radiation,temperature_2m",
        "timezone": "Europe/Kyiv",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = await client.get(ARCHIVE_URL, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            wait = BACKOFF ** attempt
            log.warning("помилка (%s), спроба %d/%d через %.0fс",
                        type(e).__name__, attempt, MAX_RETRIES, wait)
            await asyncio.sleep(wait)
    log.error("сегмент %s..%s не вдалося", seg_start, seg_end)
    return None


async def run(start, end, lat, lon, location, dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        total = 0
        async with httpx.AsyncClient(headers=headers) as client:
            # порежемо на календарні роки — стабільніше й чистіші логи
            for y in range(start.year, end.year + 1):
                seg_start = max(start, date(y, 1, 1))
                seg_end = min(end, date(y, 12, 31))
                if seg_start > seg_end:
                    continue
                payload = await fetch_segment(client, lat, lon, seg_start, seg_end)
                rows = parse_payload(payload, location)
                if not rows:
                    log.warning("[%d] порожньо", y)
                    continue
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(UPSERT, rows)
                total += len(rows)
                log.info("[%s %d] завантажено %d годин (%s → %s)",
                         location, y, len(rows), rows[0][0], rows[-1][0])
                await asyncio.sleep(1.0)  # ввічливо
        log.info("готово. location=%s рядків=%d", location, total)
        return 0
    finally:
        await pool.close()


def main():
    ap = argparse.ArgumentParser(description="Open-Meteo weather collector")
    ap.add_argument("--start", default="2023-01-01", help="YYYY-MM-DD")
    ap.add_argument("--end", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--lat", type=float, default=49.84, help="широта (деф. Львів)")
    ap.add_argument("--lon", type=float, default=24.03, help="довгота (деф. Львів)")
    ap.add_argument("--location", default="lviv", help="мітка локації")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today()
    return asyncio.run(run(start, end, a.lat, a.lon, a.location, a.dsn))


if __name__ == "__main__":
    sys.exit(main())

"""
OREE IDM (Intraday Market / ВДР) collector (v0).

Fetches hourly weighted-average prices and volumes from OREE's PXS endpoints
for a date range and stores them in PostgreSQL plus raw JSON on disk.

Endpoints (reverse-engineered, May 2026):
    1. Daily zone list:
       POST https://www.oree.com.ua/index.php/PXS/get_pxs_res_idm
       body: day=DD.MM.YYYY  (form-urlencoded)
       response: HTML table with rows; each row has hidden input
                 .hdata_link with value "DD.MM.YYYY/IDM/<zone>"

    2. Hourly data per zone:
       POST https://www.oree.com.ua/index.php/PXS/get_pxs_hdata/<DD.MM.YYYY>/IDM/<zone>
       response: JSON {
           "html": "...",
           "pricesData":  [p1, p2, ..., p24],   # weighted avg price грн/МВт·год
           "amountsData": [v1, v2, ..., v24],   # traded volume МВт·год
           "labels":      [1, 2, ..., 24],      # hour numbers
           "colors":      [...]
       }

Zone codes (same as DAM): 2 = IPS (synchronous UA grid), 1 = BEI (usually empty).

Usage:
    # Backfill 3 years
    python idm_collector.py --start 2023-05-26 --end 2026-05-27

    # Daily incremental (cron, for T-1)
    python idm_collector.py --start "$(date -d 'yesterday' +%F)"

Environment:
    OREE_DSN      PostgreSQL DSN
    OREE_RAW_DIR  raw JSON dir (default ./raw_idm)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator

import asyncpg
import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DAILY_ENDPOINT = "https://www.oree.com.ua/index.php/PXS/get_pxs_res_idm"
HDATA_ENDPOINT = "https://www.oree.com.ua/index.php/PXS/get_pxs_hdata"
IDM_PAGE = "https://www.oree.com.ua/index.php/control/results_mo/IDM"

USER_AGENT = (
    "oree-research-collector/0.1 "
    "(market research; contact: REPLACE_WITH_YOUR_EMAIL)"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_RATE_LIMIT = 1.0
MAX_RETRIES = 4
BACKOFF_BASE = 2.0

RAW_DIR = Path(os.environ.get("OREE_RAW_DIR", "./raw_idm"))

# Pull hidden hdata_link values like "27.05.2026/IDM/2" out of the daily HTML.
HDATA_LINK_RE = re.compile(r'hdata_link"\s+value="([^"]+)"')

log = logging.getLogger("oree.idm")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class IdmRow:
    delivery_date: date
    delivery_hour: int
    zone: str
    price: float | None        # weighted-average price, грн/МВт·год
    volume: float | None       # traded volume, МВт·год


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

async def _post(client: httpx.AsyncClient, url: str, data: dict | None,
                target: date, what: str) -> httpx.Response | None:
    """POST with retry/backoff. Returns Response or None on permanent failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.post(url, data=data, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                log.error("[%s] %s: permanent HTTP %s", target, what, e.response.status_code)
                return None
            wait = BACKOFF_BASE ** attempt
            log.warning("[%s] %s: HTTP %s, retry %d/%d in %.1fs",
                        target, what, e.response.status_code, attempt, MAX_RETRIES, wait)
            await asyncio.sleep(wait)
        except (httpx.RequestError, asyncio.TimeoutError) as e:
            wait = BACKOFF_BASE ** attempt
            log.warning("[%s] %s: %s, retry %d/%d in %.1fs",
                        target, what, type(e).__name__, attempt, MAX_RETRIES, wait)
            await asyncio.sleep(wait)
    log.error("[%s] %s: gave up after %d attempts", target, what, MAX_RETRIES)
    return None


async def fetch_zones(client: httpx.AsyncClient, target: date) -> list[str]:
    """Fetch the daily table, return list of hdata_link params, e.g. ['27.05.2026/IDM/2']."""
    payload = {"day": target.strftime("%d.%m.%Y")}
    resp = await _post(client, DAILY_ENDPOINT, payload, target, "daily")
    if resp is None:
        return []
    links = HDATA_LINK_RE.findall(resp.text)
    # de-dupe, preserve order
    seen: dict[str, None] = {}
    for l in links:
        seen.setdefault(l, None)
    return list(seen)


async def fetch_hourly(client: httpx.AsyncClient, target: date,
                       link: str) -> dict[str, Any] | None:
    """Fetch hourly JSON for one zone link. Returns parsed dict or None."""
    url = f"{HDATA_ENDPOINT}/{link}"
    resp = await _post(client, url, None, target, f"hdata {link}")
    if resp is None:
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("[%s] hdata %s: bad JSON (%s)", target, link, type(e).__name__)
        return None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _zone_from_link(link: str) -> str:
    """'27.05.2026/IDM/2' -> 'IPS'  (2=IPS, 1=BEI), fallback to raw code."""
    code = link.rsplit("/", 1)[-1]
    return {"2": "IPS", "1": "BEI"}.get(code, code)


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def parse_hourly(target: date, link: str, payload: dict[str, Any]) -> list[IdmRow]:
    """Flatten pricesData/amountsData/labels into hourly rows."""
    zone = _zone_from_link(link)
    prices = payload.get("pricesData") or []
    amounts = payload.get("amountsData") or []
    labels = payload.get("labels") or list(range(1, len(prices) + 1))

    rows: list[IdmRow] = []
    for i, hour in enumerate(labels):
        try:
            h = int(hour)
        except (TypeError, ValueError):
            continue
        price = _to_float(prices[i]) if i < len(prices) else None
        volume = _to_float(amounts[i]) if i < len(amounts) else None
        # Skip fully empty hours (no trade that hour)
        if price is None and volume is None:
            continue
        rows.append(IdmRow(target, h, zone, price, volume))
    return rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS idm_prices (
    delivery_date  DATE         NOT NULL,
    delivery_hour  SMALLINT     NOT NULL,
    zone           VARCHAR(10)  NOT NULL,
    price          NUMERIC(12,4),
    volume         NUMERIC(14,4),
    ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, zone)
);

CREATE INDEX IF NOT EXISTS idx_idm_prices_date_brin
    ON idm_prices USING BRIN (delivery_date);

CREATE TABLE IF NOT EXISTS idm_ingestion_log (
    id             BIGSERIAL    PRIMARY KEY,
    delivery_date  DATE         NOT NULL,
    status         VARCHAR(20)  NOT NULL,   -- 'ok', 'fail', 'empty'
    rows_count     INT,
    error          TEXT,
    finished_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
"""

UPSERT_IDM = """
INSERT INTO idm_prices (delivery_date, delivery_hour, zone, price, volume)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (delivery_date, delivery_hour, zone) DO UPDATE
SET price = EXCLUDED.price, volume = EXCLUDED.volume, ingested_at = now();
"""

LOG_INGESTION = """
INSERT INTO idm_ingestion_log (delivery_date, status, rows_count, error)
VALUES ($1, $2, $3, $4);
"""


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(DDL)


async def persist(pool: asyncpg.Pool, rows: list[IdmRow]) -> None:
    if not rows:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT_IDM, [
                (r.delivery_date, r.delivery_hour, r.zone, r.price, r.volume)
                for r in rows
            ])


async def write_log(pool: asyncpg.Pool, target: date, status: str,
                    rows_count: int | None, error: str | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(LOG_INGESTION, target, status, rows_count, error)


def save_raw(target: date, link: str, payload: dict[str, Any]) -> None:
    """Store raw JSON: raw_idm/YYYY/MM/DD_<zone>.json — audit trail."""
    out_dir = RAW_DIR / f"{target.year:04d}" / f"{target.month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    zone = _zone_from_link(link)
    out_path = out_dir / f"{target.day:02d}_{zone}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def daterange(start: date, end: date) -> Iterator[date]:
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


async def run(start: date, end: date, dsn: str, rate_limit: float) -> int:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    if pool is None:
        log.error("could not create DB pool")
        return 2
    try:
        await init_schema(pool)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, */*",
            "Referer": IDM_PAGE,
            "X-Requested-With": "XMLHttpRequest",
        }
        ok = fail = empty = 0

        async with httpx.AsyncClient(headers=headers) as client:
            for target in daterange(start, end):
                log.info("fetching %s", target)
                links = await fetch_zones(client, target)

                if not links:
                    empty += 1
                    log.info("[%s] no zones (no session this day?)", target)
                    await write_log(pool, target, "empty", 0, None)
                    await asyncio.sleep(rate_limit)
                    continue

                day_rows: list[IdmRow] = []
                had_error = False
                for link in links:
                    payload = await fetch_hourly(client, target, link)
                    await asyncio.sleep(rate_limit)
                    if payload is None:
                        had_error = True
                        continue
                    save_raw(target, link, payload)
                    day_rows.extend(parse_hourly(target, link, payload))

                if had_error and not day_rows:
                    fail += 1
                    await write_log(pool, target, "fail", None, "hdata_failed")
                    continue

                if not day_rows:
                    empty += 1
                    await write_log(pool, target, "empty", 0, None)
                    continue

                await persist(pool, day_rows)
                await write_log(pool, target, "ok", len(day_rows), None)
                log.info("[%s] persisted idm rows=%d (zones=%d)",
                         target, len(day_rows), len(links))
                ok += 1

        log.info("done. ok=%d empty=%d fail=%d", ok, empty, fail)
        return 0 if fail == 0 else 1
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="OREE IDM (ВДР) collector")
    ap.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    ap.add_argument("--end", help="end date YYYY-MM-DD (default: same as --start)")
    ap.add_argument(
        "--dsn",
        default=os.environ.get("OREE_DSN",
                               "postgresql://postgres:postgres@localhost:5432/oree"),
        help="PostgreSQL DSN (env: OREE_DSN)",
    )
    ap.add_argument("--rate-limit", type=float, default=DEFAULT_RATE_LIMIT,
                    help="seconds between requests (default: 1.0)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else start
    if end < start:
        log.error("--end must be >= --start")
        return 2
    return asyncio.run(run(start, end, args.dsn, args.rate_limit))


if __name__ == "__main__":
    sys.exit(main())

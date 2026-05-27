"""
OREE DAM curves collector (v0).

Fetches aggregated supply/demand curves from OREE's DAM endpoint for a date
range and stores them in PostgreSQL plus raw JSON on disk (audit trail).

Endpoint (reverse-engineered, May 2026):
    POST https://www.oree.com.ua/index.php/control/lines_data/
    body: c_date=DD.MM.YYYY  (form-urlencoded)
    response: {"IPS": {"<hour>": {buy:[...], sell:[...], buyPrice, sellPrice}}, "BEI": [] | {}}

Usage:
    # Backfill 3 years (the actual job)
    python oree_collector.py --start 2023-05-11 --end 2026-05-11

    # Daily incremental (cron at 14:30 Europe/Kyiv for T-1)
    python oree_collector.py --start "$(date -d 'yesterday' +%F)"

Environment:
    OREE_DSN  PostgreSQL DSN (default: postgresql://postgres:postgres@localhost/oree)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import asyncpg
import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OREE_ENDPOINT = "https://www.oree.com.ua/index.php/control/lines_data/"
DAM_PAGE = "https://www.oree.com.ua/index.php/control/results_mo/DAM"

USER_AGENT = (
    "oree-research-collector/0.1 "
    "(REMIT screening; contact: REPLACE_WITH_YOUR_EMAIL)"
)
DEFAULT_TIMEOUT = 30.0
DEFAULT_RATE_LIMIT = 1.0   # seconds between requests — be polite
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
DB_CHUNK = 5000

RAW_DIR = Path(os.environ.get("OREE_RAW_DIR", "./raw"))

log = logging.getLogger("oree.collector")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class ClearingRow:
    delivery_date: date
    delivery_hour: int
    zone: str
    buy_price: float | None
    sell_price: float | None


@dataclass(slots=True, frozen=True)
class CurvePoint:
    delivery_date: date
    delivery_hour: int
    zone: str
    side: str            # 'B' or 'S'
    step_idx: int
    price: float | None
    cum_volume: float | None


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

async def fetch_day(client: httpx.AsyncClient, target: date) -> dict[str, Any] | None:
    """Fetch all 24 hours for one delivery date. Returns parsed JSON or None."""
    payload = {"c_date": target.strftime("%d.%m.%Y")}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.post(
                OREE_ENDPOINT,
                data=payload,
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, dict) or ("IPS" not in data and "BEI" not in data):
                log.warning("[%s] unexpected schema: top-level keys=%s",
                            target, list(data)[:5] if isinstance(data, dict) else type(data))
                return None
            return data

        except httpx.HTTPStatusError as e:
            # 4xx — likely permanent; don't waste retries on 404
            if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                log.error("[%s] permanent HTTP %s", target, e.response.status_code)
                return None
            wait = BACKOFF_BASE ** attempt
            log.warning("[%s] HTTP %s, retry %d/%d in %.1fs",
                        target, e.response.status_code, attempt, MAX_RETRIES, wait)
            await asyncio.sleep(wait)

        except (httpx.RequestError, json.JSONDecodeError, asyncio.TimeoutError) as e:
            wait = BACKOFF_BASE ** attempt
            log.warning("[%s] fetch error (%s), retry %d/%d in %.1fs",
                        target, type(e).__name__, attempt, MAX_RETRIES, wait)
            await asyncio.sleep(wait)

    log.error("[%s] gave up after %d attempts", target, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# Persistence: raw JSON audit trail
# ---------------------------------------------------------------------------

def save_raw(target: date, payload: dict[str, Any]) -> Path:
    """Store raw JSON: raw/YYYY/MM/DD.json — never delete, always re-parseable."""
    out_dir = RAW_DIR / f"{target.year:04d}" / f"{target.month:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{target.day:02d}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _to_float(v: Any) -> float | None:
    """OREE sometimes returns numbers as strings ('399.99') or 0 — normalize."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_response(
    target: date,
    payload: dict[str, Any],
) -> tuple[list[ClearingRow], list[CurvePoint]]:
    """Flatten the nested response into row lists ready for batch insert."""
    clearing: list[ClearingRow] = []
    points: list[CurvePoint] = []

    for zone in ("IPS", "BEI"):
        zone_block = payload.get(zone)
        # BEI is often [] (empty) post-2022 ENTSO-E integration; sometimes {}.
        if not isinstance(zone_block, dict) or not zone_block:
            continue

        for hour_key, hour_block in zone_block.items():
            try:
                hour = int(hour_key)
            except (TypeError, ValueError):
                # DST-day edge case: hour may be '3A'/'3B' or similar.
                # TODO: confirm representation on real DST date, e.g. 2025-10-26.
                log.warning("[%s %s] non-integer hour key — skipping", target, zone)
                continue

            if not isinstance(hour_block, dict):
                continue

            clearing.append(ClearingRow(
                delivery_date=target,
                delivery_hour=hour,
                zone=zone,
                buy_price=_to_float(hour_block.get("buyPrice")),
                sell_price=_to_float(hour_block.get("sellPrice")),
            ))

            for side_key, side_code in (("buy", "B"), ("sell", "S")):
                series = hour_block.get(side_key) or []
                for idx, pt in enumerate(series):
                    if not isinstance(pt, dict):
                        continue
                    points.append(CurvePoint(
                        delivery_date=target,
                        delivery_hour=hour,
                        zone=zone,
                        side=side_code,
                        step_idx=idx,
                        price=_to_float(pt.get("x")),
                        cum_volume=_to_float(pt.get("y")),
                    ))

    return clearing, points


# ---------------------------------------------------------------------------
# Persistence: PostgreSQL
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS dam_clearing (
    delivery_date  DATE         NOT NULL,
    delivery_hour  SMALLINT     NOT NULL,
    zone           VARCHAR(10)  NOT NULL,
    buy_price      NUMERIC(12,4),
    sell_price     NUMERIC(12,4),
    ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, zone)
);

CREATE TABLE IF NOT EXISTS dam_curves (
    delivery_date  DATE         NOT NULL,
    delivery_hour  SMALLINT     NOT NULL,
    zone           VARCHAR(10)  NOT NULL,
    side           CHAR(1)      NOT NULL,
    step_idx       INT          NOT NULL,
    price          NUMERIC(12,4),
    cum_volume     NUMERIC(14,4),
    PRIMARY KEY (delivery_date, delivery_hour, zone, side, step_idx)
);

CREATE INDEX IF NOT EXISTS idx_dam_curves_date_brin
    ON dam_curves USING BRIN (delivery_date);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id             BIGSERIAL    PRIMARY KEY,
    delivery_date  DATE         NOT NULL,
    status         VARCHAR(20)  NOT NULL,    -- 'ok', 'fail', 'empty'
    points_count   INT,
    error          TEXT,
    finished_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
"""

UPSERT_CLEARING = """
INSERT INTO dam_clearing
    (delivery_date, delivery_hour, zone, buy_price, sell_price)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (delivery_date, delivery_hour, zone) DO UPDATE
SET buy_price   = EXCLUDED.buy_price,
    sell_price  = EXCLUDED.sell_price,
    ingested_at = now();
"""

UPSERT_CURVE = """
INSERT INTO dam_curves
    (delivery_date, delivery_hour, zone, side, step_idx, price, cum_volume)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (delivery_date, delivery_hour, zone, side, step_idx) DO UPDATE
SET price = EXCLUDED.price, cum_volume = EXCLUDED.cum_volume;
"""

LOG_INGESTION = """
INSERT INTO ingestion_log (delivery_date, status, points_count, error)
VALUES ($1, $2, $3, $4);
"""


async def init_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(DDL)


async def persist(
    pool: asyncpg.Pool,
    clearing: list[ClearingRow],
    points: list[CurvePoint],
) -> None:
    if not clearing and not points:
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            if clearing:
                await conn.executemany(UPSERT_CLEARING, [
                    (c.delivery_date, c.delivery_hour, c.zone, c.buy_price, c.sell_price)
                    for c in clearing
                ])
            # Curve points can run 1k-10k per day — chunk for memory predictability.
            for i in range(0, len(points), DB_CHUNK):
                chunk = points[i:i + DB_CHUNK]
                await conn.executemany(UPSERT_CURVE, [
                    (p.delivery_date, p.delivery_hour, p.zone, p.side,
                     p.step_idx, p.price, p.cum_volume)
                    for p in chunk
                ])


async def write_ingestion_log(
    pool: asyncpg.Pool,
    target: date,
    status: str,
    points_count: int | None,
    error: str | None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(LOG_INGESTION, target, status, points_count, error)


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
            "Referer": DAM_PAGE,
            "X-Requested-With": "XMLHttpRequest",
        }
        ok = fail = empty = 0

        async with httpx.AsyncClient(headers=headers) as client:
            for target in daterange(start, end):
                log.info("fetching %s", target)
                data = await fetch_day(client, target)

                if data is None:
                    fail += 1
                    await write_ingestion_log(pool, target, "fail", None, "fetch_failed")
                    await asyncio.sleep(rate_limit)
                    continue

                save_raw(target, data)
                clearing, points = parse_response(target, data)

                if not clearing:
                    empty += 1
                    log.info("[%s] empty payload (likely no session this day)", target)
                    await write_ingestion_log(pool, target, "empty", 0, None)
                    await asyncio.sleep(rate_limit)
                    continue

                await persist(pool, clearing, points)
                await write_ingestion_log(pool, target, "ok", len(points), None)
                log.info("[%s] persisted clearing=%d points=%d",
                         target, len(clearing), len(points))
                ok += 1
                await asyncio.sleep(rate_limit)

        log.info("done. ok=%d empty=%d fail=%d", ok, empty, fail)
        return 0 if fail == 0 else 1
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="OREE DAM curves collector")
    ap.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    ap.add_argument("--end", help="end date YYYY-MM-DD (default: same as --start)")
    ap.add_argument(
        "--dsn",
        default=os.environ.get(
            "OREE_DSN",
            "postgresql://postgres:postgres@localhost:5432/oree",
        ),
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

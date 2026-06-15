#!/usr/bin/env python3
"""
backfill_volumes.py — збирає клірингові обсяги з OREE PXS endpoint
і записує в dam_clearing.cleared_volume через asyncpg
"""

import asyncio
import json
import time
import sys
import os
import argparse
from datetime import date, timedelta

import httpx
import asyncpg

OREE_PXS_HOURLY = "https://www.oree.com.ua/index.php/PXS/get_pxs_hdata/{date}/DAM/2"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.oree.com.ua/index.php/control/results_mo/DAM",
    "User-Agent": "oree-research-collector/0.2",
}

RATE_LIMIT = 1.0


async def get_dates_without_volume(pool, start: date, end: date):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT delivery_date 
            FROM dam_clearing
            WHERE delivery_date BETWEEN $1 AND $2
              AND zone = 'IPS'
              AND cleared_volume IS NULL
            ORDER BY delivery_date
        """, start, end)
    return [r["delivery_date"] for r in rows]


def fetch_hourly_volumes(target: date, client: httpx.Client):
    url = OREE_PXS_HOURLY.format(date=target.strftime("%d.%m.%Y"))
    try:
        resp = client.post(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        amounts = data.get("amountsData", [])
        prices  = data.get("pricesData", [])
        return amounts if amounts else None, prices
    except Exception as e:
        print(f"  [{target}] fetch error: {e}", file=sys.stderr)
        return None, None


async def save_volumes(pool, target: date, amounts: list):
    rows = []
    for i, vol in enumerate(amounts[:24]):
        if vol is not None:
            rows.append((float(vol), target, i + 1))
    if not rows:
        return False
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany("""
                UPDATE dam_clearing
                SET cleared_volume = $1
                WHERE delivery_date = $2
                  AND delivery_hour = $3
                  AND zone = 'IPS'
            """, rows)
    return True


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-05-26")
    parser.add_argument("--end",   default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--rate",  type=float, default=RATE_LIMIT)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    # DSN з env
    pg_pass = os.environ.get("PG_PASSWORD", "")
    dsn = os.environ.get("OREE_DSN", f"postgresql://oree:{pg_pass}@localhost:5432/oree")

    print(f"Connecting to DB...")
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=3)

    print(f"Fetching volumes: {start} → {end}")
    dates = await get_dates_without_volume(pool, start, end)
    print(f"Dates without volume: {len(dates)}")

    if not dates:
        print("Nothing to do.")
        await pool.close()
        return

    ok = fail = skip = 0

    with httpx.Client() as client:
        for i, target in enumerate(dates):
            amounts, prices = fetch_hourly_volumes(target, client)

            if amounts is None or len(amounts) < 24:
                print(f"  [{target}] skip (no data)")
                skip += 1
                time.sleep(args.rate)
                continue

            saved = await save_volumes(pool, target, amounts)
            if saved:
                total_vol = sum(v for v in amounts if v)
                avg_price = sum(prices) / len(prices) if prices else 0
                print(f"  [{target}] ok | vol={total_vol:,.0f} МВт·год | avg={avg_price:,.0f} грн")
                ok += 1
            else:
                print(f"  [{target}] db error", file=sys.stderr)
                fail += 1

            time.sleep(args.rate)

            if (i + 1) % 50 == 0:
                print(f"--- Progress: {i+1}/{len(dates)} | ok={ok} skip={skip} fail={fail} ---")

    await pool.close()
    print(f"\nDone. ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
backfill_volumes.py — збирає клірингові обсяги з OREE PXS endpoint
і записує в dam_clearing.cleared_volume

Запуск: python3 backfill_volumes.py [--start 2023-05-26] [--end 2026-06-15]
"""

import subprocess
import json
import time
import sys
import argparse
from datetime import date, timedelta

import httpx

OREE_PXS_DAILY  = "https://www.oree.com.ua/index.php/PXS/get_pxs_res"
OREE_PXS_HOURLY = "https://www.oree.com.ua/index.php/PXS/get_pxs_hdata/{date}/DAM/2"

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.oree.com.ua/index.php/control/results_mo/DAM",
    "User-Agent": "oree-research-collector/0.2",
}

RATE_LIMIT = 1.0  # секунди між запитами


def query_db(sql):
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree",
           "-t", "-A", "-F", "\t", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    rows = []
    for line in r.stdout.strip().split("\n"):
        if line.strip():
            rows.append(line.split("\t"))
    return rows


def exec_db(sql):
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree",
           "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def get_dates_without_volume(start: date, end: date):
    """Повертає дати де cleared_volume IS NULL."""
    rows = query_db(f"""
        SELECT DISTINCT delivery_date 
        FROM dam_clearing
        WHERE delivery_date BETWEEN '{start}' AND '{end}'
          AND zone = 'IPS'
          AND cleared_volume IS NULL
        ORDER BY delivery_date
    """)
    return [date.fromisoformat(r[0]) for r in rows if r[0]]


def fetch_hourly_volumes(target: date, client: httpx.Client):
    """Завантажує погодинні обсяги з OREE PXS."""
    url = OREE_PXS_HOURLY.format(date=target.strftime("%d.%m.%Y"))
    try:
        resp = client.post(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        amounts = data.get("amountsData", [])
        prices  = data.get("pricesData", [])
        if not amounts:
            return None
        # amountsData — список 24 значень (по годинах 1-24)
        return amounts, prices
    except Exception as e:
        print(f"  [{target}] fetch error: {e}", file=sys.stderr)
        return None


def save_volumes(target: date, amounts: list):
    """Записує обсяги в базу."""
    updates = []
    for i, vol in enumerate(amounts[:24]):
        hour = i + 1
        if vol is not None:
            updates.append(f"""
                UPDATE dam_clearing 
                SET cleared_volume = {float(vol)}
                WHERE delivery_date = '{target}' 
                  AND delivery_hour = {hour} 
                  AND zone = 'IPS'
            """)

    if not updates:
        return False

    sql = "BEGIN;" + "".join(updates) + "COMMIT;"
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Backfill cleared volumes")
    parser.add_argument("--start", default="2023-05-26")
    parser.add_argument("--end",   default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--rate",  type=float, default=RATE_LIMIT)
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    print(f"Fetching volumes: {start} → {end}")

    dates = get_dates_without_volume(start, end)
    print(f"Dates without volume: {len(dates)}")

    if not dates:
        print("Nothing to do.")
        return

    ok = fail = skip = 0

    with httpx.Client() as client:
        for i, target in enumerate(dates):
            result = fetch_hourly_volumes(target, client)

            if result is None:
                print(f"  [{target}] skip (no data)")
                skip += 1
                time.sleep(args.rate)
                continue

            amounts, prices = result

            if len(amounts) < 24:
                print(f"  [{target}] only {len(amounts)} hours, skip")
                skip += 1
                time.sleep(args.rate)
                continue

            saved = save_volumes(target, amounts)
            if saved:
                total_vol = sum(v for v in amounts if v)
                avg_price = sum(prices) / len(prices) if prices else 0
                print(f"  [{target}] ok | vol={total_vol:,.0f} МВт·год | avg_price={avg_price:,.0f}")
                ok += 1
            else:
                print(f"  [{target}] db error", file=sys.stderr)
                fail += 1

            time.sleep(args.rate)

            # Прогрес кожні 50 дат
            if (i + 1) % 50 == 0:
                print(f"Progress: {i+1}/{len(dates)} | ok={ok} skip={skip} fail={fail}")

    print(f"\nDone. ok={ok} skip={skip} fail={fail}")


if __name__ == "__main__":
    main()

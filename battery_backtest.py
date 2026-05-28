"""
Бектест батарейного арбітражу на РДН (dam_clearing).

Модель (ідеальне передбачення, перфект-форсайт, 1 цикл/добу):
  - корисна енергія циклу usable = energy * DoD
  - із мережі треба взяти grid_in = usable / eff_roundtrip (усі втрати на заряді)
  - ЗАРЯД: найдешевші години, ≤ power МВт·год/год, поки не наберемо grid_in
  - РОЗРЯД: найдорожчі години, ≤ power, поки не віддамо usable
  - прибуток дня = виручка(розряд) − витрати(заряд), грн
Ціна: dam_clearing.buy_price (грн/МВт·год), зона IPS.
Курс EUR — середній за період з fx_rates (для довідкового переведення).

Запуск:
  python battery_backtest.py                       # 1 МВт/2 МВт·год, eff 0.87, DoD 0.9
  python battery_backtest.py --power 5 --energy 10 --eff 0.85
  python battery_backtest.py --start 2024-01-01 --end 2025-12-31 --csv bess.csv

Середовище: OREE_DSN
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from collections import defaultdict
from datetime import date

import asyncpg


def day_profit(prices, power, usable, grid_in):
    """Прибуток за добу (грн) для одного циклу, з обмеженням потужності."""
    idx = sorted(range(len(prices)), key=lambda i: prices[i])  # від дешевих
    cost = 0.0
    rem = grid_in
    for i in idx:                       # заряд у найдешевші
        if rem <= 1e-9:
            break
        q = min(power, rem)
        cost += prices[i] * q
        rem -= q
    rev = 0.0
    rem = usable
    for i in reversed(idx):             # розряд у найдорожчі
        if rem <= 1e-9:
            break
        q = min(power, rem)
        rev += prices[i] * q
        rem -= q
    # середні ціни заряду/розряду для діагностики
    return rev - cost


async def main():
    ap = argparse.ArgumentParser(description="BESS day-ahead arbitrage backtest")
    ap.add_argument("--power", type=float, default=1.0, help="потужність, МВт")
    ap.add_argument("--energy", type=float, default=2.0, help="ємність, МВт·год")
    ap.add_argument("--eff", type=float, default=0.87, help="round-trip ККД (0..1)")
    ap.add_argument("--dod", type=float, default=0.90, help="глибина розряду (0..1)")
    ap.add_argument("--start", default="2023-05-26")
    ap.add_argument("--end", help="default: today")
    ap.add_argument("--zone", default="IPS")
    ap.add_argument("--csv", help="записати помісячний підсумок у CSV")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()

    usable = a.energy * a.dod
    grid_in = usable / a.eff
    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else date.today()

    pool = await asyncpg.create_pool(dsn=a.dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            rows = await c.fetch("""
                SELECT delivery_date, delivery_hour, buy_price
                FROM dam_clearing
                WHERE zone=$1 AND delivery_date BETWEEN $2 AND $3
                  AND buy_price IS NOT NULL
                ORDER BY delivery_date, delivery_hour
            """, a.zone, start, end)
            eur = await c.fetchval("""
                SELECT AVG(rate) FROM fx_rates
                WHERE currency='EUR' AND rate_date BETWEEN $1 AND $2
            """, start, end)
        eur = float(eur) if eur else None

        # групуємо по добі
        days = defaultdict(list)
        for r in rows:
            days[r["delivery_date"]].append(float(r["buy_price"]))

        per_month = defaultdict(lambda: [0.0, 0])   # 'YYYY-MM' -> [profit_sum, day_count]
        per_year = defaultdict(lambda: [0.0, 0])
        total = 0.0
        nd = 0
        for d, prices in sorted(days.items()):
            if len(prices) < 20:        # пропускаємо неповні доби
                continue
            p = day_profit(prices, a.power, usable, grid_in)
            total += p
            nd += 1
            per_month[f"{d:%Y-%m}"][0] += p
            per_month[f"{d:%Y-%m}"][1] += 1
            per_year[f"{d:%Y}"][0] += p
            per_year[f"{d:%Y}"][1] += 1

        # вивід
        print("=" * 60)
        print("БЕКТЕСТ БАТАРЕЙНОГО АРБІТРАЖУ (РДН, перфект-форсайт, 1 цикл/добу)")
        print(f"Батарея: {a.power} МВт / {a.energy} МВт·год "
              f"({a.energy/a.power:.1f} год) | ККД {a.eff:.0%} | DoD {a.dod:.0%}")
        print(f"Корисно/цикл: {usable:.2f} МВт·год | із мережі: {grid_in:.2f} МВт·год")
        print(f"Період: {start} .. {end} | повних діб: {nd}")
        print("=" * 60)
        print(f"{'Місяць':<9}{'діб':>5}{'грн/добу':>12}{'грн/міс':>14}")
        for m in sorted(per_month):
            s, cnt = per_month[m]
            print(f"{m:<9}{cnt:>5}{s/cnt:>12,.0f}{s:>14,.0f}")
        print("-" * 60)
        print(f"{'Рік':<9}{'діб':>5}{'грн/добу':>12}{'грн/рік':>14}")
        for y in sorted(per_year):
            s, cnt = per_year[y]
            print(f"{y:<9}{cnt:>5}{s/cnt:>12,.0f}{s:>14,.0f}")
        print("=" * 60)
        avg_day = total / nd if nd else 0
        annual = avg_day * 365
        per_mwh_year = annual / a.energy
        print(f"Сер. прибуток: {avg_day:,.0f} грн/добу")
        print(f"Річний (екстраполяція): {annual:,.0f} грн/рік на батарею")
        print(f"  = {per_mwh_year:,.0f} грн/рік на 1 МВт·год ємності")
        if eur:
            print(f"  ~ {annual/eur:,.0f} EUR/рік (курс {eur:.1f}); "
                  f"{per_mwh_year/eur:,.0f} EUR/рік на МВт·год")
        print("Примітка: верхня межа (ідеальне передбачення цін, без деградації,")
        print("без плати за мережу/податків, 1 цикл/добу). Реальний дохід нижчий.")

        if a.csv:
            with open(a.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["month", "days", "profit_uah", "profit_uah_per_day"])
                for m in sorted(per_month):
                    s, cnt = per_month[m]
                    w.writerow([m, cnt, round(s), round(s/cnt)])
            print(f"\nCSV: {a.csv}")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

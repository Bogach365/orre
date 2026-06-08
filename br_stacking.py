"""
БР-стекінг поверх арбітражу РДН: чи додає балансуючий ринок дохідність батареї?

Модель (перфект-форсайт, 1 цикл/добу):
  Для кожної години:
    sell_price_h = max(DAM_h, marg_up_h)    # розряд у вище з двох ринків
    buy_price_h  = min(DAM_h, marg_down_h)  # заряд у дешевшому
  Далі — звичайний арбітраж: топ-k_dis за sell_price (розряд), низ-k_chg за
  buy_price серед решти годин (заряд, диз'юнктно). Витрати/виручка з ККД і DoD.

Конвенція БР (припущення, перевіримо результатом):
  - marg_up: батарея ОТРИМУЄ цю ціну за розряд у up-регулюванні (виручка).
  - marg_down: батарея ПЛАТИТЬ цю ціну за заряд у down-регулюванні (витрати).
  Якщо в UA конвенція інша (down-провайдер отримує плату) — потенціал стекінгу
  ще вищий, обговоримо після результату.

Запуск:
  python br_stacking.py
  python br_stacking.py --start 2025-01-01 --end 2025-12-31

Середовище: OREE_DSN
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from datetime import date

import asyncpg


def day_profit(prices_sell, prices_buy, power, usable, grid_in):
    """Денний прибуток для 1 циклу при різних цінах продажу/купівлі по годинах."""
    n = len(prices_sell)
    # години для розряду — топ за sell, для заряду — низ за buy (диз'юнктно)
    sell_order = sorted(range(n), key=lambda i: -prices_sell[i])
    k_dis = max(1, math.ceil(usable / power))
    discharge_set = set(sell_order[:k_dis])
    remaining = [i for i in range(n) if i not in discharge_set]
    buy_order = sorted(remaining, key=lambda i: prices_buy[i])
    k_chg = max(1, math.ceil(grid_in / power))
    charge_set = set(buy_order[:k_chg])

    # виручка з розряду
    rev = 0.0
    rem = usable
    for h in sorted(discharge_set, key=lambda i: -prices_sell[i]):
        if rem <= 1e-9:
            break
        q = min(power, rem)
        rev += prices_sell[h] * q
        rem -= q
    # витрати на заряд
    cost = 0.0
    rem = grid_in
    for h in sorted(charge_set, key=lambda i: prices_buy[i]):
        if rem <= 1e-9:
            break
        q = min(power, rem)
        cost += prices_buy[h] * q
        rem -= q
    return rev - cost


async def main():
    ap = argparse.ArgumentParser(description="BR stacking backtest")
    ap.add_argument("--power", type=float, default=1.0)
    ap.add_argument("--energy", type=float, default=2.0)
    ap.add_argument("--eff", type=float, default=0.87)
    ap.add_argument("--dod", type=float, default=0.90)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", help="default: сьогодні")
    ap.add_argument("--zone", default="IPS")
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
                SELECT d.delivery_date, d.delivery_hour,
                       d.buy_price::float AS dam,
                       b.marg_up::float   AS up,
                       b.marg_down::float AS dn
                FROM dam_clearing d
                JOIN br_marginal_prices b
                  ON b.delivery_date = d.delivery_date
                 AND b.delivery_hour = d.delivery_hour
                WHERE d.zone = $1
                  AND d.delivery_date BETWEEN $2 AND $3
                  AND d.buy_price IS NOT NULL
                  AND b.marg_up   IS NOT NULL
                  AND b.marg_down IS NOT NULL
                ORDER BY d.delivery_date, d.delivery_hour
            """, a.zone, start, end)
            eur = await c.fetchval("""
                SELECT AVG(rate) FROM fx_rates
                WHERE currency='EUR' AND rate_date BETWEEN $1 AND $2
            """, start, end)
        eur = float(eur) if eur else None

        days = defaultdict(list)   # date -> list of (dam, up, dn) for 24 hours
        for r in rows:
            days[r["delivery_date"]].append((r["dam"], r["up"], r["dn"]))

        per_month = defaultdict(lambda: [0.0, 0.0, 0])   # 'YYYY-MM' -> [dam_sum, stacked_sum, n]
        per_year = defaultdict(lambda: [0.0, 0.0, 0])
        dam_total = stacked_total = 0.0
        nd = 0
        for d, hrs in sorted(days.items()):
            if len(hrs) < 20:
                continue
            dam = [h[0] for h in hrs]
            up = [h[1] for h in hrs]
            dn = [h[2] for h in hrs]
            # baseline: DAM only
            p_dam = day_profit(dam, dam, a.power, usable, grid_in)
            # stacked: max sell / min buy across DAM and BR
            sell = [max(d_, u_) for d_, u_ in zip(dam, up)]
            buy  = [min(d_, n_) for d_, n_ in zip(dam, dn)]
            p_st = day_profit(sell, buy, a.power, usable, grid_in)

            dam_total += p_dam
            stacked_total += p_st
            nd += 1
            per_month[f"{d:%Y-%m}"][0] += p_dam
            per_month[f"{d:%Y-%m}"][1] += p_st
            per_month[f"{d:%Y-%m}"][2] += 1
            per_year[f"{d:%Y}"][0] += p_dam
            per_year[f"{d:%Y}"][1] += p_st
            per_year[f"{d:%Y}"][2] += 1

        print("=" * 64)
        print("БЕКТЕСТ БР-СТЕКІНГУ (РДН + балансуючий, перфект-форсайт, 1 цикл)")
        print(f"Батарея: {a.power} МВт / {a.energy} МВт·год | ККД {a.eff:.0%} | DoD {a.dod:.0%}")
        print(f"Період: {start} .. {end} | повних діб з БР: {nd}")
        print("=" * 64)
        print(f"{'Місяць':<9}{'діб':>5}{'DAM грн/добу':>15}{'+БР грн/добу':>15}{'uplift %':>11}")
        for m in sorted(per_month):
            s_dam, s_st, cnt = per_month[m]
            up_pct = (s_st/s_dam - 1)*100 if s_dam > 0 else 0
            print(f"{m:<9}{cnt:>5}{s_dam/cnt:>15,.0f}{s_st/cnt:>15,.0f}{up_pct:>10.1f}%")
        print("-" * 64)
        print(f"{'Рік':<9}{'діб':>5}{'DAM грн/добу':>15}{'+БР грн/добу':>15}{'uplift %':>11}")
        for y in sorted(per_year):
            s_dam, s_st, cnt = per_year[y]
            up_pct = (s_st/s_dam - 1)*100 if s_dam > 0 else 0
            print(f"{y:<9}{cnt:>5}{s_dam/cnt:>15,.0f}{s_st/cnt:>15,.0f}{up_pct:>10.1f}%")
        print("=" * 64)
        if nd:
            avg_dam = dam_total/nd
            avg_st = stacked_total/nd
            print(f"Сер. прибуток:    DAM {avg_dam:,.0f} → +БР {avg_st:,.0f} грн/добу "
                  f"(+{(avg_st/avg_dam-1)*100:.1f}%)")
            annual_dam = avg_dam * 365
            annual_st = avg_st * 365
            print(f"Річний (на батарею): DAM {annual_dam:,.0f} → +БР {annual_st:,.0f} грн/рік")
            print(f"  На 1 МВт·год: DAM {annual_dam/a.energy:,.0f} → "
                  f"+БР {annual_st/a.energy:,.0f} грн/рік")
            if eur:
                print(f"  В EUR (курс {eur:.1f}): DAM {annual_dam/a.energy/eur:,.0f} → "
                      f"+БР {annual_st/a.energy/eur:,.0f} EUR/МВт·год/рік")
        print("Конвенція БР: marg_up — виручка, marg_down — витрата. Перфект-форсайт,")
        print("без активаційного ризику, без РДП-резервів (потужнісних виплат).")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
Синтетична UA-форвардна крива — структурна модель на місячних агрегатах.

Логіка: для трейдингу/хеджування важливі MIДЛО-rivni (месяць/квартал/рік-уперед),
не кожна година. Місячне агрегування знижує шум регіме-шіфтів і дозволяє
параметричній моделі калібруватись на ~36 точках.

  log(DAM_monthly) = α + β1·log(TTF) + β2·log(FX_USD)
                      + β3·trend + цикл-сезонна + ε

Три цілі: baseload (середнє всіх годин), peak (8-20 робочі дні),
evening (19-22, де UA-маржинал — газ).

Вихід:
  • коеф β (інтерпретовані як еластичності: TTF+10% → DAM ×_____)
  • історичний fit (R², MAPE, по місяцях)
  • forward-крива на 12 міс при сценарії (за замовч.: останні значення TTF/FX)
  • чутливості: шок TTF ±20%, FX ±10% → річний середній

Запуск: python forward_curve.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_percentage_error, r2_score


QUERY = """
WITH dam_mo AS (
  SELECT date_trunc('month', delivery_date)::date AS month,
         AVG(buy_price)::float AS dam_base,
         AVG(buy_price) FILTER (
            WHERE delivery_hour BETWEEN 9 AND 20
              AND EXTRACT(dow FROM delivery_date) BETWEEN 1 AND 5
         )::float AS dam_peak,
         AVG(buy_price) FILTER (
            WHERE delivery_hour BETWEEN 19 AND 22
         )::float AS dam_evening,
         COUNT(*) AS n_hours
  FROM dam_clearing
  WHERE zone='IPS' AND buy_price > 0
  GROUP BY 1
),
fuels AS (
  SELECT month::date,
         MAX(value) FILTER (WHERE commodity='gas_europe')::float    AS ttf,
         MAX(value) FILTER (WHERE commodity='coal_australia')::float AS coal_au,
         MAX(value) FILTER (WHERE commodity='oil_brent')::float      AS brent
  FROM commodity_prices
  GROUP BY 1
),
fx_mo AS (
  SELECT date_trunc('month', rate_date)::date AS month,
         AVG(rate) FILTER (WHERE currency='EUR')::float AS fx_eur,
         AVG(rate) FILTER (WHERE currency='USD')::float AS fx_usd
  FROM fx_rates
  GROUP BY 1
)
SELECT d.month, d.dam_base, d.dam_peak, d.dam_evening, d.n_hours,
       f.ttf, f.coal_au, f.brent,
       x.fx_eur, x.fx_usd
FROM dam_mo d
LEFT JOIN fuels f ON f.month = d.month
LEFT JOIN fx_mo x ON x.month = d.month
WHERE d.month >= '2023-01-01' AND d.n_hours > 500
ORDER BY d.month;
"""


async def fetch(dsn: str) -> pd.DataFrame:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(QUERY)
    finally:
        await pool.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df['month'] = pd.to_datetime(df['month'])
    return df


FEATURES = ['log_ttf', 'log_fx_usd', 't', 'sin1', 'cos1', 'sin2', 'cos2']


def build_X(df: pd.DataFrame, t0: pd.Timestamp) -> tuple[pd.DataFrame, np.ndarray]:
    df = df.copy()
    df['log_ttf']    = np.log(df['ttf'])
    df['log_fx_usd'] = np.log(df['fx_usd'])
    df['t'] = (df['month'] - t0).dt.days / 365.25
    m = df['month'].dt.month
    df['sin1'] = np.sin(2*np.pi*m/12); df['cos1'] = np.cos(2*np.pi*m/12)
    df['sin2'] = np.sin(4*np.pi*m/12); df['cos2'] = np.cos(4*np.pi*m/12)
    X = np.column_stack([np.ones(len(df)), df[FEATURES].values])
    return df, X


def fit_ols(X: np.ndarray, y: np.ndarray):
    """Manual OLS з SE/t-stat."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    n, p = X.shape
    yhat = X @ beta
    resid = y - yhat
    sigma2 = (resid @ resid) / max(n - p, 1)
    XtX_inv = np.linalg.inv(X.T @ X)
    se = np.sqrt(sigma2 * np.diag(XtX_inv))
    t_stat = beta / se
    return beta, se, t_stat, yhat


def fit_target(df: pd.DataFrame, target: str, t0: pd.Timestamp):
    sub = df.dropna(subset=['ttf', 'fx_usd', target]).copy()
    sub_x, X = build_X(sub, t0)
    y = np.log(sub_x[target].values)
    beta, se, t_stat, yhat = fit_ols(X, y)
    pred = np.exp(yhat)
    actual = sub_x[target].values
    return {
        'sub': sub_x, 'beta': beta, 'se': se, 't_stat': t_stat,
        'pred': pred, 'actual': actual,
        'r2_log': r2_score(y, yhat),
        'r2_price': r2_score(actual, pred),
        'mape': mean_absolute_percentage_error(actual, pred) * 100,
        'target': target, 'n': len(sub_x),
    }


def print_coefs(res):
    print(f'\n--- {res["target"]} ---  N={res["n"]}  '
          f'R²(log)={res["r2_log"]:.3f}  R²(price)={res["r2_price"]:.3f}  '
          f'MAPE={res["mape"]:.1f}%')
    labels = ['const'] + FEATURES
    print(f'  {"feature":<14}{"β":>10}{"SE":>10}{"t":>8}   інтерпретація')
    for lab, b, s, t in zip(labels, res['beta'], res['se'], res['t_stat']):
        sig = '***' if abs(t) > 2.5 else ('**' if abs(t) > 2.0 else ('*' if abs(t) > 1.5 else ''))
        if lab.startswith('log_'):
            interp = f'еласт.: +10% → DAM ×{1.1**b:.3f}'
        elif lab == 't':
            interp = f'річний тренд ×{np.exp(b):.3f}'
        elif lab in ('sin1', 'cos1', 'sin2', 'cos2'):
            interp = 'сезонна циклічна'
        else:
            interp = 'базовий рівень'
        print(f'  {lab:<14}{b:>10.3f}{s:>10.3f}{t:>8.2f}  {interp} {sig}')


def project(res, t0: pd.Timestamp, scenario: dict, months: int = 12):
    """Forward-крива: підставляємо сценарні значення TTF/FX, ідемо 12 міс уперед."""
    start = scenario['start_month']
    rows = []
    for k in range(months):
        target_m = start + pd.DateOffset(months=k)
        t = (target_m - t0).days / 365.25
        m = target_m.month
        x = np.array([
            1.0,
            np.log(scenario['ttf']),
            np.log(scenario['fx_usd']),
            t,
            np.sin(2*np.pi*m/12), np.cos(2*np.pi*m/12),
            np.sin(4*np.pi*m/12), np.cos(4*np.pi*m/12),
        ])
        rows.append({'month': target_m.strftime('%Y-%m'),
                     'pred': np.exp(x @ res['beta'])})
    return pd.DataFrame(rows)


def main():
    dsn = os.environ.get('OREE_DSN',
                          'postgresql://oree:postgres@localhost:5432/oree')
    print('Завантажую місячні агрегати…')
    df = asyncio.run(fetch(dsn))
    print(f'Місяців у датасеті: {len(df)}')
    print(f'Період: {df["month"].min():%Y-%m} → {df["month"].max():%Y-%m}')
    t0 = df['month'].min()

    print('\nСтан фундаментальних драйверів:')
    print(f'  TTF gas: {df["ttf"].min():.1f} → {df["ttf"].max():.1f} (поточн. {df["ttf"].iloc[-1]:.1f} $/mmbtu)')
    print(f'  Brent  : {df["brent"].min():.1f} → {df["brent"].max():.1f} (поточн. {df["brent"].iloc[-1]:.1f} $/bbl)')
    print(f'  FX USD : {df["fx_usd"].min():.2f} → {df["fx_usd"].max():.2f} (поточн. {df["fx_usd"].iloc[-1]:.2f} UAH/USD)')
    print(f'  FX EUR : {df["fx_eur"].min():.2f} → {df["fx_eur"].max():.2f} (поточн. {df["fx_eur"].iloc[-1]:.2f} UAH/EUR)')
    print(f'  DAM base: {df["dam_base"].min():.0f} → {df["dam_base"].max():.0f} (поточн. {df["dam_base"].iloc[-1]:.0f} грн)')

    print('\n' + '=' * 70)
    print('СТРУКТУРНІ МОДЕЛІ — еластичності DAM до палива/FX/часу/сезону')
    print('=' * 70)
    results = {}
    for target in ['dam_base', 'dam_peak', 'dam_evening']:
        if df[target].notna().sum() >= 12:
            res = fit_target(df, target, t0)
            results[target] = res
            print_coefs(res)

    # historical fit table for baseload (показуємо ОДИН — для решти тільки метрики)
    res = results.get('dam_base')
    if res is not None:
        print(f'\nFit vs actual ({res["target"]}):')
        print(f"{'місяць':>10}{'actual':>10}{'pred':>10}{'err %':>8}")
        for _, r in res['sub'].iterrows():
            pred = np.exp(np.r_[1, r[FEATURES].values] @ res['beta'])
            actual = r[res['target']]
            err = (pred/actual - 1) * 100
            print(f"{r['month'].strftime('%Y-%m'):>10}{actual:>10,.0f}{pred:>10,.0f}{err:>7.1f}%")

    # Forward curve scenario: останні TTF/FX тримаємо плоско на 12 міс
    if results:
        last = df.iloc[-1]
        last_month = pd.to_datetime(last['month'])
        scen_base = {
            'start_month': last_month + pd.DateOffset(months=1),
            'ttf': last['ttf'], 'fx_usd': last['fx_usd'],
        }
        print('\n' + '=' * 70)
        print(f'FORWARD CURVE — 12 міс (сценарій: TTF {scen_base["ttf"]:.1f} $/mmbtu, '
              f'FX {scen_base["fx_usd"]:.2f}, плоско)')
        print('=' * 70)
        rows = []
        for tgt, res in results.items():
            fc = project(res, t0, scen_base)
            fc = fc.rename(columns={'pred': tgt})
            rows.append(fc.set_index('month')[tgt])
        forward = pd.concat(rows, axis=1).reset_index()
        print(forward.to_string(index=False, float_format=lambda v: f'{v:,.0f}'))

        # Чутливості — річний середній baseload при шоках
        print('\n' + '=' * 70)
        print('ЧУТЛИВОСТІ — річний середн. baseload при шоках TTF / FX')
        print('=' * 70)
        rb = results['dam_base']
        baseline = project(rb, t0, scen_base)['pred'].mean()
        print(f"{'сценарій':<32}{'річн. серед.':>15}{'Δ vs base':>13}")
        print(f"{'базовий (плоско)':<32}{baseline:>15,.0f}{'—':>13}")
        for shock_lab, mod in [
            ('TTF +20%', {'ttf': last['ttf']*1.2, 'fx_usd': last['fx_usd']}),
            ('TTF -20%', {'ttf': last['ttf']*0.8, 'fx_usd': last['fx_usd']}),
            ('FX USD +10%', {'ttf': last['ttf'], 'fx_usd': last['fx_usd']*1.1}),
            ('FX USD -10%', {'ttf': last['ttf'], 'fx_usd': last['fx_usd']*0.9}),
            ('TTF+20% & FX+10%', {'ttf': last['ttf']*1.2, 'fx_usd': last['fx_usd']*1.1}),
        ]:
            sc = {**scen_base, **mod}
            v = project(rb, t0, sc)['pred'].mean()
            d = (v/baseline - 1) * 100
            print(f"{shock_lab:<32}{v:>15,.0f}{d:>12.1f}%")
        print('\nПрим: чутливість симетрична за конструкцією (log-log). На реальному ринку')
        print('можуть бути нелінійності (стелі НКРЕКП обмежують зверху).')
    return 0


if __name__ == '__main__':
    sys.exit(main())

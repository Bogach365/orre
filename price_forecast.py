"""
Прогнозна модель ціни DAM (UA, IPS) — Phase 1: explanatory.

Що показує: ХТО рухає ціну, наскільки точно ми її пояснюємо, де похибки.
Не повноцінний deploy-forecast (фактичні перетоки — leakage для real-time;
для day-ahead треба flow forecast). Це structural baseline.

Цільова: ln(DAM IPS buy_price), грн/МВт·год.
Train: 2023-01-01 → 2025-12-31 | Test: 2026-01-01 → ...

Запуск:
  pip install pandas scikit-learn
  python price_forecast.py
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_percentage_error, r2_score


QUERY = """
WITH
wx AS (
  SELECT delivery_date, delivery_hour,
         AVG(ghi)::float    AS ghi,
         AVG(temp_c)::float AS temp
  FROM weather_hourly GROUP BY 1, 2
),
flows AS (
  SELECT delivery_date, delivery_hour,
         SUM(CASE WHEN direction='import' THEN mw ELSE -mw END)::float AS net_import
  FROM cross_border_flows GROUP BY 1, 2
),
neigh AS (
  SELECT delivery_date, delivery_hour, AVG(price)::float AS neigh_eur
  FROM neighbor_prices GROUP BY 1, 2
),
fx AS (
  SELECT rate_date, rate::float AS eur_rate
  FROM fx_rates WHERE currency='EUR'
),
gas AS (
  SELECT month, value::float AS gas_eu
  FROM commodity_prices WHERE commodity='gas_europe'
)
SELECT
  d.delivery_date  AS d,
  d.delivery_hour  AS h,
  d.buy_price::float AS price,
  wx.ghi,
  wx.temp,
  flows.net_import,
  neigh.neigh_eur,
  fx.eur_rate,
  gas.gas_eu
FROM dam_clearing d
LEFT JOIN wx    ON wx.delivery_date    = d.delivery_date AND wx.delivery_hour    = d.delivery_hour - 1
LEFT JOIN flows ON flows.delivery_date = d.delivery_date AND flows.delivery_hour = d.delivery_hour - 1
LEFT JOIN neigh ON neigh.delivery_date = d.delivery_date AND neigh.delivery_hour = d.delivery_hour - 1
LEFT JOIN fx    ON fx.rate_date        = d.delivery_date
LEFT JOIN gas   ON gas.month           = date_trunc('month', d.delivery_date)::date
WHERE d.zone='IPS' AND d.buy_price IS NOT NULL AND d.buy_price > 0
  AND d.delivery_date >= '2023-01-01'
ORDER BY d.delivery_date, d.delivery_hour;
"""

FEATURES = ['h', 'dow', 'month', 'doy', 'ghi', 'temp',
            'net_import', 'neigh_eur', 'neigh_uah',
            'eur_rate', 'gas_eu', 'price_lag_24']


async def fetch(dsn: str) -> pd.DataFrame:
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(QUERY)
    finally:
        await pool.close()
    return pd.DataFrame([dict(r) for r in rows])


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['d', 'h']).reset_index(drop=True)
    df['d'] = pd.to_datetime(df['d'])
    df['dow']   = df['d'].dt.dayofweek
    df['month'] = df['d'].dt.month
    df['doy']   = df['d'].dt.dayofyear
    # forward-fill повільні фічі (gas щомісяця, FX може пропускати вихідні)
    df['gas_eu']   = df['gas_eu'].ffill()
    df['eur_rate'] = df['eur_rate'].ffill()
    # ghi/temp NaN заповнимо нулем (нічні години / відсутні дані)
    df['ghi']  = df['ghi'].fillna(0)
    df['temp'] = df['temp'].fillna(df['temp'].mean())
    df['neigh_uah'] = df['neigh_eur'] * df['eur_rate']
    # лаг тієї ж години вчора
    df = df.sort_values(['h', 'd']).reset_index(drop=True)
    df['price_lag_24'] = df.groupby('h')['price'].shift(1)
    df = df.sort_values(['d', 'h']).reset_index(drop=True)
    df['log_price'] = np.log(df['price'])
    return df


def run_model(df: pd.DataFrame, split_date: str = '2026-01-01'):
    df_ok = df.dropna(subset=FEATURES + ['log_price']).copy()
    split = pd.to_datetime(split_date)
    train = df_ok[df_ok['d'] <  split]
    test  = df_ok[df_ok['d'] >= split]

    X_tr, y_tr = train[FEATURES], train['log_price']
    X_te, y_te = test[FEATURES],  test['log_price']

    model = HistGradientBoostingRegressor(
        max_iter=500, learning_rate=0.05, max_depth=8,
        l2_regularization=0.1, random_state=42,
    )
    model.fit(X_tr, y_tr)

    pred_log = model.predict(X_te)
    pred = np.exp(pred_log)
    actual = test['price'].values

    mape = mean_absolute_percentage_error(actual, pred) * 100
    r2 = r2_score(actual, pred)

    # permutation importance на семплі (швидше)
    n_sample = min(5000, len(test))
    samp = test.sample(n_sample, random_state=42)
    pi = permutation_importance(
        model, samp[FEATURES], samp['log_price'],
        n_repeats=3, random_state=42, n_jobs=-1,
    )
    fi = pd.DataFrame({'feature': FEATURES, 'imp': pi.importances_mean})
    fi = fi.sort_values('imp', ascending=False).reset_index(drop=True)

    return model, train, test, pred, actual, mape, r2, fi


def main() -> int:
    dsn = os.environ.get('OREE_DSN',
                          'postgresql://oree:postgres@localhost:5432/oree')
    print('Завантажую дані з БД…')
    df = asyncio.run(fetch(dsn))
    print(f'  усього годин: {len(df):,}')
    df = build_features(df)

    model, train, test, pred, actual, mape, r2, fi = run_model(df)

    print('\n' + '=' * 60)
    print('ПРОГНОЗНА МОДЕЛЬ DAM (IPS) — Phase 1 explanatory')
    print('=' * 60)
    print(f'Train: {train["d"].min():%Y-%m-%d} → {train["d"].max():%Y-%m-%d}  '
          f'(N={len(train):,})')
    print(f'Test : {test["d"].min():%Y-%m-%d} → {test["d"].max():%Y-%m-%d}  '
          f'(N={len(test):,})')
    print(f'\nЯкість на тесті:')
    print(f'  MAPE: {mape:5.1f}%')
    print(f'  R²:   {r2:.3f}')

    print('\nВажливість фіч (permutation, log-price):')
    for _, row in fi.iterrows():
        bar = '█' * max(1, int(row["imp"] * 50 / max(fi["imp"].max(), 1e-9)))
        print(f'  {row["feature"]:<14} {row["imp"]:7.4f}  {bar}')

    # actual vs pred по годинах доби
    tw = test.copy()
    tw['pred'] = pred
    by_h = tw.groupby('h').agg(actual=('price', 'mean'),
                                pred=('pred', 'mean')).reset_index()
    by_h['err_pct'] = (by_h['pred'] / by_h['actual'] - 1) * 100
    print('\nСередня ціна по годинах доби (тест-вибірка):')
    print(f"{'год':>4}{'actual':>10}{'pred':>10}{'err %':>10}")
    for _, r in by_h.iterrows():
        print(f"{int(r['h']):>4}{r['actual']:>10,.0f}{r['pred']:>10,.0f}{r['err_pct']:>9.1f}%")

    # помилка по місяцях (видно драфт)
    tw['ym'] = tw['d'].dt.to_period('M')
    by_m = tw.groupby('ym').agg(actual=('price', 'mean'),
                                 pred=('pred', 'mean')).reset_index()
    by_m['err_pct'] = (by_m['pred'] / by_m['actual'] - 1) * 100
    print('\nПо місяцях тесту:')
    print(f"{'місяць':>10}{'actual':>10}{'pred':>10}{'err %':>10}")
    for _, r in by_m.iterrows():
        print(f"{str(r['ym']):>10}{r['actual']:>10,.0f}{r['pred']:>10,.0f}{r['err_pct']:>9.1f}%")

    print('\nПриміт: фактичні перетоки = leakage для real-time прогнозу.')
    print('Phase 2: замінимо на forecast-фічі для day-ahead deploy.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

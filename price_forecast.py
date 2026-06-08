"""
Прогнозна модель ціни DAM (UA, IPS) — Phase 1b: виправлена.

Зміни проти 1a:
  * target у рівнях UAH (не log) — уникаємо bias-amplification через exp().
  * додано lag_168 (тиждень тому), синус/косинус години/дня/тижня.
  * м'якша імпутація (не drop) — більше тренувальних даних.
  * додано LINEAR baseline (екстраполює, на відміну від дерев) — щоб діагностувати
    drift train→test.
  * друкуються TRAIN-метрики поряд із test → видно, чи модель пасе свої власні дані.

Запуск:
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
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_percentage_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


QUERY = """
WITH wx AS (
  SELECT delivery_date, delivery_hour,
         AVG(ghi)::float AS ghi, AVG(temp_c)::float AS temp
  FROM weather_hourly GROUP BY 1, 2
), flows AS (
  SELECT delivery_date, delivery_hour,
         SUM(CASE WHEN direction='import' THEN mw ELSE -mw END)::float AS net_import
  FROM cross_border_flows GROUP BY 1, 2
), neigh AS (
  SELECT delivery_date, delivery_hour, AVG(price)::float AS neigh_eur
  FROM neighbor_prices GROUP BY 1, 2
), fx AS (
  SELECT rate_date, rate::float AS eur_rate FROM fx_rates WHERE currency='EUR'
), gas AS (
  SELECT month, value::float AS gas_eu FROM commodity_prices WHERE commodity='gas_europe'
)
SELECT d.delivery_date AS d, d.delivery_hour AS h, d.buy_price::float AS price,
       wx.ghi, wx.temp, flows.net_import, neigh.neigh_eur,
       fx.eur_rate, gas.gas_eu
FROM dam_clearing d
LEFT JOIN wx    ON wx.delivery_date=d.delivery_date    AND wx.delivery_hour=d.delivery_hour-1
LEFT JOIN flows ON flows.delivery_date=d.delivery_date AND flows.delivery_hour=d.delivery_hour-1
LEFT JOIN neigh ON neigh.delivery_date=d.delivery_date AND neigh.delivery_hour=d.delivery_hour-1
LEFT JOIN fx    ON fx.rate_date=d.delivery_date
LEFT JOIN gas   ON gas.month=date_trunc('month', d.delivery_date)::date
WHERE d.zone='IPS' AND d.buy_price IS NOT NULL AND d.buy_price > 0
  AND d.delivery_date >= '2023-01-01'
ORDER BY d.delivery_date, d.delivery_hour;
"""

FEATURES = ['h', 'dow', 'month', 'doy',
            'h_sin', 'h_cos', 'doy_sin', 'doy_cos', 'dow_sin', 'dow_cos',
            'ghi', 'temp', 'net_import',
            'neigh_eur', 'neigh_uah', 'eur_rate', 'gas_eu',
            'price_lag_24', 'price_lag_168']


async def fetch(dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            rows = await c.fetch(QUERY)
    finally:
        await pool.close()
    return pd.DataFrame([dict(r) for r in rows])


def build_features(df):
    df = df.sort_values(['d', 'h']).reset_index(drop=True)
    df['d'] = pd.to_datetime(df['d'])
    df['dow']   = df['d'].dt.dayofweek
    df['month'] = df['d'].dt.month
    df['doy']   = df['d'].dt.dayofyear
    df['h_sin']   = np.sin(2*np.pi*df['h']/24);   df['h_cos']   = np.cos(2*np.pi*df['h']/24)
    df['doy_sin'] = np.sin(2*np.pi*df['doy']/365); df['doy_cos'] = np.cos(2*np.pi*df['doy']/365)
    df['dow_sin'] = np.sin(2*np.pi*df['dow']/7);  df['dow_cos'] = np.cos(2*np.pi*df['dow']/7)
    for col in ['gas_eu', 'eur_rate']:
        df[col] = df[col].ffill().bfill()
        df[col] = df[col].fillna(df[col].median())
    df['ghi']        = df['ghi'].fillna(0)
    df['temp']       = df['temp'].fillna(df['temp'].median())
    df['net_import'] = df['net_import'].fillna(0)
    df['neigh_eur']  = df['neigh_eur'].fillna(df['neigh_eur'].median())
    df['neigh_uah']  = df['neigh_eur'] * df['eur_rate']
    df = df.sort_values(['h', 'd']).reset_index(drop=True)
    df['price_lag_24']  = df.groupby('h')['price'].shift(1)
    df['price_lag_168'] = df.groupby('h')['price'].shift(7)
    df = df.sort_values(['d', 'h']).reset_index(drop=True)
    med = df['price'].median()
    df['price_lag_24']  = df['price_lag_24'].fillna(med)
    df['price_lag_168'] = df['price_lag_168'].fillna(med)
    return df


def eval_model(model, X, y):
    pred = np.clip(model.predict(X), 1, None)
    return mean_absolute_percentage_error(y, pred) * 100, r2_score(y, pred), pred


def main():
    dsn = os.environ.get('OREE_DSN',
                          'postgresql://oree:postgres@localhost:5432/oree')
    print('Завантажую дані…')
    df = asyncio.run(fetch(dsn))
    print(f'  усього годин (raw): {len(df):,}')
    df = build_features(df)
    df_ok = df.dropna(subset=['price']).copy()
    print(f'  після фіч: {len(df_ok):,}')

    split = pd.to_datetime('2026-01-01')
    train = df_ok[df_ok['d'] < split]
    test  = df_ok[df_ok['d'] >= split]
    print(f'  train: {train["d"].min():%Y-%m-%d} → {train["d"].max():%Y-%m-%d} '
          f'(N={len(train):,}, сер.ціна {train["price"].mean():.0f})')
    print(f'  test : {test["d"].min():%Y-%m-%d} → {test["d"].max():%Y-%m-%d} '
          f'(N={len(test):,}, сер.ціна {test["price"].mean():.0f})')

    X_tr, y_tr = train[FEATURES], train['price'].values
    X_te, y_te = test[FEATURES],  test['price'].values

    gb = HistGradientBoostingRegressor(
        max_iter=600, learning_rate=0.05, max_depth=8,
        l2_regularization=0.1, random_state=42)
    gb.fit(X_tr, y_tr)
    gb_tr_mape, gb_tr_r2, _ = eval_model(gb, X_tr, y_tr)
    gb_te_mape, gb_te_r2, gb_te_pred = eval_model(gb, X_te, y_te)

    lin = Pipeline([('sc', StandardScaler()),
                    ('m', Ridge(alpha=1.0, random_state=42))])
    lin.fit(X_tr, y_tr)
    lin_tr_mape, lin_tr_r2, _ = eval_model(lin, X_tr, y_tr)
    lin_te_mape, lin_te_r2, lin_pred = eval_model(lin, X_te, y_te)

    print('\n' + '=' * 64)
    print('РЕЗУЛЬТАТИ — Phase 1b (target=ціна UAH, рівні)')
    print('=' * 64)
    print(f'{"":30}{"TRAIN":>15}{"TEST":>15}')
    print(f'{"HistGB MAPE":30}{gb_tr_mape:>14.1f}%{gb_te_mape:>14.1f}%')
    print(f'{"HistGB R²":30}{gb_tr_r2:>15.3f}{gb_te_r2:>15.3f}')
    print(f'{"Linear Ridge MAPE":30}{lin_tr_mape:>14.1f}%{lin_te_mape:>14.1f}%')
    print(f'{"Linear Ridge R²":30}{lin_tr_r2:>15.3f}{lin_te_r2:>15.3f}')

    print(f'\nСередні (UAH):')
    print(f'  train actual    = {y_tr.mean():.0f}')
    print(f'  test  actual    = {y_te.mean():.0f}')
    print(f'  HistGB test pred= {gb_te_pred.mean():.0f}')
    print(f'  Linear test pred= {lin_pred.mean():.0f}')

    n_samp = min(5000, len(test))
    samp = test.sample(n_samp, random_state=42)
    pi = permutation_importance(gb, samp[FEATURES], samp['price'].values,
                                 n_repeats=3, random_state=42, n_jobs=-1)
    fi = pd.DataFrame({'feature': FEATURES, 'imp': pi.importances_mean})
    fi = fi.sort_values('imp', ascending=False).reset_index(drop=True)
    print(f'\nВажливість фіч (HistGB, permutation; усі {len(fi)}):')
    max_imp = max(fi['imp'].max(), 1e-9)
    for _, r in fi.iterrows():
        bar = '█' * max(1, int(r["imp"] * 40 / max_imp))
        print(f'  {r["feature"]:<14} {r["imp"]:>12.0f}  {bar}')

    tw = test.copy(); tw['pred_gb'] = gb_te_pred; tw['pred_lin'] = lin_pred
    by_h = tw.groupby('h').agg(actual=('price','mean'),
                                pred_gb=('pred_gb','mean'),
                                pred_lin=('pred_lin','mean')).reset_index()
    print('\nСередня ціна по годинах доби (test):')
    print(f"{'h':>4}{'actual':>10}{'HistGB':>10}{'Linear':>10}{'GB %':>8}{'Lin %':>8}")
    for _, r in by_h.iterrows():
        eg = (r['pred_gb']/r['actual']-1)*100; el = (r['pred_lin']/r['actual']-1)*100
        print(f"{int(r['h']):>4}{r['actual']:>10,.0f}{r['pred_gb']:>10,.0f}"
              f"{r['pred_lin']:>10,.0f}{eg:>7.0f}%{el:>7.0f}%")

    tw['ym'] = tw['d'].dt.to_period('M')
    by_m = tw.groupby('ym').agg(actual=('price','mean'),
                                 pred_gb=('pred_gb','mean'),
                                 pred_lin=('pred_lin','mean')).reset_index()
    print('\nПо місяцях (test):')
    print(f"{'місяць':>10}{'actual':>10}{'HistGB':>10}{'Linear':>10}{'GB %':>8}{'Lin %':>8}")
    for _, r in by_m.iterrows():
        eg = (r['pred_gb']/r['actual']-1)*100; el = (r['pred_lin']/r['actual']-1)*100
        print(f"{str(r['ym']):>10}{r['actual']:>10,.0f}{r['pred_gb']:>10,.0f}"
              f"{r['pred_lin']:>10,.0f}{eg:>7.0f}%{el:>7.0f}%")
    return 0


if __name__ == '__main__':
    sys.exit(main())

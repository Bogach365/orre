"""
Прогнозна модель ціни DAM (UA, IPS) — Phase 1c: ratio-to-neighbor target.

Основна зміна проти 1b: target = log(price / neigh_uah).
Це знімає дрейф рівня (FX, інфляція, стелі, війна вже відображені в neigh_uah).
Модель прогнозує "ratio до сусідів" (стабільне ~1.2-1.3), а потім
помножується на ПОТОЧНУ neigh_uah → отримуємо ціну в актуальному масштабі.

Аналогічно lag фічі — теж у ratio (lag_ratio_24, lag_ratio_168).

Запуск: python price_forecast.py
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
from sklearn.metrics import mean_absolute_percentage_error, r2_score, mean_absolute_error
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
            'eur_rate', 'gas_eu',
            'lag_ratio_24', 'lag_ratio_168']


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
    df['neigh_uah']  = (df['neigh_eur'] * df['eur_rate']).clip(lower=100)
    # ratio target і lag-фічі у ratio-просторі
    df['ratio'] = df['price'] / df['neigh_uah']
    df['log_ratio'] = np.log(df['ratio'].clip(lower=0.01))
    df = df.sort_values(['h', 'd']).reset_index(drop=True)
    df['lag_ratio_24']  = df.groupby('h')['ratio'].shift(1)
    df['lag_ratio_168'] = df.groupby('h')['ratio'].shift(7)
    df = df.sort_values(['d', 'h']).reset_index(drop=True)
    med_r = df['ratio'].median()
    df['lag_ratio_24']  = df['lag_ratio_24'].fillna(med_r)
    df['lag_ratio_168'] = df['lag_ratio_168'].fillna(med_r)
    return df


def eval_back_to_price(model, X, anchor, y_actual):
    """Модель видає log_ratio → відновлюємо ціну: pred_price = neigh_uah × exp(log_ratio)."""
    pred_log_ratio = model.predict(X)
    pred_price = np.clip(anchor * np.exp(pred_log_ratio), 1, None)
    mape = mean_absolute_percentage_error(y_actual, pred_price) * 100
    r2 = r2_score(y_actual, pred_price)
    mae = mean_absolute_error(y_actual, pred_price)
    return mape, r2, mae, pred_price


def main():
    dsn = os.environ.get('OREE_DSN',
                          'postgresql://oree:postgres@localhost:5432/oree')
    print('Завантажую дані…')
    df = asyncio.run(fetch(dsn))
    print(f'  усього годин (raw): {len(df):,}')
    df = build_features(df)
    df_ok = df.dropna(subset=['price', 'log_ratio']).copy()
    print(f'  після фіч: {len(df_ok):,}')

    split = pd.to_datetime('2026-01-01')
    train = df_ok[df_ok['d'] < split]
    test  = df_ok[df_ok['d'] >= split]
    print(f'  train: {train["d"].min():%Y-%m-%d} → {train["d"].max():%Y-%m-%d} '
          f'(N={len(train):,}, ratio сер. {train["ratio"].mean():.2f})')
    print(f'  test : {test["d"].min():%Y-%m-%d} → {test["d"].max():%Y-%m-%d} '
          f'(N={len(test):,}, ratio сер. {test["ratio"].mean():.2f})')
    print(f'  Дрейф рівня: train сер.ціна {train["price"].mean():.0f} → '
          f'test {test["price"].mean():.0f} (×{test["price"].mean()/train["price"].mean():.2f})')
    print(f'  Дрейф ratio: ×{test["ratio"].mean()/train["ratio"].mean():.2f} '
          '(чим ближче до 1.0 — тим краще ratio-підхід працює)')

    X_tr, y_tr = train[FEATURES], train['log_ratio'].values
    X_te, y_te_logr = test[FEATURES], test['log_ratio'].values
    a_tr, a_te = train['neigh_uah'].values, test['neigh_uah'].values
    p_tr, p_te = train['price'].values,     test['price'].values

    gb = HistGradientBoostingRegressor(
        max_iter=600, learning_rate=0.05, max_depth=8,
        l2_regularization=0.1, random_state=42)
    gb.fit(X_tr, y_tr)
    gb_tr_mape, gb_tr_r2, _, _ = eval_back_to_price(gb, X_tr, a_tr, p_tr)
    gb_te_mape, gb_te_r2, gb_te_mae, gb_te_pred = eval_back_to_price(gb, X_te, a_te, p_te)

    lin = Pipeline([('sc', StandardScaler()), ('m', Ridge(alpha=1.0, random_state=42))])
    lin.fit(X_tr, y_tr)
    lin_tr_mape, lin_tr_r2, _, _ = eval_back_to_price(lin, X_tr, a_tr, p_tr)
    lin_te_mape, lin_te_r2, lin_te_mae, lin_te_pred = eval_back_to_price(lin, X_te, a_te, p_te)

    # Naive baseline: pred = neigh_uah × train ratio mean
    naive_ratio = train['ratio'].mean()
    naive_pred = a_te * naive_ratio
    naive_mape = mean_absolute_percentage_error(p_te, naive_pred) * 100
    naive_r2 = r2_score(p_te, naive_pred)

    print('\n' + '=' * 64)
    print('РЕЗУЛЬТАТИ — Phase 1c (target=log(price/neigh_uah))')
    print('=' * 64)
    print(f'{"":30}{"TRAIN":>15}{"TEST":>15}')
    print(f'{"HistGB MAPE":30}{gb_tr_mape:>14.1f}%{gb_te_mape:>14.1f}%')
    print(f'{"HistGB R²":30}{gb_tr_r2:>15.3f}{gb_te_r2:>15.3f}')
    print(f'{"Linear Ridge MAPE":30}{lin_tr_mape:>14.1f}%{lin_te_mape:>14.1f}%')
    print(f'{"Linear Ridge R²":30}{lin_tr_r2:>15.3f}{lin_te_r2:>15.3f}')
    print(f'{"Naive (anchor × mean_ratio)":30}{" ":15}{naive_mape:>14.1f}%')
    print(f'{"   R²":30}{" ":15}{naive_r2:>15.3f}')

    print(f'\nСередні на тесті (UAH):')
    print(f'  actual           = {p_te.mean():.0f}')
    print(f'  HistGB           = {gb_te_pred.mean():.0f}')
    print(f'  Linear           = {lin_te_pred.mean():.0f}')
    print(f'  Naive (anchor×μ) = {naive_pred.mean():.0f}')

    # importance на ratio-таргеті
    n_samp = min(5000, len(test))
    samp = test.sample(n_samp, random_state=42)
    pi = permutation_importance(gb, samp[FEATURES], samp['log_ratio'].values,
                                 n_repeats=3, random_state=42, n_jobs=-1)
    fi = pd.DataFrame({'feature': FEATURES, 'imp': pi.importances_mean})
    fi = fi.sort_values('imp', ascending=False).reset_index(drop=True)
    print(f'\nВажливість фіч (HistGB, на log_ratio target):')
    max_imp = max(fi['imp'].max(), 1e-9)
    for _, r in fi.iterrows():
        bar = '█' * max(1, int(r["imp"] * 40 / max_imp))
        print(f'  {r["feature"]:<16} {r["imp"]:>8.4f}  {bar}')

    tw = test.copy(); tw['pred_gb'] = gb_te_pred; tw['pred_lin'] = lin_te_pred
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

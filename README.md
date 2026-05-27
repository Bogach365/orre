# OREE DAM curves collector — v0

Fetches Day-Ahead Market supply/demand curves from
[oree.com.ua](https://www.oree.com.ua/index.php/control/results_mo/DAM)
and stores them in PostgreSQL + raw JSON for audit.

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Prepare PostgreSQL (anything 14+ works)
createdb oree
psql oree -f schema.sql

# 3. Configure connection (or pass --dsn)
export OREE_DSN='postgresql://user:pass@localhost:5432/oree'

# 4. Smoke test — one day
python oree_collector.py --start 2026-05-02 -v

# 5. Backfill 3 years (will take ~30-40 min at 1 req/s)
python oree_collector.py --start 2023-05-11 --end 2026-05-10

# 6. Daily incremental (cron at 14:30 Europe/Kyiv for T-1)
python oree_collector.py --start "$(date -d 'yesterday' +%F)"
```

## What you get after backfill

- `dam_clearing` — ~26 000 rows (24 hours × 1095 days × 1 zone)
- `dam_curves` — ~5–10 M points
- `raw/YYYY/MM/DD.json` — full audit trail, never deleted
- `ingestion_log` — operational visibility, find days that need re-run

## Sanity queries

```sql
-- How many days collected, any gaps?
SELECT COUNT(DISTINCT delivery_date),
       MIN(delivery_date),
       MAX(delivery_date)
FROM dam_clearing;

-- Find non-clearing hours (suspicious or interesting)
SELECT delivery_date, delivery_hour, buy_price, sell_price
FROM dam_clearing
WHERE buy_price = 0 OR sell_price = 0
   OR buy_price IS DISTINCT FROM sell_price
ORDER BY delivery_date DESC, delivery_hour;

-- First REMIT detector candidate: micro-atomization (≥5 sell steps within 0.1 грн)
WITH clusters AS (
  SELECT delivery_date, delivery_hour, zone,
         floor(price * 10) / 10.0 AS price_bin,
         COUNT(*) AS step_count
  FROM dam_curves
  WHERE side = 'S' AND price > 1
  GROUP BY 1, 2, 3, 4
)
SELECT delivery_date, delivery_hour, zone, price_bin, step_count
FROM clusters
WHERE step_count >= 5
ORDER BY delivery_date DESC, step_count DESC
LIMIT 100;
```

## Configuration notes

- **User-Agent**: edit `USER_AGENT` in `oree_collector.py` to include your real
  contact. Polite scraping practice and reduces chance of being blocked.
- **Rate limit**: default 1 req/s. Don't go below 0.5 — OREE is a public-good
  site, not Cloudflare-protected commerce.
- **DST**: hours on transition days (last Sun of March, last Sun of October)
  may need special handling. The parser currently skips non-integer hour keys
  and logs a warning — verify on real DST data (e.g. 2024-10-27) and adjust.
- **Block orders**: OREE reports buyPrice ≠ sellPrice when block-order
  clearing leaves residual supply or demand. Keep both prices.

## Known gaps / next steps

1. **Indices endpoint** — separate XHR captures the BASE/PEAK indices. Need
   to find that endpoint (likely `/control/indexes_data/` or similar) and
   add a parallel collector.
2. **ENTSO-E join** — for proper REMIT screening, fetch
   `installed_capacity_by_unit`, `actual_generation`, `UMM` from
   [transparency.entsoe.eu](https://transparency.entsoe.eu) using a free API key.
   Required for the withholding indicator.
3. **Parquet export** — dump `dam_curves` to date-partitioned Parquet daily
   for DuckDB/Polars analytics. PostgreSQL is for the operational store.
4. **Detectors** — write as separate modules reading from PostgreSQL,
   writing into `remit_signals`. Start with: micro-atomization,
   hockey-stick supply, price-spike Z-score.

## Field reference

OREE response structure (one day):

```json
{
  "IPS": {
    "1":  {
      "buy":  [{"x": "3461.8", "y": "399.99"}, ...],
      "sell": [{"x": "0",      "y": "10"},     ...],
      "buyPrice":  "12500",
      "sellPrice": "11500"
    },
    "2":  {...},
    ...
    "24": {...}
  },
  "BEI": []
}
```

- `x` = price level (грн/МВт·год)
- `y` = cumulative volume (МВт·год) at or above (sell) / at or below (buy) that price
- Points come in pairs forming a step function (vertical, then horizontal segment)
- `buyPrice` / `sellPrice` = clearing prices from buyer and seller side
  (equal when clean clearing; differ when partial/block clearing or unmatched market)
- Zone `IPS` = synchronous Ukrainian grid (since 2022-06 the only zone)
- Zone `BEI` = Burshtyn Energy Island, empty array after ENTSO-E integration

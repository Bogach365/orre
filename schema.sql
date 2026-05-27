-- ---------------------------------------------------------------------------
-- OREE DAM data platform — schema v0
-- ---------------------------------------------------------------------------
-- The collector creates these via init_schema(), but this file is the source
-- of truth for production deploys, and it includes additional optimizations
-- (partitioning, monitoring tables, ENTSO-E join keys) that you'll want
-- before the database grows past ~10 GB.
-- ---------------------------------------------------------------------------

-- ----- Hourly clearing results (compact, ~9k rows/year per zone) -----
CREATE TABLE IF NOT EXISTS dam_clearing (
    delivery_date  DATE         NOT NULL,
    delivery_hour  SMALLINT     NOT NULL,
    zone           VARCHAR(10)  NOT NULL,  -- 'IPS' | 'BEI' (BEI empty after 2022-06)
    buy_price      NUMERIC(12,4),          -- грн/МВт·год; NULL or 0 if no clearing
    sell_price     NUMERIC(12,4),
    ingested_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, zone)
);

-- Derived clearing price view: when buy/sell prices agree, that's the MCP.
-- When they diverge (partial / blocked clearing), keep both for analysis.
CREATE OR REPLACE VIEW v_dam_mcp AS
SELECT
    delivery_date,
    delivery_hour,
    zone,
    CASE WHEN buy_price = sell_price THEN buy_price ELSE NULL END AS mcp,
    buy_price,
    sell_price,
    CASE WHEN buy_price IS DISTINCT FROM sell_price THEN TRUE ELSE FALSE END AS divergent,
    CASE WHEN COALESCE(buy_price, 0) = 0 OR COALESCE(sell_price, 0) = 0
         THEN TRUE ELSE FALSE END AS non_clearing
FROM dam_clearing;


-- ----- Curve points (the heavy table — partition by month) -----
-- ~1k-10k points per day → ~1.5-3M rows/year. With 3y backfill: ~5-10M rows.
-- BRIN index is enough given monotonic insertion by date.
CREATE TABLE IF NOT EXISTS dam_curves (
    delivery_date  DATE         NOT NULL,
    delivery_hour  SMALLINT     NOT NULL,
    zone           VARCHAR(10)  NOT NULL,
    side           CHAR(1)      NOT NULL,  -- 'B' buy / 'S' sell
    step_idx       INT          NOT NULL,
    price          NUMERIC(12,4),          -- x-axis: price level
    cum_volume     NUMERIC(14,4),          -- y-axis: cumulative MWh at this step
    PRIMARY KEY (delivery_date, delivery_hour, zone, side, step_idx)
)
-- Uncomment when ready to switch to declarative partitioning. Note: the PK
-- must then include delivery_date as the first column (which it does).
-- PARTITION BY RANGE (delivery_date)
;

CREATE INDEX IF NOT EXISTS idx_dam_curves_date_brin
    ON dam_curves USING BRIN (delivery_date);

-- For REMIT-detector queries: "find all sell-side steps near a given price"
CREATE INDEX IF NOT EXISTS idx_dam_curves_side_price
    ON dam_curves (side, price)
    WHERE side IS NOT NULL;


-- ----- Ingestion log (operational visibility) -----
CREATE TABLE IF NOT EXISTS ingestion_log (
    id             BIGSERIAL    PRIMARY KEY,
    delivery_date  DATE         NOT NULL,
    status         VARCHAR(20)  NOT NULL,   -- 'ok' | 'fail' | 'empty'
    points_count   INT,
    error          TEXT,
    finished_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ingestion_log_date
    ON ingestion_log (delivery_date DESC);


-- ----- REMIT signal store (populated by detector pipeline, not collector) -----
CREATE TABLE IF NOT EXISTS remit_signals (
    signal_id      BIGSERIAL    PRIMARY KEY,
    delivery_date  DATE         NOT NULL,
    delivery_hour  SMALLINT,
    zone           VARCHAR(10),
    detector_name  VARCHAR(60)  NOT NULL,    -- 'hockey_stick', 'micro_atomization', ...
    severity       SMALLINT     NOT NULL,    -- 1..5
    score          NUMERIC(10,4),
    payload        JSONB,                    -- detector-specific evidence
    status         VARCHAR(20)  NOT NULL DEFAULT 'new',  -- 'new', 'reviewed', 'dismissed', 'escalated'
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    reviewed_at    TIMESTAMPTZ,
    reviewer_note  TEXT
);

CREATE INDEX IF NOT EXISTS idx_remit_signals_date
    ON remit_signals (delivery_date DESC, severity DESC);
CREATE INDEX IF NOT EXISTS idx_remit_signals_status
    ON remit_signals (status) WHERE status IN ('new', 'escalated');


-- ----- Convenience: clearing daily aggregates for fast dashboards -----
CREATE OR REPLACE VIEW v_dam_daily AS
SELECT
    delivery_date,
    zone,
    COUNT(*) FILTER (WHERE buy_price = sell_price)              AS hours_cleared,
    COUNT(*) FILTER (WHERE buy_price IS DISTINCT FROM sell_price) AS hours_divergent,
    AVG(NULLIF(buy_price, 0))   AS avg_buy_price,
    AVG(NULLIF(sell_price, 0))  AS avg_sell_price,
    MIN(buy_price)              AS min_buy_price,
    MAX(buy_price)              AS max_buy_price,
    MIN(sell_price)             AS min_sell_price,
    MAX(sell_price)             AS max_sell_price
FROM dam_clearing
GROUP BY delivery_date, zone;


-- ----- Detector reference: "baseload index" approximation -----
-- True baseload index from OREE uses volume-weighted MCP; until you collect
-- the indices endpoint, this is a usable proxy.
CREATE OR REPLACE VIEW v_dam_baseload_proxy AS
SELECT
    delivery_date,
    zone,
    AVG(NULLIF(buy_price, 0)) AS baseload_proxy
FROM dam_clearing
WHERE buy_price IS NOT NULL AND buy_price = sell_price
GROUP BY delivery_date, zone;

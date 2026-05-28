"""
Завантажувач «Маржинальних цін активованої балансуючої енергії» Укренерго
(на завантаження/розвантаження) → PostgreSQL.

Той самий принцип, що br_imbalance_loader: файли качаються вручну,
заливаються в репозиторій, підтягуються (git pull), скрипт парсить
ЛОКАЛЬНІ файли.

Структура файлу (Укренерго, 2026), аркуш «Аркуш1»:
    кол.0 = дата (datetime; лише в першій годині доби → далі merged → None)
    кол.1 = година '00:00 - 01:00'
    кол.2 = маржинальна ціна «Завантаження»   (up-regulation), грн/МВт·год
    кол.3 = маржинальна ціна «Розвантаження» (down-regulation), грн/МВт·год
Зона: «ОЕС України» (IPS).

Використання:
    python br_marginal_loader.py <файл.xlsx | тека> [ще файли...]
    # приклад:  python br_marginal_loader.py .
Середовище:
    OREE_DSN — DSN PostgreSQL
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import sys
from datetime import date, datetime

import asyncpg
import openpyxl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("oree.brm")

OREE_DSN = os.environ.get(
    "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"
)

DDL = """
CREATE TABLE IF NOT EXISTS br_marginal_prices (
    delivery_date  DATE        NOT NULL,
    delivery_hour  SMALLINT    NOT NULL,
    zone           VARCHAR(10) NOT NULL DEFAULT 'IPS',
    marg_up        NUMERIC(12,4),   -- завантаження (up-regulation)
    marg_down      NUMERIC(12,4),   -- розвантаження (down-regulation)
    source_file    TEXT,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, zone)
);
"""

UPSERT = """
INSERT INTO br_marginal_prices
    (delivery_date, delivery_hour, zone, marg_up, marg_down, source_file)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (delivery_date, delivery_hour, zone) DO UPDATE
SET marg_up     = EXCLUDED.marg_up,
    marg_down   = EXCLUDED.marg_down,
    source_file = EXCLUDED.source_file,
    ingested_at = now();
"""

HOUR_RE = re.compile(r"^\s*(\d{1,2}):\d{2}\s*[-–—]\s*\d{1,2}:\d{2}")


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_file(path: str):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = []
    cur_date = None
    fname = os.path.basename(path)

    for row in ws.iter_rows(values_only=True):
        if not row or len(row) < 4:
            continue
        c0, c1 = row[0], row[1]
        if isinstance(c0, datetime):
            cur_date = c0.date()
        elif isinstance(c0, date):
            cur_date = c0
        if not isinstance(c1, str):
            continue
        m = HOUR_RE.match(c1)
        if not m or cur_date is None:
            continue
        hour = int(m.group(1))
        rows.append((
            cur_date, hour, "IPS",
            _f(row[2]),  # завантаження (up)
            _f(row[3]),  # розвантаження (down)
            fname,
        ))
    wb.close()
    return rows


async def main(paths: list[str]) -> int:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "*.xlsx")))
        else:
            files.append(p)
    files = [f for f in files if "marzh" in os.path.basename(f).lower()]
    if not files:
        log.error("не знайдено файлів *marzh*.xlsx")
        return 2

    pool = await asyncpg.create_pool(dsn=OREE_DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(DDL)
        total = 0
        for f in files:
            try:
                rows = parse_file(f)
            except Exception as e:
                log.error("[%s] помилка парсингу: %s", os.path.basename(f), e)
                continue
            if not rows:
                log.warning("[%s] жодного рядка", os.path.basename(f))
                continue
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.executemany(UPSERT, rows)
            total += len(rows)
            log.info("[%s] завантажено %d годин (%s → %s)",
                     os.path.basename(f), len(rows), rows[0][0], rows[-1][0])
        log.info("готово. файлів=%d рядків=%d", len(files), total)
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python br_marginal_loader.py <file.xlsx|dir> [...]")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1:])))

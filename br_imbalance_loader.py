"""
Завантажувач «Фактичних цін небалансів» Укренерго → PostgreSQL.

Чому "loader", а не "collector": сайт ua.energy блокує завантаження з
сервера (Cloudflare, 403) і навіть зовнішні fetch'і. Тому .xlsx-файли
качаються вручну з телефона, заливаються в GitHub-репозиторій і
підтягуються на сервер (git pull), а цей скрипт парсить ЛОКАЛЬНІ файли
й кладе погодинні дані в базу.

Структура файлу (Укренерго, станом на 2026), аркуш «Аркуш1»:
    кол.0 = дата (datetime; лише в першій годині доби — далі merged → None)
    кол.1 = година, напр. '00:00 - 01:00'
    кол.2 = IMSP   — фактична ціна небалансу (грн/МВт·год)
    кол.3 = PDAM   — ціна РДН (грн/МВт·год), для крос-звірки
    кол.4 = ціна платежу за позитивний небаланс
    кол.5 = ціна платежу за негативний небаланс
Одна зона: «ОЕС України» (IPS).

Використання:
    python br_imbalance_loader.py <файл.xlsx | тека> [ще файли...]
    # приклад:  python br_imbalance_loader.py br_raw/
Середовище:
    OREE_DSN — DSN підключення до PostgreSQL

Залежності: openpyxl, asyncpg   (pip install openpyxl)

Примітки / відомі межі:
  * Розрахований лише на формат «ОЕС України» (одна зона) — файли 2022+.
    Старіші файли (2019–2021) з блоком «Бурштин» сюди краще не подавати.
  * День переходу на зимовий час має 25 годин: дубль години 02:00 буде
    перезаписаний (upsert) — втрата ~1 год/рік, прийнятно. За потреби
    уточнимо пізніше.
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
log = logging.getLogger("oree.br")

OREE_DSN = os.environ.get(
    "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"
)

DDL = """
CREATE TABLE IF NOT EXISTS imbalance_prices (
    delivery_date   DATE        NOT NULL,
    delivery_hour   SMALLINT    NOT NULL,
    zone            VARCHAR(10) NOT NULL DEFAULT 'IPS',
    imbalance_price NUMERIC(12,4),   -- IMSP
    dam_price       NUMERIC(12,4),   -- PDAM (крос-звірка)
    pos_payment     NUMERIC(12,4),   -- платіж за позитивний небаланс
    neg_payment     NUMERIC(12,4),   -- платіж за негативний небаланс
    source_file     TEXT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, zone)
);
"""

UPSERT = """
INSERT INTO imbalance_prices
    (delivery_date, delivery_hour, zone, imbalance_price, dam_price,
     pos_payment, neg_payment, source_file)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (delivery_date, delivery_hour, zone) DO UPDATE
SET imbalance_price = EXCLUDED.imbalance_price,
    dam_price       = EXCLUDED.dam_price,
    pos_payment     = EXCLUDED.pos_payment,
    neg_payment     = EXCLUDED.neg_payment,
    source_file     = EXCLUDED.source_file,
    ingested_at     = now();
"""

# приймає '0:00 - 1:00', '00:00 - 01:00', з дефісом або тире
HOUR_RE = re.compile(r"^\s*(\d{1,2}):\d{2}\s*[-–—]\s*\d{1,2}:\d{2}")


def _f(v):
    """OREE/Укренерго інколи дають числа рядком або порожнечу — нормалізуємо."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_file(path: str):
    """Розплющує один xlsx у список кортежів, готових до upsert."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = []
    cur_date = None
    fname = os.path.basename(path)

    for row in ws.iter_rows(values_only=True):
        if not row or len(row) < 6:
            continue
        c0, c1 = row[0], row[1]

        # дата стоїть лише в першій годині доби (merged) — запам'ятовуємо
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
            _f(row[2]),  # IMSP
            _f(row[3]),  # PDAM
            _f(row[4]),  # pos payment
            _f(row[5]),  # neg payment
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
    # беремо лише файли цін небалансів
    files = [f for f in files if "nebalansiv" in os.path.basename(f).lower()]
    if not files:
        log.error("не знайдено файлів *nebalansiv*.xlsx")
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
                log.warning("[%s] жодного рядка не розпарсено", os.path.basename(f))
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
        print("usage: python br_imbalance_loader.py <file.xlsx|dir> [...]")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1:])))

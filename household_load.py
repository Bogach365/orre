"""
Збирач погодинних фактичних обсягів е/е для побутових споживачів (сегмент ПУП).

Підтримує:
  - YASNO (Київщина/Дніпропетровщина) — PDF: static.yasno.ua/.../*.pdf
  - LEZ Львівенергозбут (Львівщина)  — XLSX: api.lez.com.ua/media/*.xlsx
Авто-роутер за розширенням URL: .pdf → pdfplumber, .xlsx → openpyxl.

Структура файлу в обох постачальників однакова:
  таблиця ~31 рядок (день) × 24 стовпці (година доби, МВт·год) + «Всього за добу».

Зберігаємо в household_load(delivery_date, delivery_hour 1..24, supplier, mwh).
Конвенція годин: 1..24 як у dam_clearing/idm. При join із neighbor_prices/flows
(там 0..23) — зсув на 1.

Застереження по даних: «фактичні обсяги» — у періоди ГПВ вечір занижений
відключеннями (supply-constrained), не природний попит. Регіональні сегменти,
не вся країна.

Запуск:
  pip install pdfplumber openpyxl
  python household_load.py --supplier YASNO --urls "https://static.yasno.ua/.../jan.pdf"
  python household_load.py --supplier LEZ   --urls "https://api.lez.com.ua/.../feb.xlsx"
  python household_load.py --supplier LEZ   --url-file lez_urls.txt
"""
from __future__ import annotations

import argparse
import asyncio
import io
import os
import re
import sys
from datetime import date
from urllib.parse import unquote

import asyncpg
import httpx

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import openpyxl
except ImportError:
    openpyxl = None


MONTHS = {
    "січ": 1, "лют": 2, "берез": 3, "квіт": 4, "трав": 5, "черв": 6,
    "лип": 7, "серп": 8, "верес": 9, "жовт": 10, "листоп": 11, "груд": 12,
}

DDL = """
CREATE TABLE IF NOT EXISTS household_load (
    delivery_date DATE        NOT NULL,
    delivery_hour SMALLINT    NOT NULL,
    supplier      VARCHAR(20) NOT NULL,
    mwh           NUMERIC(12,3),
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (delivery_date, delivery_hour, supplier)
);
CREATE INDEX IF NOT EXISTS idx_hl_date ON household_load (delivery_date);
"""

UPSERT = """
INSERT INTO household_load (delivery_date, delivery_hour, supplier, mwh)
VALUES ($1,$2,$3,$4)
ON CONFLICT (delivery_date, delivery_hour, supplier) DO UPDATE
SET mwh = EXCLUDED.mwh, ingested_at = now();
"""


def month_year(text):
    """('за <місяць> <рік>' або просто <місяць> + <рік>) → (year, month) | (None, None)."""
    low = text.lower()
    m = re.search(r"([а-яіїєґ’']+)\s+(20\d\d)", low)
    if m:
        word, year = m.group(1), int(m.group(2))
        for stem, num in MONTHS.items():
            if word.startswith(stem):
                return year, num
    ym = re.search(r"(20\d\d)", low)
    year = int(ym.group(1)) if ym else None
    for stem, num in MONTHS.items():
        if stem in low:
            return year, num
    return year, None


def _valid_row(day, hours, total):
    """Перевірка: сума годин ≈ підсумок (з округленням)."""
    if not (1 <= day <= 31):
        return False
    return abs(sum(hours) - total) <= max(5, 0.03 * abs(total) if total else 5)


# ---------------- PDF (YASNO) ------------------------------------------------

def parse_pdf_text(text, supplier):
    """Текст PDF (pdfplumber) → [(date, hour 1..24, supplier, mwh)]."""
    year, month = month_year(text)
    if not year or not month:
        raise ValueError("PDF: не вдалося визначити місяць/рік")

    rows = []
    for line in text.splitlines():
        nums = re.findall(r"-?\d+", line)
        if len(nums) != 26:
            continue
        ints = [int(x) for x in nums]
        day, hours, total = ints[0], ints[1:25], ints[25]
        if not _valid_row(day, hours, total):
            continue
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        for h, v in enumerate(hours, start=1):
            rows.append((d, h, supplier, float(v)))
    return year, month, rows


def fetch_pdf(url):
    if pdfplumber is None:
        raise RuntimeError("потрібен pdfplumber: pip install pdfplumber")
    r = httpx.get(url, timeout=60.0, headers={"User-Agent": "oree-research/0.1"})
    r.raise_for_status()
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


# ---------------- XLSX (LEZ Львівенергозбут) ---------------------------------

def parse_xlsx(content_bytes, supplier, filename_hint=""):
    """XLSX → rows. Шукаємо рядки, де є int 1..31 і 25 наступних числових клітинок."""
    if openpyxl is None:
        raise RuntimeError("потрібен openpyxl: pip install openpyxl")
    wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)

    # Збираємо текст всіх клітинок для пошуку місяця/року
    text_blob = unquote(filename_hint) + " "
    candidates = []   # (day, hours[24], total)
    seen_days = set()

    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            # текст
            for c in row:
                if isinstance(c, str):
                    text_blob += c + " "
            # пошук дня + 24 год + total у рядку
            for i, cell in enumerate(row):
                if isinstance(cell, (int, float)) and cell == int(cell) and 1 <= cell <= 31:
                    following = row[i + 1: i + 26]
                    if len(following) < 25:
                        continue
                    if not all(isinstance(x, (int, float)) for x in following):
                        continue
                    day = int(cell)
                    hours = [float(x) for x in following[:24]]
                    total = float(following[24])
                    if _valid_row(day, hours, total) and day not in seen_days:
                        candidates.append((day, hours, total))
                        seen_days.add(day)
                        break

    year, month = month_year(text_blob)
    if not year or not month:
        raise ValueError("XLSX: не вдалося визначити місяць/рік (ні з тексту, ні з імені файлу)")

    rows = []
    for day, hours, _ in candidates:
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        for h, v in enumerate(hours, start=1):
            rows.append((d, h, supplier, v))
    return year, month, rows


def fetch_xlsx(url):
    r = httpx.get(url, timeout=60.0, headers={"User-Agent": "oree-research/0.1"},
                  follow_redirects=True)
    r.raise_for_status()
    return r.content


# ---------------- Роутер + збереження ---------------------------------------

def fetch_and_parse(url, supplier):
    lower = url.lower().split("?")[0]
    if lower.endswith(".pdf"):
        text = fetch_pdf(url)
        return parse_pdf_text(text, supplier)
    elif lower.endswith(".xlsx"):
        content = fetch_xlsx(url)
        return parse_xlsx(content, supplier, filename_hint=url)
    else:
        raise ValueError(f"невідоме розширення: {url}")


async def run(urls, supplier, dsn):
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as c:
            await c.execute(DDL)
        total = 0
        for url in urls:
            try:
                year, month, rows = fetch_and_parse(url, supplier)
            except Exception as e:
                print(f"[ПОМИЛКА] {url[:80]}…: {e}")
                continue
            if rows:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(UPSERT, rows)
                total += len(rows)
                days = len({r[0] for r in rows})
                print(f"[OK] {year}-{month:02d} {supplier}: {days} діб, {len(rows)} год.")
            else:
                print(f"[ПУСТО] {url[:80]}…")
        print(f"готово. усього рядків: {total}")
        return 0
    finally:
        await pool.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", help="URL(и) через кому")
    ap.add_argument("--url-file", help="файл зі списком URL")
    ap.add_argument("--supplier", default="YASNO")
    ap.add_argument("--dsn", default=os.environ.get(
        "OREE_DSN", "postgresql://oree:postgres@localhost:5432/oree"))
    a = ap.parse_args()

    urls = []
    if a.urls:
        urls += [u.strip() for u in a.urls.split(",") if u.strip()]
    if a.url_file:
        with open(a.url_file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not urls:
        print("дайте --urls або --url-file")
        return 2
    return asyncio.run(run(urls, a.supplier, a.dsn))


if __name__ == "__main__":
    sys.exit(main())

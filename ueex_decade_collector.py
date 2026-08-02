#!/usr/bin/env python3
"""
ueex_decade_collector.py — збір декадних індексів РДД з УЄББ
"""
import re, subprocess, sys
import httpx

UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
URL = "https://www.ueex.com.ua/rus/exchange-quotations/electric-power/indexes/"

MONTHS_RU = {
    "январь":1,"февраль":2,"март":3,"апрель":4,
    "май":5,"июнь":6,"июль":7,"август":8,
    "сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12
}

def fetch():
    try:
        r = httpx.get(URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"Fetch error: {e}", file=sys.stderr)
        return None

def parse(html):
    pattern = r'<td>(\w+)\s+(\d{4})\s+\(DEC\s+(\d)\)</td>\s*<td>([\d\s&nbsp;,]+)грн'
    results = []
    for m in re.finditer(pattern, html, re.IGNORECASE):
        month_ru = m.group(1).lower()
        year     = int(m.group(2))
        decade   = int(m.group(3))
        price_str = re.sub(r'[&nbsp;\s]', '', m.group(4)).replace(',', '.')
        try:
            price = float(price_str)
            month = MONTHS_RU.get(month_ru)
            if not month:
                continue
            label = f"{year}-{month:02d}-D{decade}"
            results.append((label, year, month, decade, price))
        except ValueError:
            continue
    return results

def query_db(sql):
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree",
           "-t", "-A", "-F", "\t", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    rows = []
    for line in r.stdout.strip("\n").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if all(p in ("", "\\N") for p in parts):
            continue
        rows.append(parts)
    return rows

def save(label, year, month, decade, price):
    sql = (f"INSERT INTO ueex_decade_index "
           f"(period_label, year_num, month_num, decade_num, price_avg) "
           f"VALUES ('{label}', {year}, {month}, {decade}, {price}) "
           f"ON CONFLICT (period_label) DO UPDATE "
           f"SET price_avg=EXCLUDED.price_avg, ingested_at=now();")
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.returncode == 0

def main():
    print("Fetching UEEX decade index...")
    html = fetch()
    if not html:
        sys.exit(1)
    records = parse(html)
    if not records:
        print("No data parsed")
        sys.exit(1)
    print(f"Parsed {len(records)} records")
    ok = skip = 0
    for label, year, month, decade, price in records:
        existing = query_db(f"SELECT price_avg FROM ueex_decade_index WHERE period_label='{label}'")
        if existing and abs(float(existing[0][0]) - price) < 0.01:
            skip += 1
            continue
        if save(label, year, month, decade, price):
            print(f"  [{label}] {price:,.1f} грн/МВт·год")
            ok += 1
    print(f"Done. ok={ok} skip={skip}")
    rows = query_db("""
        SELECT period_label, price_avg
        FROM ueex_decade_index
        ORDER BY year_num DESC, month_num DESC, decade_num DESC
        LIMIT 12
    """)
    if rows:
        print("\nОстанні записи:")
        for r in rows:
            print(f"  {r[0]}: {float(r[1]):,.1f} грн/МВт·год")

if __name__ == "__main__":
    main()

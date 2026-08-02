#!/usr/bin/env python3
import re, subprocess, sys
from datetime import datetime
import httpx

UA  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
URL = "https://www.ueex.com.ua/rus/exchange-quotations/electric-power/"

def fetch_page():
    try:
        r = httpx.get(URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"Fetch error: {e}", file=sys.stderr)
        return None

def parse_chart_data(html):
    dm = re.search(r"categories:\s*\[([^\]]+)\]", html)
    pm = re.search(r"data:\s*\[([0-9.,\s]+)\]", html)
    if not dm or not pm:
        return []
    dates  = re.findall(r"'(\d{2}\.\d{2}\.\d{4})'", dm.group(1))
    prices = [float(p.strip()) for p in pm.group(1).split(",") if p.strip()]
    n = min(len(dates), len(prices))
    result = []
    for d, p in zip(dates[:n], prices[:n]):
        try:
            result.append((datetime.strptime(d, "%d.%m.%Y").date(), round(p, 4)))
        except ValueError:
            continue
    return result

def query_db(sql):
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree", "-t", "-A", "-F", "\t", "-c", sql]
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

def save_record(trade_date, price_avg):
    sql = (f"INSERT INTO ueex_index (trade_date, price_avg) VALUES ('{trade_date}', {price_avg}) "
           f"ON CONFLICT (trade_date) DO UPDATE SET price_avg=EXCLUDED.price_avg, ingested_at=now();")
    cmd = ["docker", "exec", "oree_pg", "psql", "-U", "oree", "-d", "oree", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return r.returncode == 0

def main():
    print("Fetching UEEX index...")
    html = fetch_page()
    if not html:
        sys.exit(1)
    records = parse_chart_data(html)
    if not records:
        print("No data parsed")
        sys.exit(1)
    ok = skip = 0
    for trade_date, price_avg in records:
        if query_db(f"SELECT 1 FROM ueex_index WHERE trade_date='{trade_date}'"):
            skip += 1
            continue
        if save_record(trade_date, price_avg):
            print(f"  [{trade_date}] {price_avg:,.2f} грн/МВт·год")
            ok += 1
        else:
            print(f"  [{trade_date}] DB error", file=sys.stderr)
    print(f"Done. ok={ok} skip={skip}")
    rows = query_db("""
        SELECT u.trade_date, ROUND(u.price_avg::numeric,0),
               ROUND(AVG(d.buy_price)::numeric,0),
               ROUND((u.price_avg - AVG(d.buy_price))::numeric,0)
        FROM ueex_index u
        LEFT JOIN dam_clearing d ON d.delivery_date=u.trade_date AND d.zone='IPS'
        WHERE u.trade_date >= CURRENT_DATE - 14
        GROUP BY u.trade_date, u.price_avg
        ORDER BY u.trade_date DESC LIMIT 7
    """)
    if rows:
        print("\nДата       | УЄББ   | OREE   | Спред")
        print("-" * 42)
        for r in rows:
            oree   = f"{float(r[2]):>6,.0f}" if r[2] and r[2]!='\\N' else "   н/д"
            spread = f"{float(r[3]):>+6,.0f}" if r[3] and r[3]!='\\N' else "   н/д"
            print(f"{r[0]} | {float(r[1]):>6,.0f} | {oree} | {spread}")

if __name__ == "__main__":
    main()

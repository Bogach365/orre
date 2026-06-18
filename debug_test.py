import sys
sys.path.insert(0, '/home/oree')
from dam_chart import query, get_stats

date_str = '2026-06-17'
sql = f"""
        SELECT ROUND(AVG(buy_price)::numeric,0),
               ROUND(AVG(sell_price)::numeric,0),
               ROUND(MIN(buy_price)::numeric,0),
               ROUND(MAX(buy_price)::numeric,0),
               ROUND(MAX(sell_price)::numeric,0),
               COUNT(*),
               ROUND(SUM(cleared_volume)::numeric,0)
        FROM dam_clearing
        WHERE delivery_date='{date_str}' AND zone='IPS'
    """

print("SQL repr:", repr(sql))
result = query(sql)
print("query() result:", result)
print("get_stats() result:", get_stats(date_str))

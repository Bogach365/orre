#!/usr/bin/env bash
# Оновлення погоди по всіх локаціях (останні 10 днів; лаг ERA5 ~5 днів)
set -uo pipefail
cd /home/oree/orre
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
export OREE_DSN="postgresql://oree:${PG_PASSWORD}@localhost:5432/oree"
START="$(date -d '-10 days' +%F)"
while read -r LAT LON LOC; do
  [ -z "$LOC" ] && continue
  python weather_collector.py --start "$START" --lat "$LAT" --lon "$LON" --location "$LOC" || echo "weather $LOC failed"
done <<'LOCS'
49.84 24.03 lviv
48.46 35.05 dnipro
46.97 31.99 mykolaiv
50.45 30.52 kyiv
49.23 28.47 vinnytsia
LOCS

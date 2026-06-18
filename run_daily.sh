#!/usr/bin/env bash
# Daily T-1 collection wrapper. Called by systemd timer.
set -euo pipefail

cd /home/oree/orre
source .venv/bin/activate

export OREE_DSN="postgresql://oree:776a2ddbafd3bb4a975e97aec071a91f@localhost:5432/oree"
export OREE_RAW_DIR="/home/oree/orre/raw"

# DAM results for delivery day T-1 are published the day before delivery,
# but to be safe we collect yesterday's delivery date.
YESTERDAY="$(date -d 'yesterday' +%F)"

python oree_collector.py --start "$YESTERDAY" -v

# Optional: notify via Telegram (uncomment + set vars)
# if [ -n "${TG_TOKEN:-}" ]; then
#   STATUS=$?
#   MSG="OREE collect $YESTERDAY: $([ $STATUS -eq 0 ] && echo OK || echo FAILED)"
#   curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
#        -d chat_id="${TG_CHAT}" -d text="$MSG" >/dev/null
# fi

# --- IDM (ВДР) daily collection (added 2026-05-28) ---
OREE_RAW_DIR="/home/oree/orre/raw_idm" python idm_collector.py --start "$YESTERDAY" -v

# --- FX (НБУ): курс щодня ---
python fx_collector.py --start "$(date -d '-7 days' +%F)" || echo "FX failed"

# --- NBU макро (інфляція + платіжний баланс): upsert не дублює ---
python nbu_macro_collector.py --start "$(date -d '-120 days' +%F)" || echo "NBU macro failed"
python nbu_macro_collector.py --blocks key --period d --start "$(date -d '-30 days' +%F)" || echo "key rate failed"
python wb_commodities.py --url "https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Monthly.xlsx" --start 2021-01-01 || echo "WB commodities failed"
export $(grep -v '^#' /home/oree/orre/.env | xargs) 2>/dev/null
python entsoe_flows.py --start "$(date -d '-7 days' +%F)" || echo "ENTSO-E flows failed"
python entsoe_prices.py --start "$(date -d "-7 days" +%F)" || echo "ENTSO-E prices failed"

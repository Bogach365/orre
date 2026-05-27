#!/usr/bin/env bash
# Daily T-1 collection wrapper. Called by systemd timer.
set -euo pipefail

cd /home/oree/oree
source .venv/bin/activate

export OREE_DSN="postgresql://oree:CHANGE_ME@localhost:5432/oree"
export OREE_RAW_DIR="/home/oree/oree/raw"

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

#!/usr/bin/env bash
# Weekly orchestration — runs Sunday ~5 am ET.
#
# Includes everything daily.sh does, plus:
#   - Crunchbase enrichment for all companies in the store
#     (funding, investors, last round, type/domain hints)
#
# Crunchbase costs add up — this runs weekly to amortize the API
# budget. Set CRUNCHBASE_BACKEND=offline in .env to skip live calls.
#
# Usage:
#   ./cron/weekly.sh
#
# Cron example (Sunday 05:00 ET):
#   0 10 * * 0 cd /opt/defense-aggregator && ./cron/weekly.sh >> logs/weekly.log 2>&1
#
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a; source .env; set +a
fi

mkdir -p logs

stamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
echo "[$(stamp)] weekly start"

# 1. Run daily first (event refresh + discovery)
./cron/daily.sh

# 2. Crunchbase enrichment
python - <<'PYEOF'
import logging
logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
from enrich.crunchbase import enrich_all
summary = enrich_all()
print(f"  crunchbase: {summary}")
PYEOF

echo "[$(stamp)] weekly done"

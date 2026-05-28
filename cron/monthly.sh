#!/usr/bin/env bash
# Monthly orchestration — runs 1st of each month ~4 am ET.
#
# Includes everything weekly.sh does, plus:
#   - Compliance enrichment (FedRAMP + DoD IL + USASpending OT signals)
#   - Builds the seven-section markdown report for the previous month
#   - Archives the report with a timestamped filename
#
# This is the cadence at which the human-facing report ships.
#
# Usage:
#   ./cron/monthly.sh
#
# Cron example (1st of every month at 04:00 ET):
#   0 9 1 * * cd /opt/defense-aggregator && ./cron/monthly.sh >> logs/monthly.log 2>&1
#
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a; source .env; set +a
fi

mkdir -p logs reports/out

stamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
echo "[$(stamp)] monthly start"

# Defaults: skip USASpending and the OT-column/transitions sections it
# powers. Override per-run by exporting these vars before invoking.
: "${SKIP_USASPENDING:=1}"
: "${SKIP_OTA_COLUMN:=$SKIP_USASPENDING}"
: "${SKIP_TRANSITIONS:=1}"
: "${SKIP_NEW_COMPANIES:=0}"
# Outbound-target filter: drop sponsors/judges/mentors + known
# primes/integrators from sections 1-5, 7, monthly tracking.
# Section 6 (ecosystem mapping) always sees the unfiltered data.
: "${TARGET_PARTICIPANTS_ONLY:=1}"
export SKIP_USASPENDING SKIP_OTA_COLUMN SKIP_TRANSITIONS SKIP_NEW_COMPANIES
export TARGET_PARTICIPANTS_ONLY

# 1. Run weekly first (event refresh + discovery + crunchbase)
./cron/weekly.sh

# 2. Compliance enrichment (FedRAMP + DoD IL + USASpending OT signals)
python - <<'PYEOF'
import logging, os
logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
from enrich.compliance import enrich_all
skip = os.getenv("SKIP_USASPENDING", "").lower() in ("1", "true", "yes")
summary = enrich_all(skip_usaspending=skip)
print(f"  compliance: {summary} (skip_usaspending={skip})")
PYEOF

# 3. Build the monthly report — covers the previous 30 days.
#    Honors SKIP_OTA_COLUMN / SKIP_TRANSITIONS / SKIP_NEW_COMPANIES from
#    the env. Also emits CSV + PDF alongside the Markdown.
python - <<'PYEOF'
import logging, os
from datetime import date, timedelta
from pathlib import Path
logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
from reports.build_markdown import build as build_md
from reports.build_xlsx import build as build_xlsx
from reports.build_pdf import build as build_pdf

def _flag(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")

skip_ota = _flag("SKIP_OTA_COLUMN")
skip_trans = _flag("SKIP_TRANSITIONS")
skip_new = _flag("SKIP_NEW_COMPANIES")
target_only = _flag("TARGET_PARTICIPANTS_ONLY")

since = date.today() - timedelta(days=30)
text = build_md(
    since=since,
    skip_ota_column=skip_ota,
    skip_new_companies=skip_new,
    skip_transitions=skip_trans,
    target_participants_only=target_only,
)
out_dir = Path("reports/out")
out_dir.mkdir(parents=True, exist_ok=True)
md_path = out_dir / f"report_{date.today().isoformat()}.md"
md_path.write_text(text, encoding="utf-8")
print(f"  markdown: wrote {md_path} ({len(text):,} chars)")

xlsx_path = build_xlsx(target_participants_only=target_only)
print(f"  xlsx:     wrote {xlsx_path}")

pdf_path = build_pdf(since=since, skip_ota_column=skip_ota,
                     target_participants_only=target_only)
print(f"  pdf:      wrote {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
PYEOF

echo "[$(stamp)] monthly done"

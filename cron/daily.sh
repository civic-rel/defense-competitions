#!/usr/bin/env bash
# Daily orchestration — runs ~6 am ET.
#
# Refreshes the event calendar from all live sources, then runs
# discovery + recap scraping on events in the active window
# (default: dates_end within last 90 days).
#
# Cheap operations only — no Crunchbase, no FedRAMP, no USASpending.
# Those run on weekly/monthly cadences (see weekly.sh, monthly.sh).
#
# Usage:
#   ./cron/daily.sh
#   LOG_LEVEL=DEBUG ./cron/daily.sh
#
# Cron example (run at 06:15 ET every day):
#   15 11 * * * cd /opt/defense-aggregator && ./cron/daily.sh >> logs/daily.log 2>&1
#
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
    # shellcheck disable=SC1091
    set -a; source .env; set +a
fi

mkdir -p logs

stamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
echo "[$(stamp)] daily start"

# 1. Refresh events from all live sources. Each adapter is best-effort.
python - <<'PYEOF'
import logging
logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
from store import cache as store

for mod_name, fn_name, label in [
    ("sources.navalx",             "fetch_events", "navalx"),
    ("sources.darpa",              "fetch_events", "darpa"),
    ("sources.usagov_challenges",  "fetch_events", "usagov"),
    ("sources.sbir_gov",           "fetch_dod_solicitations", "sbir"),
    ("sources.samgov",             "fetch_events", "samgov"),
    # The five WERX-style adapters below all write Participations
    # directly via the recap_scraper (no Brave queries needed),
    # in addition to returning Events. xtech also exposes a
    # separate fetch_participations() that reads the participant
    # sitemap; that's invoked below.
    ("sources.xtech",          "fetch_events", "xtech"),
    ("sources.diu",            "fetch_events", "diu"),
    ("sources.afwerx",         "fetch_events", "afwerx"),
    ("sources.sofwerx",        "fetch_events", "sofwerx"),
    ("sources.defensewerx",    "fetch_events", "defensewerx"),
    # dronedominance.mil: defaults to fixture; set
    # DRONEDOMINANCE_BACKEND=live to actually fetch the .mil page.
    ("sources.dronedominance", "fetch_events", "dronedominance"),
    # github.com: defaults to fixture; set GITHUB_BACKEND=api +
    # GITHUB_TOKEN=<pat> to hit the real GitHub Search API.
    # Returns events that received new participations (no new
    # events created — Events must already exist in the store).
    ("sources.github",         "fetch_events", "github"),
]:
    try:
        mod = __import__(mod_name, fromlist=[fn_name])
        events = getattr(mod, fn_name)()
        for e in events:
            store.upsert_event(e.to_dict())
        print(f"  {label}: {len(events)} events")
    except Exception as exc:
        logging.warning("[%s] failed: %s", label, exc)

# xTech's participant sitemap gives us authoritative participation
# data without Brave. Sync it after fetch_events so the competition
# Events exist for the participations to FK to.
try:
    from sources.xtech import fetch_participations as xtech_fetch_parts
    n = xtech_fetch_parts()
    print(f"  xtech_participations: {n} written")
except Exception as exc:
    logging.warning("[xtech_participations] failed: %s", exc)
PYEOF

# 2. Run discovery + recap scraping on events in the active window.
#    DISCOVERY_WINDOW_DAYS controls how far back we look for events to
#    process (default: 143 days ~ YTD). MAX_QUERIES_PER_SOURCE caps
#    Brave queries against any single editorial source across the
#    whole run (default: 50). Both are env-overridable.
: "${DISCOVERY_WINDOW_DAYS:=143}"
: "${MAX_QUERIES_PER_SOURCE:=50}"
export DISCOVERY_WINDOW_DAYS MAX_QUERIES_PER_SOURCE

python - <<'PYEOF'
import logging, os
from datetime import date, timedelta
logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
from sources.discover import discover_all

window_days = int(os.environ.get("DISCOVERY_WINDOW_DAYS", "143"))
since = date.today() - timedelta(days=window_days)
summaries = discover_all(since=since)
total_parts = sum(s.get("participations_written", 0) for s in summaries)
total_arts = sum(s.get("articles_processed", 0) for s in summaries)
total_capped = sum(s.get("queries_skipped_capped", 0) for s in summaries)
print(f"  discovery: {len(summaries)} events processed, "
      f"{total_arts} articles, {total_parts} participations written, "
      f"{total_capped} queries skipped by cap")
PYEOF

echo "[$(stamp)] daily done"

# defense-aggregator v2 — Production Runbook

## What this is

A monthly market-intelligence aggregator that tracks defense
innovation events (hackathons, TEMs, Industry Days, prize
challenges, SBIR solicitations) and the companies that participate
in them. Each month, it produces a seven-section markdown report
covering competitions, participants, cross-event patterns, stealth
startups, compliance maturity, ecosystem co-occurrence, and a
fully-cited evidence appendix.

The pipeline is **internal-only**. The output report and the
underlying database should not be republished externally without
clearing data-rights with each source — particularly Crunchbase.

## Architecture in one paragraph

Three jobs (`cron/daily.sh`, `cron/weekly.sh`, `cron/monthly.sh`)
write to a single SQLite database at `store/events.sqlite`. Daily
refreshes events from public sources and discovers recap articles
via a configurable search backend (default: Brave). Weekly adds
Crunchbase enrichment. Monthly adds FedRAMP + DoD IL + USASpending
OT-signal enrichment, then renders the markdown report. Schema:
three tables — `events`, `companies`, `participations` (the atomic
evidence row). Every claim in the final report traces back to a
`participations` row with a URL and excerpt.

## First-time setup

```bash
# 1. Clone and venv
git clone <repo-url> /opt/defense-aggregator
cd /opt/defense-aggregator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Initialize config
cp .env.example .env
$EDITOR .env    # add API keys you have; leave rest as 'offline'

# 3. Initialize the database
python -c "from store import cache; cache.connect().__enter__()"

# 4. Test end-to-end against fixtures
python -m unittest tests/test_extract.py
python run_full_demo_v2.py

# 5. Install cron entries
crontab -e
# Add (UTC times shown — adjust for your timezone):
#   15 11 * * *   cd /opt/defense-aggregator && ./cron/daily.sh    >> logs/daily.log   2>&1
#   0  10 * * 0   cd /opt/defense-aggregator && ./cron/weekly.sh   >> logs/weekly.log  2>&1
#   0  9  1 * *   cd /opt/defense-aggregator && ./cron/monthly.sh  >> logs/monthly.log 2>&1
```

## API keys you might want

All optional. The pipeline degrades gracefully if a key is missing
— the corresponding adapter no-ops, and the rest still runs.

| Key | Used for | Cost | Where to get |
|---|---|---|---|
| `BRAVE_API_KEY` | Search backend for recap discovery | Free: 2000 q/month | https://api.search.brave.com |
| `CRUNCHBASE_API_KEY` | Funding / investor enrichment | ~$50/mo minimum (paid only since 2026) | https://data.crunchbase.com |
| `SAM_API_KEY` | Optional — for the v1 SAM.gov adapter when ported | Free, rotates every 90 days | https://api.sam.gov |
| `GITHUB_TOKEN` | Future — repo-level participant discovery | Free | https://github.com/settings/tokens |

FedRAMP marketplace and USASpending are open APIs — no key needed.

## What the three jobs do

### `daily.sh` — fast, cheap

- Refreshes events from public sources (NavalX, DARPA, USA.gov
  challenges, SBIR.gov solicitations; plus v1 adapters once
  ported: SAM.gov, xtech, DIU, SOFWERX, AFWERX, defensewerx).
- Runs `discover.discover_all()` over events whose dates_start is
  within the last 90 days.

Expected runtime: 2–10 minutes (search rate limit dominates).

### `weekly.sh` — adds Crunchbase enrichment

- Everything `daily.sh` does.
- Crunchbase lookup for every company in the store. ~80 seconds
  of API time at the default rate limit for a 200-company store.

Expected runtime: 5–20 minutes.

### `monthly.sh` — adds compliance + report build

- Everything `weekly.sh` does.
- FedRAMP marketplace bulk-fetch then in-memory join.
- DoD IL detection from FedRAMP product descriptions.
- USASpending OT/contract lookup per company (slowest step;
  rate-limited 0.3s/request). Set `SKIP_USASPENDING=1` to skip.
- Builds the markdown report to `reports/out/report_<YYYY-MM-DD>.md`.

Expected runtime: 15–60 minutes (USASpending dominates).

## Triage — when things go wrong

**Zero events discovered after `daily.sh`.**
1. Check `logs/daily.log` for 403s. Some `.mil` and `.gov` sites
   reject datacenter IPs. Fix: deploy from a residential or
   corporate egress, or proxy through one. The adapters already
   send a browser-like User-Agent; site-specific quirks may need
   per-adapter tuning.
2. Verify `SEARCH_BACKEND` in `.env` and that `BRAVE_API_KEY` is
   set if using Brave.

**Crunchbase enrichment all "no_match".**
- `CRUNCHBASE_BACKEND` must be `live`, not `offline`.
- Auth header is `X-cb-user-key`, not Bearer. The `CrunchbaseAPI`
  class handles this; if you wrote a custom backend, check.
- Stealth and government-only contractors won't be on Crunchbase
  by design. Persistent no-matches there are correct behavior.

**FedRAMP enrichment 0 records loaded.**
- The marketplace JSON export shape has shifted twice since 2023.
  The adapter is defensive but not omniscient.
- Debug: `python -c "from enrich.compliance import FedrampAPI;
  print(len(FedrampAPI().all_records()))"`. If 0, the API shape
  changed — inspect the raw response and adjust the parser.

**Review queue growing unbounded.**
- Inspect: `sqlite3 store/events.sqlite "SELECT candidate_name,
  nearest_match, similarity FROM review_queue WHERE resolution IS
  NULL ORDER BY created_at DESC LIMIT 20"`.
- For each row, decide: `merged` (update
  participations.company_id) or `created` (set
  resolution='created'; next pipeline run will auto-create since
  it's no longer fuzzy-matched against the wrong company).
- If the queue is consistently large, raise `REVIEW_THRESHOLD` in
  `extract/company_match.py`. Default is 0.82; 0.85 will cut
  noise but lose some recall.

**Section 7 is enormous.**
- Section 7 lists every (company × source) pair. 20 events × 10
  companies × 3 sources = 600 entries. The current builder shows
  all of them; cap with a slice in `section_7()` if needed.

**Monthly report missing.**
- Check `logs/monthly.log` for the last successful run.
- Reports are written atomically; partial files don't appear.
- Re-running `python -m reports.build_markdown` regenerates from
  current store state without re-running any enrichment.

## Adjusting the heuristics

Most knobs are in plain-text config:

| File | Controls |
|---|---|
| `config/gazetteer.txt` | Company names the NER recognizes. One per line, with aliases. |
| `config/recap_sources.yaml` | Editorial domains, per-source weight, confidence floor. |
| `extract/company_match.py` | `REVIEW_THRESHOLD` (0.82) and `AUTO_MERGE_THRESHOLD` (0.90). |
| `extract/confidence.py` | `OFFICIAL_DOMAINS` and `EDITORIAL_DOMAINS`. |

Gazetteer growth is healthy at +50–200 entries/month as the
analyst confirms matches. Faster than that and false-positive
matches accumulate.

## What the system does not do

- **People tracking.** Deferred to v3. We track companies, not
  founders or investors as individuals.
- **Photo / badge OCR.** Manual analyst workflow; out of scope.
- **LinkedIn / X automation.** TOS forbids it.
- **Real-time alerts.** Cadence is daily / weekly / monthly. For
  same-day notification, build a separate alerting layer on the
  events table.
- **Predictive scoring.** No "this startup is hot" labels. The
  report shows evidence; the analyst interprets.

## Confidentiality

Output reports are internal-only. In particular:

- Crunchbase fields come from a paid feed. Section 2's funding
  columns must not leave the internal team.
- Quoted excerpts in Section 7 are short (≤200 chars) and may
  fall under fair use, but the overall report is a derived work.
  Treat it as internal-only.

## Glossary

- **Event** — hackathon, TEM, Industry Day, prize challenge, SBIR
  solicitation. Identified by host + dates_start + name.
- **Company** — a participating organization. Aliases collapse to
  one record. `is_stealth=true` means the name has no legal
  suffix and isn't in the gazetteer.
- **Participation** — atomic evidence row. One per
  (company × event × role × evidence_url). Carries excerpt and
  confidence.
- **Confidence tier**:
  - `confirmed` — official source page.
  - `highly_likely` — named-byline editorial recap or first-person
    organizer post.
  - `ecosystem_associated` — third-party social, photo coverage,
    or indirect mention.

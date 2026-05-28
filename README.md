# defense-aggregator

A monthly defense-innovation-events aggregator. Tracks hackathons,
TEMs, prize challenges, and SBIR solicitations across DoD
innovation hubs. Extracts company participation from official
pages, editorial recaps, and first-person organizer posts.
Enriches with funding/investor data (Crunchbase) and compliance
data (FedRAMP, DoD IL, USASpending OT signals). Produces a
seven-section report each month — Markdown, CSV (Section 2 master
list), and PDF — with every claim traceable to a source URL.

## Quick start

```bash
# 1. Install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (defaults to offline mode — works without API keys)
cp .env.example .env

# 3. Verify everything works end-to-end against fixtures
python -m unittest tests/test_extract.py    # 15 unit tests
python run_final_demo.py                    # full pipeline + report

# 4. (Optional) Switch to live mode by editing .env
#    Set SEARCH_BACKEND=brave + BRAVE_API_KEY=...    (free tier from api.search.brave.com)
#    Set CRUNCHBASE_BACKEND=live + CRUNCHBASE_API_KEY=...  (Enterprise tier — Organizations endpoints)
#    Set FEDRAMP_BACKEND=live   (no key required; uses GitHub-hosted FedRAMP export)

# 5. Run a monthly report manually (writes MD + CSV + PDF to reports/out/)
./cron/monthly.sh

# 6. (Optional) Install cron schedule
crontab -e
#  15 11 * * *   cd /opt/defense-aggregator && ./cron/daily.sh    >> logs/daily.log   2>&1
#  0  10 * * 0   cd /opt/defense-aggregator && ./cron/weekly.sh   >> logs/weekly.log  2>&1
#  0  9  1 * *   cd /opt/defense-aggregator && ./cron/monthly.sh  >> logs/monthly.log 2>&1
```

### Outputs

Each monthly run writes three artifacts to `reports/out/`:

| File | Purpose |
|---|---|
| `report_<date>.md` | Full seven-section markdown report (read-friendly) |
| `section2_<date>.csv` | Deduped participant master list — for Excel / CRM import / outbound automation |
| `report_<date>.pdf` | Landscape-letter PDF (rendered via reportlab, no system deps) |

The Markdown report's Section 2 includes a **Where seen** column listing
the event name(s) per company, so outbound messages can reference the
specific competition (e.g., *"I noticed you recently demoed at the xTech
National Security Hackathon…"*).

### Skip flags

`cron/monthly.sh` honors these env vars (defaults shown):

| Var | Default | Effect |
|---|---|---|
| `SKIP_USASPENDING` | `1` | Skip USASpending OT/contract lookups (~2 min per run) |
| `SKIP_OTA_COLUMN` | inherits from `SKIP_USASPENDING` | Drop the "DoD OT/contracts (24m)" column from Section 5 |
| `SKIP_TRANSITIONS` | `1` | Drop the "Hackathon → SBIR/OT transitions" subsection |
| `SKIP_NEW_COMPANIES` | `0` | Drop the "New companies this window" subsection (useful for YTD reports) |
| `TARGET_PARTICIPANTS_ONLY` | `1` | Filter sponsors/judges/mentors + primes/integrators from outbound-focused sections |
| `DISCOVERY_WINDOW_DAYS` | `143` | How far back `daily.sh` looks for events to run Brave discovery on (~5 months) |
| `MAX_QUERIES_PER_SOURCE` | `50` | Hard ceiling per editorial source per run; keeps Brave usage well under the 2k/month free tier |

Override per-run, e.g. `SKIP_USASPENDING=0 ./cron/monthly.sh` or `MAX_QUERIES_PER_SOURCE=20 ./cron/monthly.sh`.

See `RUNBOOK.md` for setup, triage, and operational notes.
See `CLAUDE.md` for architecture guidance when working in this
repo with Claude Code.

## Repo layout

```
schema/         dataclasses: Event, Company, Participation + vocab
store/          SQLite store + cached HTTP responses
sources/        event/article adapters
  recap_scraper.py    NER + matching + confidence pipeline
  discover.py         orchestrates per-event article discovery
  search_backend.py   pluggable: Brave (live) / Offline (fixtures)
  navalx.py / darpa.py / usagov_challenges.py / sbir_gov.py
extract/        NER, fuzzy company matching, confidence assignment
enrich/         weekly+monthly enrichment
  crunchbase.py       funding / investors / type / domains
  compliance.py       FedRAMP / DoD IL / USASpending OT signals
analysis/       monthly diff queries
reports/        report builders + output dir
  build_markdown.py   seven-section markdown report
  build_csv.py        Section 2 master list as CSV
  build_pdf.py        full report as PDF (pure-Python, reportlab)
cron/           daily.sh / weekly.sh / monthly.sh orchestration
pipeline/       CLI: python -m pipeline.discover_all
config/         gazetteer + recap_sources YAML
tests/          unit tests + offline fixtures
```

## Demo runners

- `run_demo.py` — minimal: seed event, process 3 fixtures, print results.
- `run_full_demo.py` — discovery + scrape, no enrichment.
- `run_full_demo_v2.py` — adds Crunchbase enrichment.
- `run_final_demo.py` — all layers (recommended).

## Key design choices

- **Companies are first-class.** Participations are the atomic
  evidence row linking a company to an event with a URL + excerpt.
  Every report claim derives from a Participation.
- **Three confidence tiers**: `confirmed` (official source page),
  `highly_likely` (editorial recap with byline OR first-person
  organizer post), `ecosystem_associated` (indirect mention).
- **Pluggable backends**: search, Crunchbase, and FedRAMP each
  have a live API client + an offline fixture client. Production
  runs live; tests and demos run offline. Same code path.
- **Graceful degradation**: when one source 403s or one API key is
  missing, the rest of the pipeline still completes.
- **No people tracking, no LinkedIn/X automation, no predictive
  scoring.** The report shows evidence; an analyst interprets.

## License

### Code

MIT — see [LICENSE](LICENSE). Use, modify, redistribute freely.

### Data (output)

The MIT license covers the source code only. **Data produced by running
this pipeline has separate restrictions** and must not be republished
externally without clearing rights:

- **Crunchbase fields** in Section 2 come from a paid data feed and have
  contractual restrictions on redistribution.
- **Quoted article excerpts** in Section 7 are short (≤200 chars) and may
  fall under fair use, but the overall report is a derived work.
- **FedRAMP / USASpending data** is public-domain US government data and
  generally redistributable, but check current terms.

Treat the generated report as internal-only unless you've cleared rights
with each upstream source.

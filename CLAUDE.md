# CLAUDE.md — guidance for Claude Code working in this repo

## What this repo is

A defense-innovation-events aggregator. Monthly batch pipeline:
events → recap articles → company participations → enrichment →
markdown + PDF report. SQLite-backed, Python 3.11+, no web framework.

## Why this repo exists (the use case)

The output drives **ATO-outreach for a compliance-acceleration
service**. We track U.S. defense-industry-facing engagements
(sprints, hackathons, technical exchange meetings, prize
challenges, assessment events, qualifying events, capability
engagements, proposers days, industry days) and the **companies
that participate** in them — winners AND non-winners. The premise
is: if the government invited you to compete, they want your
solution; if you don't already have FedRAMP or DISA authorization,
we can help. The report is read top-down by analysts doing
outbound; the first page is meant to be an action list.

Hosts we care about include AFWERX, DIU, SOFWERX, xTech, DARPA,
NavalX, the SBIR program, and granular programs like
dronedominance.mil. Adjacent press (DefenseScoop, Inside Defense,
Breaking Defense) is signal too.

What we explicitly **don't** want in the report: commercial
hackathons (Claude Code, OpenAI Codex, Llama, Gemini, GPT-5,
crypto/DeFi, hospitality, fintech, consumer health, YC demo-day
afterparties), explainer / personal-blog content ("My First
Hackathon"), and SOFWERX community outreach (STEM showcases,
high-school career fairs, welding workshops). The
defense-relevance gate (rule 6) exists to keep these out.

## Architectural ground rules

These are load-bearing decisions. Don't change them without
explicit discussion.

### 1. Companies are first-class entities; participations are
   atomic evidence.

The schema has three tables: `events`, `companies`,
`participations`. A Participation is one row per
(company × event × role × evidence_url). It carries an excerpt
from the source and a confidence rating. The report is rendered
by SELECTs over this schema.

**Don't** denormalize "company X attended events [A, B, C]" into
the companies table. The participation rows are the audit trail.
The report's value comes from being able to point at evidence for
every claim.

### 2. Three confidence tiers — and only three.

- `confirmed`: the program's own page, official social, or
  primary-source recap from the host organization.
- `highly_likely`: editorial recap from a named-author byline
  (DefenseScoop, Inside Defense, Breaking Defense), sponsor's
  portfolio blog, or founder's first-person post.
- `ecosystem_associated`: photo coverage, third-party social,
  indirect mention.

The vocab lives in `schema/vocab.py`. Adding a fourth tier would
ripple through report rendering, the confidence-assignment logic,
and downstream filters. Resist this.

### 3. Pluggable backends — same contract, two implementations.

`search_backend.py`, `enrich/crunchbase.py`, and
`enrich/compliance.py` each define an abstract base class with
two concrete subclasses: a live API client and an offline fixture
client. Selection is via environment variable
(`SEARCH_BACKEND`, `CRUNCHBASE_BACKEND`, `FEDRAMP_BACKEND`). The
factory function (`get_backend()`) handles the dispatch.

When adding a new external data source, follow this pattern.
Don't add live HTTP calls inside the call site of the consumer —
hide them behind the backend interface.

### 4. Graceful degradation across sources.

Every adapter that can fail (403, timeout, schema drift) does so
without crashing the pipeline. Look at `cron/daily.sh` —  it wraps
each source adapter in try/except and continues. Don't change
this. A bad day for one source isn't a bad day for the whole
pipeline.

### 5. Internal-only data.

The output report and the database should not be shipped
externally without clearing data rights with each source. The
Crunchbase fields in particular come from a paid feed and have
license restrictions.

### 6. Defense-relevance gate — one helper, two call sites.

`extract/defense_relevance.py` is the single source of truth for
the question "is this a U.S. defense-industry-facing engagement?"
It must be called in two places, by design:

  1. **Ingestion**: `sources.discover._discover_event_from_article`
     calls `is_defense_relevant(...)` before creating an Event from
     a search hit. Articles that fail are dropped — no Event, no
     Participations.
  2. **Report**: `reports.build_markdown._filter_data_for_target_audience`
     re-runs the gate over every event already in the store, so
     legacy data ingested under older rules also gets cleaned up.

The two call sites use the **same module**. If you find yourself
writing a parallel allow/deny list anywhere else in the codebase,
stop — extend `defense_relevance.py` instead. Otherwise the rules
drift and Section 1 fills back up with Hospitality 2030 Hackathon.

The gate is rule-based, not ML. Edit `DEFENSE_HOSTS`,
`_DEFENSE_VOCAB_RE`, `_COMMERCIAL_NEG_RE`, `_OUTREACH_NEG_RE`, and
`THRESHOLD` to tune. Whenever you tune, run the report against the
existing store and spot-check what dropped or got admitted.

### 7. Report is participant-only.

The report excludes **judges, sponsors, hosts, and mentors**
(roles) and **primes / integrators** (companies). The role list
lives in `reports.build_markdown.EXCLUDED_ROLES`; the company list
lives in `KNOWN_PRIMES_INTEGRATORS`. The prime exclusion is
controllable per run via `exclude_primes=False`. Funders /
investors are kept — they're useful outreach context.

The report's **first section is "Outreach priority"** —
defense-engagement participants that do NOT already carry FedRAMP
authorization or a DoD IL designation. Everything else in the PDF
supports that list. Don't reorder sections without explicit
discussion; the analyst workflow assumes outreach candidates at
the top.

## Conventions

### Imports
- Standard library first, third-party second, project third.
- Project imports use absolute paths from the repo root
  (`from schema.event import Event`), not relative
  (`from ..schema.event import Event`).

### Logging
- `log = logging.getLogger(__name__)` at module top.
- Log lines start with a `[component]` prefix:
  `log.info("[discover] %d in window", n)`.
- INFO for normal milestones; WARNING for graceful failures
  (e.g., source 403); ERROR for things that should page someone;
  DEBUG for verbose internal traces.

### Errors
- HTTP errors caught and logged, never re-raised across adapter
  boundaries. The caller gets `None` or `[]`.
- Programmer errors (wrong env var, invalid config) raise on
  startup with a clear message.

### Dates
- Internal storage: ISO 8601 (`YYYY-MM-DD`).
- `normalize_helpers.parse_date_loose()` is the canonical fuzzy
  parser. Use it everywhere user-facing dates are read.

### IDs
- Event ID: `sha1(host|dates_start|name)[:16]` — see
  `schema.event.make_event_id`.
- Company ID: `sha1(normalized_name)[:16]` — see
  `schema.company.make_company_id`.
- Participation ID: `sha1(company|event|role|evidence_url)[:16]`.

These are intentionally collision-resistant but not cryptographic.
Don't use them for anything security-sensitive.

## Common tasks

### Adding a new event source

1. Create `sources/<name>.py` with a `fetch_events() -> list[Event]`
   function.
2. Follow the pattern in `sources/navalx.py`, `sources/sbir_gov.py`,
   or `sources/dronedominance.py`: best-effort HTTP fetch with
   caching, tolerant date parsing, return `[]` on any failure.
3. If the source is a `.mil` / `.gov` site, gate the live fetch
   behind an env var (default: fixture). See
   `sources/dronedominance.py` for the pattern — we don't want
   batch cron jobs hammering DoD sites without sign-off.
4. Add it to the loop in `cron/daily.sh`.
5. (Optional but encouraged) Add a fixture under `tests/fixtures/`
   and a test case.
6. If your adapter discovers events from article titles (i.e.
   calls `_discover_event_from_article`), the defense-relevance
   gate fires automatically. If you parse a structured list (like
   dronedominance vendors), the items are trusted — no gate run.

### Tuning the matcher

`extract/company_match.py` has `REVIEW_THRESHOLD` (default 0.82)
and `AUTO_MERGE_THRESHOLD` (default 0.90). If the review queue is
growing too fast, raise `REVIEW_THRESHOLD`. If wrong-merges are
happening, raise `AUTO_MERGE_THRESHOLD` (it's already
conservative — go to 0.93 or higher before doing this).

### Adding a gazetteer entry

Edit `config/gazetteer.txt`. Format is one company per line, with
aliases separated by `|`:
```
Anduril Industries|Anduril|Anduril Industries, Inc.
```
Re-sort alphabetically. The NER step reloads on the next run.

### Building a one-off report from current store state

```bash
python -m reports.build_markdown   # writes reports/out/report_<YYYY-MM-DD>.md
python -m reports.build_pdf        # writes reports/out/report_<YYYY-MM-DD>.pdf
```

No enrichment runs; uses whatever's already in the store. PDF is
the canonical deliverable; markdown is the analyst-facing raw view.

`build(target_participants_only=False)` disables the
defense-relevance / role / prime filters — useful for a
one-time analyst audit but not for the outbound report.
`build(exclude_primes=False)` keeps primes in but still applies
the other filters.

### Tuning the defense-relevance gate

`extract/defense_relevance.py` is the choke point. Three knobs:

  - `DEFENSE_HOSTS` — add any new program / publication domain
    you want to whitelist.
  - `_DEFENSE_VOCAB_RE` — add terms that, when present, mark an
    event as defense-relevant.
  - `_COMMERCIAL_NEG_RE` / `_OUTREACH_NEG_RE` — add patterns that
    should disqualify (commercial hackathons, STEM outreach, etc.).
  - `THRESHOLD` — raise for stricter, lower for more inclusive.

After tuning, re-run the report against the store and grep the
new output for the noise patterns you were trying to kill.

## What's deferred (don't add without asking)

- People tracking (founder names, etc.) — deferred to v3.
- LinkedIn / X automation — TOS forbids, won't add.
- Predictive "this startup is hot" scoring — out of scope by
  design. The report is evidence; analysts interpret.
- Photo / badge OCR — manual analyst workflow.
- Real-time alerts — separate system if needed.
- Live dronedominance.mil fetch in cron by default — opt-in via
  `DRONEDOMINANCE_BACKEND=live`. Default fixture path keeps the
  pipeline safe to run without DoD-site sign-off.
- Auto-tuning the defense-relevance gate from missed-event
  reports — for now, additions are manual edits to
  `defense_relevance.py`.

## Where to find things

- API and backend env vars: `.env.example`
- Cron orchestration: `cron/*.sh`
- Production triage: `RUNBOOK.md`
- Schema: `store/schema.sql`
- Vocabularies: `schema/vocab.py`
- Tests: `tests/test_extract.py`
- Demos: `run_*.py` at repo root

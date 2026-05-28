# Hand-off document — v2 Recap Prototype

This document is for whoever picks up the project next — another
Claude session in Cowork, an engineer, or a contractor. Read this
first, then `CLAUDE.md`, then `README.md`, then `RUNBOOK.md`.

## What this project is

A monthly batch pipeline that produces a defense-innovation-events
**outbound list** for the project owner (LaRel Rogers). Primary use
case: post-competition outreach to defense-tech startups that
participated in named hackathons / challenges / sprints / industry
days / cohorts / assessment events.

Outputs each month: Markdown report, CSV of the master list, PDF
of the full report. All in `reports/out/`.

## Where it lives

`/Users/larelrogers/Downloads/v2-recap-prototype` on LaRel's
MacBook. Distribution zip at `~/Downloads/v2-recap-prototype.zip`.

Git: initialized but **not pushed to a remote yet** at time of
hand-off. 17 commits on `main`. Add a GitHub remote and `git push`
to share.

## Current state at hand-off (2026-05-27)

- **303 events** in store, **866 companies**, **1,775 participations**
- **820 target-audience-filtered companies** in Section 2 (after
  dropping primes/integrators + sponsor/judge/mentor-only entries)
- Latest report: `reports/out/report_2026-05-26.md` (550 KB),
  `.pdf` (359 KB), `section2_2026-05-26.csv` (75 KB)
- All 10 event adapters wired into `cron/daily.sh`
- All 29 unit tests passing

## Owner preferences (load-bearing)

These came up explicitly in conversation with the project owner.
Honor them by default; ask before overriding.

1. **Default-skip USASpending and Hackathon→SBIR/OT transitions**
   when running the report from chat — `SKIP_USASPENDING=1` and
   `SKIP_TRANSITIONS=1` are already the defaults in
   `cron/monthly.sh`. If running interactively, ask before
   enabling either of these.

2. **Target audience = competing defense-tech startups, NOT**:
   - Primes (Lockheed, Northrop, Boeing, RTX, L3Harris, BAE,
     General Dynamics)
   - Integrators (Palantir, Leidos, Deloitte, Booz Allen, CACI,
     SAIC, Scale AI)
   - Sponsors / partners / event organizers
   - Judges / mentors (ecosystem-adjacent, not competing)

   Investors **are kept** (relevant for outbound). The filter is
   on by default via `TARGET_PARTICIPANTS_ONLY=1`. Hard-coded
   exclude list lives in `reports/build_markdown.py` as
   `KNOWN_PRIMES_INTEGRATORS`.

3. **Brave quota is real**. Free tier = 2k queries/month. Per-
   source cap is set to 50 (`MAX_QUERIES_PER_SOURCE=50`), giving
   a hard ceiling of ~300 queries per monthly run. Don't disable
   this without explicit owner approval.

4. **Don't add features outside the deferred-items list in
   `CLAUDE.md`**:
   - No people/founder tracking
   - No LinkedIn / X automation
   - No predictive "this startup is hot" scoring
   - No photo / badge OCR
   - No real-time alerts

## Architecture in one paragraph

Adapters under `sources/` produce `Event` rows. A discovery loop
(`sources/discover.py`) processes each Event in the active window
(default 143 days) by either: (a) running Brave searches against
6 editorial sources for that event's name + aliases, then scraping
returned articles; or (b) for WERX-style adapters, scraping their
site directly with no Brave queries. NER (`extract/ner.py`)
extracts company mentions; `extract/company_match.py` dedupes
against existing companies; `extract/confidence.py` assigns
confirmed / highly_likely / ecosystem_associated. Enrichment
(`enrich/`) layers in Crunchbase funding/investor data and
FedRAMP/IL/OT compliance signals. Report builders (`reports/`)
render Markdown, CSV, and PDF from the SQLite store.

## Adapter status

| Adapter | File | Status | Notes |
|---|---|---|---|
| xTech | `sources/xtech.py` | ✅ Working | 49 events + 646 participations direct from WordPress sitemaps. Zero Brave queries. |
| DARPA | `sources/darpa.py` | ✅ Working | Sitemap-based after `/events` went JS-rendered. |
| DIU | `sources/diu.py` | ✅ Working | Scrapes `/latest` articles, uses article-title event discovery. |
| AFWERX | `sources/afwerx.py` | ✅ Working | Yoast news sitemap, ~21 engagement events / 135 articles. |
| SOFWERX | `sources/sofwerx.py` | ✅ Working | Strapi sitemap, 116 events. |
| DefenseWerx | `sources/defensewerx.py` | ✅ Working but low-yield | Umbrella org, ~19 posts, mostly leadership announcements. |
| SAM.gov | `sources/samgov.py` | ⏸ Ready, needs key | No-ops without `SAM_API_KEY`. Free key from api.sam.gov (90-day expiry). |
| SBIR.gov | `sources/sbir_gov.py` | ⏸ Server-side outages | Adapter works; their public API is flaky. |
| NavalX | `sources/navalx.py` | ❌ TLS failure | Needs Python 3.11+ to upgrade OpenSSL. Currently on 3.9. |
| USA.gov | `sources/usagov_challenges.py` | ❌ Broken | Page redesigned; selectors outdated. Low value for outbound — defer. |

## Known issues / open items, ranked by priority

### RESOLVED

1. ✅ **Cerebral Valley noise / commercial hackathon spillover**.
   Implemented (option B from the original plan, expanded).
   `extract/defense_relevance.py` is now the single source of truth
   for the gate; it's called at ingestion time in
   `sources.discover._discover_event_from_article` and at report
   time in `reports.build_markdown._filter_data_for_target_audience`.
   Covers AI-lab hackathons, crypto/DeFi, hospitality/fintech/health,
   YC demo-day afterparties, Medium explainers, and SOFWERX
   community/STEM outreach. Unit tests in
   `tests/test_defense_relevance.py` lock in the 30 cases from the
   May 26 report (7 must pass, 23 must drop).

2. ✅ **DefenseScoop URLs going to 404 in PDF**. Two-part fix in
   `reports/build_pdf.py`: (a) `_clean_url()` strips control /
   object-replacement / zero-width characters that scraping left in
   evidence URLs (notably `%EF%BF%BC`); (b) Section 7 now renders
   URLs as proper PDF hyperlinks via `<link href="...">`, with a
   shortened display label so wrapped text doesn't break the link.

3. ✅ **Skyrun mis-classified as participant**. Reclassified as a
   co-host of xTech National Security Hackathon. Removed from
   `config/gazetteer.txt`'s participant section; new
   `KNOWN_HOSTS_SPONSORS` set in `reports/build_markdown.py`
   excludes named co-hosts regardless of inferred role. The
   filter is always-on (unlike `KNOWN_PRIMES_INTEGRATORS` which
   is configurable per run).

4. ✅ **Report restructured around the ATO-outreach goal**. New
   top section: "Outreach priority — defense participants without
   FedRAMP / IL", ranked by evidence strength × competition
   footprint, with bonus weight for winners/finalists and for
   companies already in FedRAMP `ready`/`in_process` (warmest
   leads). Section 6 (ecosystem mapping) dropped from the PDF —
   it lives in the markdown for analysts. Funding column dropped
   from outreach priority (we don't independently validate
   Crunchbase data); replaced with "Last seen" for outbound
   timing. Funding remains in Section 2 for analyst context.

5. ✅ **dronedominance.mil source added** as
   `sources/dronedominance.py`. Default fixture (no .mil calls
   without sign-off); set `DRONEDOMINANCE_BACKEND=live` to opt in.
   Parses each `phaseNPanel` as a separate Event.

6. ✅ **GitHub source added** as `sources/github.py`. Abstract
   backend with `GitHubAPIBackend` (live, needs `GITHUB_TOKEN`)
   and `GitHubFixtureBackend` (default). Config in
   `config/github_events.yaml`. Found repos become Participations
   on existing Events (no new Events created). Confidence:
   `confirmed` if repo opts into a configured topic tag,
   `highly_likely` otherwise. Fixture covers the four xTech
   National Security repos surfaced by the owner
   (Kingkali69/Army_Xtech, jeremyloseycesi/K9-Sentinel,
   sachin-crispai/xtech-natsec-cv, PoggyBobby/EMCON-Sentinel).

2. **Test isolation regression risk**. `tests/test_extract.py`
   `TestPerSourceQueryCap` now uses a tempfile-backed DB via
   setUp/tearDown after a prior version polluted production.
   `store.cache.connect()` resolves `DB_PATH` at call time
   (changed from default-arg evaluation). If anyone changes the
   `connect()` signature, re-verify the test still isolates by
   running `python -m unittest tests/test_extract.py` followed
   by `python -c "from store import cache; ..."` to confirm no
   `Cap Test` event leaked.

### HIGH

3. **NER residual noise**. After the existing stoplists and
   filters, Section 2 still occasionally surfaces glommed entries
   like "MAIK Snorkel AI Unstructured Technologies" (list-element
   concatenation in DIU pages) and DoD-program-name false
   positives like "USSF Space Systems" or "Air Missile Systems".
   Diminishing returns at the regex level — would need either a
   trained NER model or per-source HTML parsing hints.

4. **Gazetteer is small** (~80 companies). Most Section 2
   entries show `type=unknown` and empty domains because Crunchbase
   offline fixture only covers 15 companies and gazetteer is
   sparse. Either:
   - Switch to live Crunchbase (owner has a key but needs to
     confirm the package — Organizations vs Predictions; see
     conversation history), OR
   - Add ~50 known defense-tech startups to
     `config/gazetteer.txt` manually.

### MEDIUM

5. **AeroVironment example wasn't recovered**. The owner cited
   AeroVironment being invited to "Drone Dominance Phase II
   qualifying event" as a target case. Article-title discovery
   only fires when the URL passes the non-article-URL filter,
   and AeroVironment's evidence was on author/digest pages.
   Recovery would require either relaxing URL-shape filtering
   (risky — re-introduces misattribution) or finding the real
   recap article via different Brave queries.

6. **Article-discovered events all date today**. When
   `_discover_event_from_article` can't extract a date from the
   article, it defaults to `date.today()`. Improvement: parse
   the article body for a date string, or use Brave's
   `published_at` (already passed in by `discover_for_event`
   but not yet propagated by direct-adapter callers like DIU,
   AFWERX, SOFWERX).

7. **Section 6 (Ecosystem mapping) intentionally unfiltered**.
   It shows supporters who co-occur with participants. If a
   future owner wants this filtered too, see
   `reports/build_markdown.py:build()` where Section 6 is the
   only section using `raw` instead of the filtered data.

### LOW

8. **NavalX TLS failure** would be fixed by Python 3.11+.
9. **USA.gov selectors** need updating for the redesigned page.
10. **SBIR.gov server outages** are external; no action needed.

## How to run

```bash
cd /Users/larelrogers/Downloads/v2-recap-prototype
source .venv/bin/activate

# Full monthly (daily + weekly + monthly enrichments + MD/CSV/PDF)
./cron/monthly.sh

# Just rebuild the report from existing store state
python -m reports.build_markdown
python -m reports.build_csv
python -m reports.build_pdf

# Run a single adapter to debug
python -m sources.xtech    # any of: darpa, diu, afwerx, sofwerx, defensewerx, samgov

# Run tests
python -m unittest tests/test_extract.py
```

Env vars worth knowing (all overridable per-run):
- `SEARCH_BACKEND=brave|offline` (currently `brave`, key in `.env`)
- `CRUNCHBASE_BACKEND=offline|live` (currently `offline`)
- `FEDRAMP_BACKEND=live|offline` (currently `live`, GitHub JSON)
- `SKIP_USASPENDING=1` (default)
- `SKIP_TRANSITIONS=1` (default)
- `SKIP_NEW_COMPANIES=0` (default; set to 1 for YTD reports)
- `TARGET_PARTICIPANTS_ONLY=1` (default)
- `DISCOVERY_WINDOW_DAYS=143` (default ~ YTD)
- `MAX_QUERIES_PER_SOURCE=50` (hard cap per source per run)
- `SAM_API_KEY=` (empty; needs free key from api.sam.gov)
- `BRAVE_API_KEY=` (set in `.env`)
- `CRUNCHBASE_API_KEY=` (empty pending package confirmation)

## Critical files to know

- `CLAUDE.md` — architectural ground rules + deferred-items list
- `RUNBOOK.md` — operational notes (older)
- `config/gazetteer.txt` — known defense-tech companies
- `config/recap_sources.yaml` — editorial sources for Brave search
  *(Cerebral Valley remains here; defense-relevance gate handles it
  at ingestion + report time — see resolved issue #1)*
- `config/github_events.yaml` — GitHub query → event_id mapping
- `extract/defense_relevance.py` — defense-relevance gate (single
  source of truth)
- `cron/daily.sh` / `cron/monthly.sh` — orchestration entry points
- `sources/discover.py` — discovery loop, URL filter, engagement
  keyword regex, article-title-based event discovery, per-source
  cap logic
- `sources/recap_scraper.py` — NER pipeline integration
- `extract/ner.py` — heuristic + gazetteer + stoplists
- `reports/build_markdown.py` — main report builder, target-
  audience filter logic, `KNOWN_PRIMES_INTEGRATORS` list

## Commit history (most recent first)

```
c4f5cae  Isolate unit tests from production DB; fix store.connect default
cae6be0  Add Brave quota caps: per-source ceiling + 143-day discovery window
673c383  Wire all 6 new adapters into cron/daily.sh; add 13 unit tests
bef9580  Add DefenseWerx adapter (low-yield by design)
c87ca76  Add SOFWERX adapter; improve _extract_article_title for SPA pages
49b28cd  Add AFWERX adapter: 135 news articles -> 21 engagement events
9b6456d  Add SAM.gov special-notices adapter (no-op without SAM_API_KEY)
d9ea32d  Add DIU adapter; broaden engagement-keyword regex
cf64287  Add xTech (Army FUZE) adapter: 49 events, 646 participations
cc1003e  Add target_participants_only filter
625a80e  NER noise cleanup
87becf8  Replace loose-attr sentinel with article-title-based event discovery
55be1b8  Fix attribution drift: URL filter + event-mention check
a27427c  Fix copyright attribution: Larel -> LaRel
5a428a4  Add MIT LICENSE; expand README
458d002  Initial commit
```

## Suggested first actions for the next session

1. **Fix Cerebral Valley noise (issue #1)** — quickest, biggest
   quality win. Either remove from `recap_sources.yaml` or add
   defense-relevance check. ~10-30 min.
2. **Re-run `./cron/monthly.sh`** with the fix to validate.
3. **Owner audits the new report**. Section 2 / CSV first.
4. **Consider live Crunchbase** if the package question is sorted
   (see conversation history for the Organizations vs Predictions
   distinction).
5. **Push to GitHub** if not already done — `git remote add origin
   <url> && git push -u origin main`.

## Things NOT to do without asking the owner

- Enable USASpending or Hackathon→SBIR/OT transitions
- Re-include primes/integrators/sponsors in Section 2
- Raise `MAX_QUERIES_PER_SOURCE` above 50
- Add adapters or scrapers for sites that aren't in the existing
  set without confirming defense-relevance
- Ship the SQLite DB or `reports/out/` files externally (per
  `CLAUDE.md` §5 data-rights rule)

## Source backlog — candidate new sources, ranked

Owner asked us to think beyond the existing source list. The list
below is the result of a second-pass research sweep — each entry
was checked against the actual site / API / docs, not just
plausibly-named. Sources are ranked by expected ROI for the
ATO-outreach use case (named participants per implementation hour).

### Tier 1 — confirmed, structured, high-yield

1. **SBIR.gov awards API** (`sources/sbir_awards.py`).
   Public JSON / XML API at `api.www.sbir.gov/public/api/awards`
   plus full bulk dumps (~290 MB, abstracts included). ~220k
   awards, names awardee firm + UEI/DUNS. Regular updates
   (~90-day lag for DoD). This is **distinct** from the existing
   `sources/sbir_gov.py` which pulls *solicitations*; awards are
   the participant side. Highest-ROI single feed for confirmed
   participants. Free, no key. Each award row becomes a
   Participation against an implied "<Agency> SBIR <Topic>"
   event (or the existing solicitation event if matched).

2. **Catalyst Accelerator** (`sources/catalyst_accelerator.py`).
   Space Force / SDA cohort accelerator. Every cohort publishes
   a GlobeNewswire press release naming all 6–7 selected small
   businesses by name (CADEW, CASJS, AI/ML ISR Delta 7, etc.).
   3–4 cohorts per year, very clean data. Site at
   `catalystaccelerator.space`; cohort announcements distributed
   via globenewswire.com and executivegov.com. Either scrape the
   site or hit a GlobeNewswire RSS filtered on "Catalyst
   Accelerator." Confidence = `confirmed`.

3. **NSIN cohort feed** (`sources/nsin.py`).
   `nsin.mil/news/` announces every Propel (130+ ventures),
   Vector (~20/yr), Foundry, and Emerge cohort by name. HTML
   scrape of the news index; same shape as the existing AFWERX
   adapter. Per-cohort cadence. **Caveat**: NSIN was sunset /
   restructured into DIU in 2024 — verify the feed still
   updates before investing real effort, and check whether new
   cohorts land at `diu.mil` instead.

4. **H4D (Hacking 4 Defense)** (`sources/h4d.py`).
   Stanford-anchored but federated across 30+ universities.
   Stanford publishes `stanfordh4d.substack.com/p/<cohort>` with
   named teams (Hydra Strike, OmniComm, ArgusNet, Horizon
   Shield); h4d.us/universities lists every participating school.
   Semester cadence (spring + fall). Per-cohort 30–60 named
   teams across the network. Each team becomes a Participation.

### Tier 2 — confirmed but messier

5. **DevPost** (`sources/devpost.py`).
   Hosts named DoD hackathons (USSOCOM MetalOps, Combat Feeding,
   MIDAS, National Security Hackathon, others). Per-hackathon
   subdomain like `natsechack.devpost.com`. Submission galleries
   are typically public but the dedicated `/participants` page
   often requires login. Strategy: scrape the public submission
   gallery + project pages, skip the gated roster page.
   Per-event cadence; back-catalog durable.

6. **USAspending.gov bulk extracts** as a *discovery* source
   (not just enrichment). Full bulk download + REST API
   (`api.usaspending.gov`); Award Data Archive by agency/FY back
   to FY2008. Filter DoD awards under $10M to surface SBIR-class
   awardees not in our gazetteer. **Caveat**: it's awards, not
   "applied/competed" — surfaces winners only, no non-winners.

7. **DTIC Grant Awards search**. Better-than-grants.gov surface
   at `discover.dtic.mil/grant/`. Publicly searchable DoD
   grants since Dec 2014 with awardee name + abstract. Useful as
   a backfill / cross-reference layer rather than a primary feed.

8. **AUSA Annual + SOF Week exhibitor lists**.
   AUSA: HTML at `meetings.ausa.org/annual/<year>/exhibitor_exhibitor_list.cfm`.
   SOF Week: full exhibitor PDF on `sofweek.org`. Both include
   Small Business Pavilion entries (the relevant section for
   outbound). Annual cadence — once per year per event. Skews
   toward booth-paying vendors; cross-reference against the
   prime/integrator exclusion list before treating as participants.

### Tier 3 — narrow / opportunistic

9. **HeroX prize challenges**.
   Hosts DARPA / NASA / USAF challenges. Finalist tables exist
   historically (e.g., DARPA Robotics Challenge 25-finalist post),
   but full solver lists for *live* challenges are typically not
   public — solvers self-disclose. HTML scrape, sporadic. Worth
   a thin adapter that opportunistically grabs finalist posts.

10. **Challenge.gov winner pages**.
    Federal prize-challenge hub; per-challenge winner pages exist
    with name + rank + photo. Recently restructured — no
    documented public API. HTML scrape only. ~136 challenges/yr;
    not all defense, so add the defense-relevance gate at
    ingestion.

11. **Wikipedia event pages**.
    DARPA Grand / Urban / Robotics / Cyber Grand Challenge each
    have Wikipedia pages with full finalist tables. Static /
    historical — useful for backfill, not monthly cadence.

12. **Press-release wires** (PR Newswire, Business Wire,
    GlobeNewswire) filtered on defense keywords. Companies
    announce when they win / place. Tier 2 sources (Catalyst,
    NSIN) effectively use this feed; a broader filter is mostly
    duplication.

### Don't bother — investigated and not useful

- **Tradewind Marketplace** (`tradewindai.com`). Marketplace of
  awardable vendor pitch videos gated behind a free government
  account; vendor names not surfaced as a public list. Not a
  participant feed.
- **Capital Factory Defense** (`capitalfactory.com/government`).
  Government landing page exists, NavalX/AFWERX/DIU co-locate
  there, but no public alumni roster found. Cohort companies
  surface only via individual press releases.
- **AFWERX Spark / Refinery accelerator**. Cohort companies are
  not posted as a structured directory; announcements appear in
  `afwerx.com/news` which the existing AFWERX adapter already
  covers.
- **Plug and Play National Security**. No dedicated NS cohort
  directory; defense-adjacent companies surface only in the
  general "Our Startups" listing without batch tagging.

### Commercial / restricted (weigh against `CLAUDE.md` §5)

- **HigherGov, GovTribe**. Third-party SBIR/contract aggregators
  with richer entity resolution than SBIR.gov. Both are paid
  commercial feeds — usable if licensed; data-rights friction
  per `CLAUDE.md` §5 (internal-only data). Document the user's
  contract status before enabling.

### Off-limits / explicitly out of scope

- **LinkedIn / X / Meta automation**. ToS forbids; the
  `CLAUDE.md` deferred list already calls this out. Manual,
  in-browser checking of public LinkedIn posts is fine (and is
  in the analyst playbook); automation isn't.
- **Crunchbase as a *discovery* source**. Paid feed allows
  enrichment of known companies (already wired through
  `enrich/crunchbase.py`); using it to enumerate companies by
  industry tag would violate the contract.

### Ranked build order

If you can only add one or two adapters next cycle, build in
this order:

1. **SBIR.gov awards API** — free, structured JSON, ~220k named
   awardees. Single biggest add.
2. **Catalyst Accelerator** — small but very clean signal;
   matches the "ATO-not-yet" target profile almost perfectly.
3. **NSIN news feed** — pending confirmation that the feed
   still updates post-DIU restructure.
4. **H4D Stanford Substack** — semester cadence, named teams.
5. **DevPost** — broader but messier; add after Tier 1 lands.

### How to add one of these

Use `sources/github.py` as the template for any source that
returns structured participant lists, or `sources/dronedominance.py`
for a single-page parser. Both follow the same pattern:

  1. Define `<Source>Backend` abstract base + Live + Fixture
     implementations (or a single fetch_fixture+fetch_live for
     simpler cases).
  2. Add `fetch_events()` (and `fetch_participations(event_id, …)`
     if the source is participant-only, like GitHub).
  3. Add a fixture under `tests/fixtures/` so the demo can exercise
     it offline.
  4. Wire into `cron/daily.sh`.
  5. Default the backend to fixture; require an env var to enable
     the live path. `.mil` and `.gov` sources especially must
     default to fixture.
  6. If the source publishes a confirmed participant total,
     set `Event.expected_participants` at ingestion so the
     report shows accurate "Found / Total" coverage.

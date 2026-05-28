"""v2 full pipeline demo, expanded.

  1. Seed the xTech event (always — needed for recap fixtures).
  2. (Optional) Pull NavalX / DARPA / USA.gov / SBIR events live.
     Live fetches will silently no-op if sites 403 the sandbox.
  3. Run discovery → recap scrape against the fixtures.
  4. Run Crunchbase enrichment (offline backend, fixture).
  5. Build the seven-section markdown report.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure offline backends BEFORE importing modules that read env
os.environ["SEARCH_BACKEND"] = "offline"
os.environ["OFFLINE_SEARCH_FIXTURE"] = str(
    PROJECT_ROOT / "tests" / "fixtures" / "search_results.json"
)
os.environ["OFFLINE_FETCH_MAP"] = str(
    PROJECT_ROOT / "tests" / "fixtures" / "fetch_map.json"
)
os.environ["CRUNCHBASE_BACKEND"] = "offline"
os.environ["OFFLINE_CRUNCHBASE_FIXTURE"] = str(
    PROJECT_ROOT / "tests" / "fixtures" / "crunchbase_offline.json"
)
os.environ["FEDRAMP_BACKEND"] = "offline"
os.environ["OFFLINE_FEDRAMP_FIXTURE"] = str(
    PROJECT_ROOT / "tests" / "fixtures" / "fedramp_offline.json"
)

from enrich.compliance import enrich_all as enrich_compliance
from enrich.crunchbase import enrich_all as enrich_crunchbase
from reports.build_markdown import build
from schema.event import Event, make_event_id
from sources.discover import discover_for_event
from store import cache as store


def reset_db() -> None:
    if store.DB_PATH.exists():
        store.DB_PATH.unlink()
    with store.connect():
        pass


def seed_xtech_event() -> str:
    e = Event(
        id=make_event_id(
            "U.S. Army FUZE xTech Program",
            date(2026, 5, 2),
            "xTech National Security Hackathon",
        ),
        name="xTech National Security Hackathon",
        aliases=["xTech Hackathon", "National Security Hackathon"],
        host="U.S. Army FUZE xTech Program",
        dates_start=date(2026, 5, 2),
        dates_end=date(2026, 5, 3),
        location="San Francisco, CA",
        source_url="https://xtech.army.mil/competition/xtech-hackathon/",
    )
    store.upsert_event(e.to_dict())
    return e.id


def try_live_event_sources() -> dict:
    """Attempt the four new event adapters. Each returns [] if its
    target site is unreachable; the demo doesn't fail."""
    counts = {"navalx": 0, "darpa": 0, "usagov": 0, "sbir": 0}
    try:
        from sources.navalx import fetch_events as navalx_fetch
        events = navalx_fetch()
        for e in events:
            store.upsert_event(e.to_dict())
        counts["navalx"] = len(events)
    except Exception as exc:
        logging.warning("navalx adapter failed: %s", exc)
    try:
        from sources.darpa import fetch_events as darpa_fetch
        events = darpa_fetch()
        for e in events:
            store.upsert_event(e.to_dict())
        counts["darpa"] = len(events)
    except Exception as exc:
        logging.warning("darpa adapter failed: %s", exc)
    try:
        from sources.usagov_challenges import fetch_events as usagov_fetch
        events = usagov_fetch()
        for e in events:
            store.upsert_event(e.to_dict())
        counts["usagov"] = len(events)
    except Exception as exc:
        logging.warning("usagov adapter failed: %s", exc)
    try:
        from sources.sbir_gov import fetch_dod_solicitations
        events = fetch_dod_solicitations(only_open=True)
        for e in events:
            store.upsert_event(e.to_dict())
        counts["sbir"] = len(events)
    except Exception as exc:
        logging.warning("sbir adapter failed: %s", exc)
    return counts


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(message)s")
    print("=" * 72)
    print("v2 expanded pipeline demo")
    print("  events: xTech (fixture) + NavalX / DARPA / USA.gov / SBIR (live)")
    print("  enrichment: Crunchbase (offline fixture)")
    print("=" * 72)

    reset_db()
    xtech_id = seed_xtech_event()
    print(f"\nseeded xTech event: {xtech_id}\n")

    print("--- Step 1: try live event sources (best-effort) ---")
    live_counts = try_live_event_sources()
    for src, n in live_counts.items():
        print(f"  {src}: {n} events")
    print()

    print("--- Step 2: discovery + recap scraping for xTech event ---")
    summary = discover_for_event(xtech_id)
    for k, v in summary.items():
        if k != "errors":
            print(f"  {k}: {v}")
    print()

    print("--- Step 3: Crunchbase enrichment (weekly cadence in prod) ---")
    cb_summary = enrich_crunchbase()
    for k, v in cb_summary.items():
        print(f"  {k}: {v}")
    print()

    print("--- Step 4: Compliance enrichment (monthly cadence in prod) ---")
    # Skip the live USASpending call in the demo — it'd 403 from this
    # sandbox the same way the other live sources did. Pass
    # do_usaspending=False so the run completes against the offline
    # FedRAMP fixture only.
    comp_summary = enrich_compliance(do_usaspending=False)
    for k, v in comp_summary.items():
        print(f"  {k}: {v}")
    print()

    print("--- Step 5: build markdown report ---")
    text = build(since=date(2026, 4, 1))
    out_dir = PROJECT_ROOT / "reports" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_v2_{date.today().isoformat()}.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"  wrote {out_path} ({len(text):,} chars)\n")

    # Print just the new Section 2 (funding columns should now populate)
    print("--- Section 2 preview (after Crunchbase enrichment) ---")
    in_section_2 = False
    for line in text.splitlines():
        if line.startswith("## Section 2"):
            in_section_2 = True
        elif in_section_2 and line.startswith("## "):
            break
        if in_section_2:
            print(line)


if __name__ == "__main__":
    main()

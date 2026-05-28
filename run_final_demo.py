"""Final v2 integrated demo — all layers.

  1. Seed xTech event (anchor for fixtures).
  2. Try live event adapters (best-effort; sandbox 403s degrade
     gracefully).
  3. Discovery + recap scrape (offline fixtures).
  4. Crunchbase enrichment (offline fixture).
  5. Compliance enrichment (FedRAMP offline fixture +
     USASpending skipped since the sandbox can't reach it).
  6. Monthly diff queries.
  7. Build full seven-section markdown + monthly tracking sections.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Offline configuration
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

from analysis.monthly_diff import run_all as run_monthly_diff
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


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(message)s")
    print("=" * 72)
    print("v2 final integrated demo — all enrichment layers active")
    print("=" * 72)

    reset_db()
    xtech_id = seed_xtech_event()
    print(f"\nseeded xTech event: {xtech_id}\n")

    print("--- Step 1: discovery + recap scrape ---")
    summary = discover_for_event(xtech_id)
    for k, v in summary.items():
        if k != "errors":
            print(f"  {k}: {v}")
    print()

    print("--- Step 2: Crunchbase enrichment ---")
    cb = enrich_crunchbase()
    for k, v in cb.items():
        print(f"  {k}: {v}")
    print()

    print("--- Step 3: Compliance enrichment (USASpending skipped) ---")
    cmp_ = enrich_compliance(skip_usaspending=True)
    for k, v in cmp_.items():
        print(f"  {k}: {v}")
    print()

    print("--- Step 4: Monthly diff queries ---")
    diff = run_monthly_diff(since=date(2026, 4, 1))
    print(f"  new_companies: {len(diff['new_companies'])}")
    print(f"  increasing_frequency: {len(diff['increasing_frequency'])}")
    print(f"  transitions: {len(diff['transitions'])}")
    print(f"  recurring_supporters: {len(diff['recurring_supporters'])}")
    print()

    print("--- Step 5: Build final report ---")
    text = build(since=date(2026, 4, 1))
    out_dir = PROJECT_ROOT / "reports" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_final_{date.today().isoformat()}.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"  wrote {out_path} ({len(text):,} chars)\n")

    # Quick previews of the new sections
    print("--- Section 5 preview (Compliance) ---")
    _print_section(text, "Section 5")
    print("\n--- Monthly tracking preview ---")
    _print_section(text, "Monthly tracking")


def _print_section(text: str, header: str) -> None:
    in_section = False
    for line in text.splitlines():
        if line.startswith(f"## {header}"):
            in_section = True
        elif in_section and line.startswith("## "):
            break
        if in_section:
            print(line)


if __name__ == "__main__":
    main()

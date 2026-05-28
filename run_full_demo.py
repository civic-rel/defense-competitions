"""Full v2 pipeline demo.

  1. Seed event.
  2. Configure offline search backend + offline fetch map.
  3. Run discovery — search returns canned URLs from the fixture.
  4. Each URL is fetched (locally via fetch_map) → HTML scraped.
  5. NER + matching + confidence → Participation rows.
  6. Build the seven-section markdown report.

This is the same flow that runs in production with SEARCH_BACKEND=brave
and live fetches — only the two offline override env vars differ.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure offline mode BEFORE importing modules that read env
os.environ["SEARCH_BACKEND"] = "offline"
os.environ["OFFLINE_SEARCH_FIXTURE"] = str(
    PROJECT_ROOT / "tests" / "fixtures" / "search_results.json"
)
os.environ["OFFLINE_FETCH_MAP"] = str(
    PROJECT_ROOT / "tests" / "fixtures" / "fetch_map.json"
)

from schema.event import Event, make_event_id
from sources.discover import discover_for_event
from reports.build_markdown import build
from store import cache as store


def reset_db() -> None:
    if store.DB_PATH.exists():
        store.DB_PATH.unlink()
    with store.connect():
        pass


def seed_event() -> str:
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
    print("=" * 70)
    print("v2 full pipeline demo — discover → scrape → store → report")
    print("=" * 70)

    reset_db()
    event_id = seed_event()
    print(f"\nseeded event: {event_id}\n")

    print("--- Step 1: discover_for_event ---")
    summary = discover_for_event(event_id)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print()

    print("--- Step 2: build markdown report ---")
    text = build(since=date(2026, 4, 1))
    out_dir = PROJECT_ROOT / "reports" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report_{date.today().isoformat()}.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"  wrote {out_path} ({len(text):,} chars)\n")

    print("--- Preview (first 1500 chars) ---")
    print(text[:1500])
    print("...\n")


if __name__ == "__main__":
    main()

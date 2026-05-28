"""Demo: run the recap pipeline end-to-end against the xTech
Hackathon (May 2-3, 2026) using three fixture documents:

  1. The official xtech.army.mil RFI excerpt (confirmed-tier source).
  2. A Maggie Gray substack first-person organizer post
     (highly_likely-tier, first-person hint).
  3. A simulated DefenseScoop recap article (highly_likely-tier
     editorial recap).

The simulated DefenseScoop article uses real company names mapped
to illustrative placements — do not treat the winner ordering as
fact. The point is to demonstrate the extraction and confidence
pipeline against named-entity content.

After processing, prints:
  - companies discovered
  - participations written, grouped by confidence
  - review queue entries
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Make the v2 prototype importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from schema.event import Event, make_event_id
from sources.recap_scraper import process_html
from store import cache as store


def reset_db() -> None:
    """Wipe the store so the demo is reproducible."""
    if store.DB_PATH.exists():
        store.DB_PATH.unlink()
    # Re-initialize empty
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
        aliases=[
            "xTech Hackathon",
            "National Security Hackathon",
            "NatSec Hackathon",
            "3rd Annual National Security Hackathon",
        ],
        host="U.S. Army FUZE xTech Program",
        dates_start=date(2026, 5, 2),
        dates_end=date(2026, 5, 3),
        location="San Francisco, CA",
        source_url="https://xtech.army.mil/competition/xtech-hackathon/",
    )
    store.upsert_event(e.to_dict())
    return e.id


def run() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(levelname)s %(message)s")

    print("=" * 70)
    print("v2 recap-scraper prototype — xTech Hackathon (May 2-3, 2026)")
    print("=" * 70)

    reset_db()
    event_id = seed_event()
    print(f"\nSeeded event id: {event_id}\n")

    fixtures_dir = Path(__file__).parent / "tests" / "fixtures"
    fixtures = [
        {
            "path": fixtures_dir / "xtech_official.html",
            "url": "https://xtech.army.mil/competition/xtech-hackathon/",
            "extracted_by": "recap_scraper:program_official",
            "has_named_author": False,
        },
        {
            "path": fixtures_dir / "maggiegray_substack.html",
            "url": "https://maggiegray.us/p/2026-national-security-hackathon",
            "extracted_by": "recap_scraper:substack_organizer",
            "has_named_author": True,
        },
        {
            "path": fixtures_dir / "defensescoop_recap.html",
            "url": "https://defensescoop.com/2026/05/04/xtech-natsec-hackathon-winners-illustrative/",
            "extracted_by": "recap_scraper:defensescoop",
            "has_named_author": True,
        },
    ]

    all_participations: list[dict] = []
    for fix in fixtures:
        print(f"--- Processing: {fix['path'].name}")
        print(f"    URL: {fix['url']}")
        html = fix["path"].read_text(encoding="utf-8")
        ps = process_html(
            html=html,
            evidence_url=fix["url"],
            event_id=event_id,
            extracted_by=fix["extracted_by"],
            has_named_author=fix["has_named_author"],
        )
        print(f"    → {len(ps)} participation rows written\n")
        all_participations.extend(ps)

    # Summary
    print("=" * 70)
    print("Companies discovered")
    print("=" * 70)
    companies = store.load_companies()
    print(f"{len(companies)} companies in store:\n")
    for c in sorted(companies, key=lambda x: x["name"]):
        stealth = " (STEALTH)" if c.get("is_stealth") else ""
        print(f"  • {c['name']}{stealth}")

    print()
    print("=" * 70)
    print("Participations by confidence")
    print("=" * 70)
    parts = store.load_participations(event_id=event_id)
    buckets: dict[str, list[dict]] = {
        "confirmed": [], "highly_likely": [], "ecosystem_associated": [],
    }
    for p in parts:
        buckets.setdefault(p["confidence"], []).append(p)

    # Map company_id → name for readable output
    by_id = {c["id"]: c["name"] for c in companies}

    for conf in ("confirmed", "highly_likely", "ecosystem_associated"):
        rows = buckets[conf]
        print(f"\n{conf}: {len(rows)} rows")
        # Deduplicate by (company, role) for display
        seen = set()
        for p in rows:
            key = (p["company_id"], p["role"])
            if key in seen:
                continue
            seen.add(key)
            name = by_id.get(p["company_id"], "?")
            print(f"  [{p['role']:11}] {name}")
            print(f"               via {p['evidence_url']}")

    # Review queue
    print()
    print("=" * 70)
    print("Review queue")
    print("=" * 70)
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM review_queue WHERE resolution IS NULL"
        ).fetchall()
    if not rows:
        print("(empty)")
    else:
        for r in rows:
            print(f"  candidate={r['candidate_name']!r}")
            print(f"    nearest={r['nearest_match']}  sim={r['similarity']:.2f}")
            print(f"    url={r['evidence_url']}")

    # Per-company evidence rollup — preview of Section 7
    print()
    print("=" * 70)
    print("Section 7 preview — evidence appendix")
    print("=" * 70)
    by_company: dict[str, list[dict]] = {}
    for p in parts:
        by_company.setdefault(p["company_id"], []).append(p)
    for cid, rows in sorted(by_company.items(), key=lambda kv: by_id.get(kv[0], "")):
        name = by_id.get(cid, cid)
        print(f"\n{name}")
        seen_urls = set()
        for p in rows:
            tag = f"({p['confidence']}, {p['role']})"
            if p["evidence_url"] in seen_urls:
                continue
            seen_urls.add(p["evidence_url"])
            print(f"  {tag} {p['evidence_url']}")
            print(f"    \"{p['evidence_excerpt']}\"")

    print()


if __name__ == "__main__":
    run()

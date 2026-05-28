"""NavalX adapter.

Scrapes events from the NavalX events page:
  https://navalx.nre.navy.mil/s/events

NavalX events include MilTech Mashup, Crucible accelerator programs,
Tech Bridge industry days, and Symposium events. Each is a real
industry-engagement event the brief targets.

NavalX uses a Salesforce-backed site (s.com path patterns) — most of
the event data is in the HTML on first paint. If it ever becomes
fully JS-rendered, swap to Playwright (see jhuapl_eventlink.py).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Iterator

import httpx
from selectolax.parser import HTMLParser

from normalize_helpers import parse_date_loose
from schema.event import Event, make_event_id
from store import cache as store

log = logging.getLogger(__name__)

EVENTS_URL = "https://navalx.nre.navy.mil/s/events"

# NavalX events look like "MilTech Mashup" or "Crucible Ignitor 2026"
# — typical date patterns include "May 15-17, 2026" or "October 2025".
DATE_RE = re.compile(
    r"([A-Z][a-z]{2,9}\s+\d{1,2}(?:-\d{1,2})?,?\s+\d{4})"
)

# Words that indicate it's an event card vs. nav/footer junk
EVENT_HINTS = re.compile(
    r"(?:mashup|ignitor|crucible|industry day|symposium|tech bridge|"
    r"hackathon|pitch|innovation forum|expo|workshop)",
    re.I,
)


def _fetch(url: str) -> str | None:
    cached = store.cache_get(url)
    if cached:
        return cached.decode("utf-8", errors="replace")
    try:
        r = httpx.get(url, headers={"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        )}, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("[navalx] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def fetch_events() -> list[Event]:
    """Return a list of NavalX events from the index page.

    The page structure has been stable: each event card has a title
    in an h3 and a date string nearby. We pull title + first date
    we see in the same card.
    """
    html = _fetch(EVENTS_URL)
    if not html:
        return []
    doc = HTMLParser(html)

    events: list[Event] = []
    seen_titles: set[str] = set()

    # Each event card lives under .slds-card or similar Salesforce
    # widget — but we don't depend on that selector. Walk h-tags
    # and check whether the surrounding text matches event-card hints.
    for heading in doc.css("h1, h2, h3, h4"):
        title = (heading.text(strip=True) or "").strip()
        if not title or len(title) < 6 or title in seen_titles:
            continue
        if not EVENT_HINTS.search(title):
            continue
        # Look at the next ~600 chars of nearby text for a date
        # (selectolax doesn't give easy DOM-walking, so we use the
        # parent's full text)
        parent = heading.parent
        if not parent:
            continue
        nearby = parent.text(separator=" ", strip=True)[:800]
        date_match = DATE_RE.search(nearby)
        if not date_match:
            continue
        dates_start = parse_date_loose(date_match.group(1).split("-")[0])
        if not dates_start:
            continue
        seen_titles.add(title)
        events.append(Event(
            id=make_event_id("NavalX", dates_start, title),
            name=title,
            aliases=[],
            host="NavalX",
            dates_start=dates_start,
            location="",
            source_url=EVENTS_URL,
        ))

    log.info("[navalx] parsed %d events", len(events))
    return events


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events():
        print(f"  {e.dates_start}  {e.name[:70]}")

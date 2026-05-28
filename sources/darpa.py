"""DARPA adapter.

Scrapes the DARPA events index and follows each event's detail page.
DARPA proposers days and industry days are the canonical "early
notice" signal — competitors register here before SAM.gov solicitations
post. Each event detail page reliably contains:

  - Title
  - Event type (Proposers Day, Industry Day, Information Session)
  - Date and location
  - Registration deadline
  - Program acronym (in the title)

These are exactly the events the OSINT brief targets — "DARPA
challenge programs" — even when they're not formally prize challenges,
because they identify the universe of competitors *before* the
solicitation creates a SAM.gov record.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import httpx
from selectolax.parser import HTMLParser

from normalize_helpers import parse_date_loose
from schema.event import Event, make_event_id
from store import cache as store

log = logging.getLogger(__name__)

# darpa.mil/events is Drupal-rendered client-side and contains no
# event links in the initial HTML. The sitemap is the stable contract.
SITEMAP_URL = "https://www.darpa.mil/sitemap.xml"
EVENT_URL_RE = re.compile(
    r"<loc>(https://www\.darpa\.mil/events/\d{4}/[^<]+)</loc>"
)

# DARPA event types we care about (vs. recruiting events etc.)
EVENT_TYPE_HINTS = re.compile(
    r"(proposers? day|industry day|information session|industry session|"
    r"workshop|innovation forum)",
    re.I,
)

DATE_RE = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2}(?:[-,]\s*\d{1,2})?,?\s+\d{4})"
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
        log.error("[darpa] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def fetch_events() -> list[Event]:
    """Return DARPA events discovered via sitemap.xml.

    DARPA's /events page is now Drupal-rendered client-side, so the
    initial HTML has no event links. The sitemap publishes per-event
    URLs at /events/<year>/<slug>, which is what we consume.
    """
    xml = _fetch(SITEMAP_URL)
    if not xml:
        return []

    urls = sorted(set(EVENT_URL_RE.findall(xml)))
    log.info("[darpa] discovered %d candidate event URLs from sitemap", len(urls))

    events: list[Event] = []
    for url in urls:
        event = _parse_event_page(url)
        if event:
            events.append(event)
    log.info("[darpa] parsed %d events", len(events))
    return events


def _parse_event_page(url: str) -> Event | None:
    html = _fetch(url)
    if not html:
        return None
    doc = HTMLParser(html)
    h1 = doc.css_first("h1")
    title = (h1.text(strip=True) if h1 else "").strip()
    if not title or len(title) < 4:
        return None
    page_text = doc.text(separator=" ", strip=True)

    # Filter to event-types we care about
    if not EVENT_TYPE_HINTS.search(page_text):
        log.debug("[darpa] skipping non-event-type page %s", url)
        return None

    date_match = DATE_RE.search(page_text)
    if not date_match:
        return None
    dates_start = parse_date_loose(date_match.group(1).split("-")[0])
    if not dates_start:
        return None

    # Location hint — DARPA Conference Center / Executive Conference
    # Center / Virtual / hybrid
    location = ""
    for loc_hint in (
        "DARPA Conference Center", "Executive Conference Center",
        "Virtual", "Arlington, Va", "Arlington, VA",
    ):
        if loc_hint in page_text:
            location = loc_hint
            break

    # Aliases: DARPA programs are referred to by both their acronym
    # (in the URL/slug) and full name. Extract slug as alias.
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    aliases = [slug] if slug and slug != title.lower() else []

    return Event(
        id=make_event_id("DARPA", dates_start, title),
        name=title,
        aliases=aliases,
        host="DARPA",
        dates_start=dates_start,
        location=location or "",
        source_url=url,
    )


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events():
        print(f"  {e.dates_start}  {e.name[:70]}")

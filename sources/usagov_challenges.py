"""USA.gov challenges adapter.

Challenge.gov was decommissioned and active competitions migrated
to USA.gov's Innovation section. The adapter scrapes:

  https://www.usa.gov/challenges

Each challenge has a per-page detail URL like:
  https://www.usa.gov/challenges/<slug>

We filter to defense-relevant challenges by host agency tags:
DoD, DoW, Army, Navy, Air Force, Space Force, DARPA, DIU, NSA,
NGA, and SOCOM.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from normalize_helpers import parse_date_loose
from schema.event import Event, make_event_id
from store import cache as store

log = logging.getLogger(__name__)

INDEX_URL = "https://www.usa.gov/challenges"

DEFENSE_HOSTS = re.compile(
    r"(department of (?:defense|war|the army|the navy|the air force)|"
    r"u\.s\. (?:army|navy|air force|space force|marine corps)|"
    r"darpa|defense innovation unit|diu|"
    r"national security agency|nsa|"
    r"national geospatial-intelligence agency|nga|"
    r"special operations command|socom|"
    r"defense logistics agency|dla)",
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
        log.error("[usagov] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def fetch_events() -> list[Event]:
    html = _fetch(INDEX_URL)
    if not html:
        return []
    doc = HTMLParser(html)

    urls: set[str] = set()
    for a in doc.css("a[href*='/challenges/']"):
        href = (a.attributes.get("href") or "").strip()
        full = urljoin(INDEX_URL, href)
        if full.rstrip("/") == INDEX_URL.rstrip("/"):
            continue
        urls.add(full)

    log.info("[usagov] %d challenge candidates", len(urls))

    events: list[Event] = []
    for url in sorted(urls):
        event = _parse_challenge(url)
        if event:
            events.append(event)
    log.info("[usagov] %d defense-relevant challenges", len(events))
    return events


def _parse_challenge(url: str) -> Event | None:
    html = _fetch(url)
    if not html:
        return None
    doc = HTMLParser(html)
    h1 = doc.css_first("h1")
    title = (h1.text(strip=True) if h1 else "").strip()
    if not title:
        return None
    page_text = doc.text(separator=" ", strip=True)

    # Defense-host filter — must mention a defense agency
    if not DEFENSE_HOSTS.search(page_text):
        return None

    # Host: pull the first defense agency mention
    host_match = DEFENSE_HOSTS.search(page_text)
    host = host_match.group(0).title() if host_match else "USA.gov"

    date_match = DATE_RE.search(page_text)
    dates_start = parse_date_loose(date_match.group(1).split("-")[0]) if date_match else None
    if not dates_start:
        return None

    return Event(
        id=make_event_id(host, dates_start, title),
        name=title,
        aliases=[],
        host=host,
        dates_start=dates_start,
        location="",
        source_url=url,
    )


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events():
        print(f"  {e.dates_start}  {e.host[:30]}  {e.name[:60]}")

"""SOFWERX adapter.

events.sofwerx.org publishes a Strapi-driven sitemap at
https://strapi.sofwerx.org/api/sitemap/index.xml listing every
event page. We mirror the DIU/AFWERX pattern: pull the URL list,
fetch each event page, derive an Event from the H1 title (engagement-
keyword check), and run recap_scraper NER on the body.

Typical SOFWERX events: Capability Engagements, Assessment Events
(AEs), Tech Sprints, Innovation Foundry sessions, SBIR releases,
Combatant Craft Division subsystem evals, JSOU assessments.

Public API:
  fetch_events()  -> list[Event]
"""

from __future__ import annotations

import logging
import re
from datetime import date

import httpx

from schema.event import Event
from store import cache as store

log = logging.getLogger(__name__)

SITEMAP_URL = "https://strapi.sofwerx.org/api/sitemap/index.xml"
EVENT_URL_RE = re.compile(
    r"<loc>(https://events\.sofwerx\.org/[^<]+)</loc>"
)
# Pages on events.sofwerx.org that aren't individual events:
_EVENT_INDEX_PATHS = frozenset({
    "", "/", "/discover", "/external-events", "/ussocom-sponsored-events-exercises",
})


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
        log.error("[sofwerx] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def _looks_like_event_url(url: str) -> bool:
    """Drop index/landing pages — keep only per-event slugs."""
    path = url.replace("https://events.sofwerx.org", "").rstrip("/")
    if path in _EVENT_INDEX_PATHS:
        return False
    return True


def fetch_events() -> list[Event]:
    from sources.discover import (
        _discover_event_from_article,
        _is_non_article_url,
    )
    from sources.recap_scraper import process_html

    xml = _fetch(SITEMAP_URL)
    if not xml:
        return []
    urls = sorted({u for u in EVENT_URL_RE.findall(xml) if _looks_like_event_url(u)})
    log.info("[sofwerx] %d event URLs in sitemap", len(urls))

    events: list[Event] = []
    written = 0
    for url in urls:
        if _is_non_article_url(url):
            continue
        html = _fetch(url)
        if not html:
            continue
        ev = _discover_event_from_article(html, url)
        if not ev:
            log.debug("[sofwerx] skip non-event-titled page: %s", url)
            continue
        process_html(
            html=html,
            evidence_url=url,
            event_id=ev["id"],
            extracted_by="sofwerx_adapter",
            has_named_author=True,
        )
        events.append(Event(
            id=ev["id"],
            name=ev["name"],
            aliases=ev.get("aliases") or [],
            host=ev.get("host", ""),
            dates_start=date.fromisoformat(ev["dates_start"]),
            dates_end=date.fromisoformat(ev["dates_end"]) if ev.get("dates_end") else None,
            location=ev.get("location", ""),
            source_url=ev.get("source_url", ""),
        ))
        written += 1
    log.info("[sofwerx] %d events parsed", written)
    return events


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events()[:15]:
        print(f"  {e.dates_start}  {e.name[:80]}")

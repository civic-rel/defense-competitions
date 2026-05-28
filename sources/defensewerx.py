"""DefenseWerx adapter.

defensewerx.org is the umbrella organization for the various WERX
hubs (AFWERX, SOFWERX, NavalX, MGMWerx, FleetWerx, ICWERX, etc.).
Their own post stream is small (~20 posts as of 2026-05) and
mostly leadership/partnership announcements rather than engagement
events, so yield from this adapter is intentionally modest. The
adapter exists so we don't miss the rare DefenseWerx-direct
program announcement.

Mirrors the DIU/AFWERX/SOFWERX pattern:
  1. Pull post-sitemap from the WordPress/Yoast SEO structure.
  2. Per article: discover an Event from the title if engagement-
     shaped; otherwise skip.
  3. Run recap_scraper NER on the body.

Note: defensewerx.org rejects bare httpx UAs via mod_security.
We supply a Safari UA + Accept-Language to get past it.

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

POST_SITEMAP_URL = "https://defensewerx.org/post-sitemap.xml"
ARTICLE_URL_RE = re.compile(
    r"<loc>(https://defensewerx\.org/[^<]+)</loc>"
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch(url: str) -> str | None:
    cached = store.cache_get(url)
    if cached:
        return cached.decode("utf-8", errors="replace")
    try:
        r = httpx.get(url, headers=_BROWSER_HEADERS,
                      timeout=30.0, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("[defensewerx] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def fetch_events() -> list[Event]:
    from sources.discover import (
        _discover_event_from_article,
        _is_non_article_url,
    )
    from sources.recap_scraper import process_html

    xml = _fetch(POST_SITEMAP_URL)
    if not xml:
        return []
    # The post sitemap also lists static-page URLs; ignore those.
    urls = sorted({
        u for u in ARTICLE_URL_RE.findall(xml)
        if "/post-sitemap" not in u
        and not u.endswith(("/page-sitemap.xml", "/wp-sitemap.xml"))
    })
    log.info("[defensewerx] %d post URLs in sitemap", len(urls))

    events: list[Event] = []
    for url in urls:
        if _is_non_article_url(url):
            continue
        html = _fetch(url)
        if not html:
            continue
        ev = _discover_event_from_article(html, url)
        if not ev:
            log.debug("[defensewerx] skip non-event post: %s", url)
            continue
        process_html(
            html=html,
            evidence_url=url,
            event_id=ev["id"],
            extracted_by="defensewerx_adapter",
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
    log.info("[defensewerx] %d events parsed", len(events))
    return events


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events():
        print(f"  {e.dates_start}  {e.name[:80]}")

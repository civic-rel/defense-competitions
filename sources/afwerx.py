"""AFWERX adapter.

afwerx.com is a WordPress site with a Rank Math SEO news sitemap
listing every /news/<slug> article. We use the same article-title-
based event discovery + recap_scraper NER pipeline as the DIU
adapter: each engagement-event-shaped article becomes an Event,
and the body's named companies become Participations.

No Brave queries used; data comes straight from AFWERX's news
feed (program-official source).

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

NEWS_SITEMAP_URL = "https://afwerx.com/news-sitemap.xml"
ARTICLE_URL_RE = re.compile(
    r"<loc>(https://afwerx\.com/news/[^<]+)</loc>"
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
        log.error("[afwerx] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def fetch_events() -> list[Event]:
    """Pull AFWERX news articles, derive engagement Events from each
    article whose title looks like an event, run recap NER on the
    body. Returns the list of Events created."""
    from sources.discover import (
        _discover_event_from_article,
        _is_non_article_url,
    )
    from sources.recap_scraper import process_html

    xml = _fetch(NEWS_SITEMAP_URL)
    if not xml:
        return []
    urls = sorted(set(ARTICLE_URL_RE.findall(xml)))
    log.info("[afwerx] %d news articles in sitemap", len(urls))

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
            log.debug("[afwerx] skip non-event article: %s", url)
            continue
        process_html(
            html=html,
            evidence_url=url,
            event_id=ev["id"],
            extracted_by="afwerx_adapter",
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
    log.info("[afwerx] %d events discovered from articles", written)
    return events


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events()[:15]:
        print(f"  {e.dates_start}  {e.name[:80]}")

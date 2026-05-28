"""DIU (Defense Innovation Unit) adapter.

diu.mil/latest is the program-official news feed. Each article is
typically about a single DIU competition, prize challenge, prototype
selection, or program announcement — and the body names specific
companies (finalists, winners, awardees).

Unlike xTech, DIU doesn't expose a participant sitemap. We scrape
the /latest listing for article URLs, then for each article:

  1. Auto-discover an Event from the article's H1 title (using the
     shared `sources.discover._discover_event_from_article` helper —
     same logic as Brave-driven discovery, just without Brave).
  2. Run the recap_scraper NER + matcher to write Participations
     against that auto-discovered event.

Result: every named company in every DIU news article ends up in
the store, tied to the specific DIU event that named them. No
Brave queries used.

Public API:
  fetch_events()  -> list[Event]  (returns events discovered from articles)
"""

from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from schema.event import Event
from store import cache as store

log = logging.getLogger(__name__)

LATEST_URL = "https://www.diu.mil/latest"
LATEST_LINK_RE = re.compile(r'href="(/latest/[^"]+)"')


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
        log.error("[diu] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


def _article_urls() -> list[str]:
    """Pull all /latest/<slug> article URLs from the news index."""
    html = _fetch(LATEST_URL)
    if not html:
        return []
    paths = set(LATEST_LINK_RE.findall(html))
    # Drop the bare /latest landing page itself
    paths.discard("/latest")
    paths.discard("/latest/")
    return sorted(urljoin(LATEST_URL, p) for p in paths)


def fetch_events() -> list[Event]:
    """Discover DIU events from /latest articles. For each article
    whose title looks like an engagement event (challenge / sprint /
    finalists selected / awarded / etc.), upsert an Event and run
    the recap_scraper to write Participations directly.

    Returns the list of Events created.
    """
    # Local imports to avoid a circular dependency at module load.
    from sources.discover import (
        _discover_event_from_article,
        _is_non_article_url,
    )
    from sources.recap_scraper import process_html

    urls = _article_urls()
    log.info("[diu] %d /latest article URLs discovered", len(urls))

    events: list[Event] = []
    written = 0
    for url in urls:
        if _is_non_article_url(url):
            continue
        html = _fetch(url)
        if not html:
            continue
        # Use the shared title-based event discovery so the resulting
        # Event row has the same shape as Brave-discovered ones.
        ev = _discover_event_from_article(html, url)
        if not ev:
            # Article exists but its title isn't engagement-event-shaped
            # (e.g., a leadership announcement). Skip it — we don't want
            # to attribute company mentions to a non-event article.
            log.debug("[diu] skip non-event article: %s", url)
            continue
        # process_html writes Participations against the auto-discovered event.
        process_html(
            html=html,
            evidence_url=url,
            event_id=ev["id"],
            extracted_by="diu_adapter",
            has_named_author=True,  # diu.mil is program-official
        )
        # Rehydrate Event dataclass from the dict (for the caller's list).
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
    log.info("[diu] %d events discovered from articles", written)
    return events


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    for e in fetch_events():
        print(f"  {e.dates_start}  {e.name[:80]}")

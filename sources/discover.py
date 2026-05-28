"""Discover recap articles for tracked events, then process them.

Given an Event from the store, this module:

  1. Loads the editorial sources config (config/recap_sources.yaml).
  2. For each editorial source, builds queries from the event name
     and any aliases.
  3. Runs each query through the configured SearchBackend.
  4. Filters hits to the date window (default ±3/+21 days around
     dates_end).
  5. Fetches each URL (with the 24h cache).
  6. Passes the HTML to recap_scraper.process_html() with the
     per-source confidence floor and extracted_by tag.

The discovery loop is the link between "we have an event in the
store" and "we have participation rows backed by evidence." It's
the only piece that touches a search API; everything downstream
is deterministic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser

from sources.recap_scraper import process_html
from sources.search_backend import SearchBackend, get_backend, SearchResult
from store import cache as store

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "recap_sources.yaml"

# URL shapes that are NOT single-article pages — they aggregate content
# across many topics (author profiles, section indexes, tag/category
# landing pages, daily/weekly digests, RSS feeds). Brave often returns
# these and the recap_scraper would otherwise extract every company on
# the page and stamp them all with the seed event_id, producing wrong
# attributions (see AeroVironment / xTech misattribution case).
_NON_ARTICLE_URL_RE = re.compile(
    r"/(?:authors?|topics?|categor(?:y|ies)|tags?|page/\d+)/"
    r"|/(?:insider|news|articles|stories|press|media|events|blog)/?$"
    r"|digest"
    r"|\.(?:xml|rss|atom)(?:\?|$)",
    re.IGNORECASE,
)


def _is_non_article_url(url: str) -> bool:
    """True if `url` looks like a section/author/digest page rather than
    a single-article recap."""
    return bool(_NON_ARTICLE_URL_RE.search(url))


# Kept as a re-export so existing report code that imports
# LOOSE_EVENT_ID doesn't error. We no longer create or attribute to a
# sentinel event — articles either tie to a seed event, an event
# auto-discovered from the article's title, or get dropped entirely.
LOOSE_EVENT_ID = "loose_defense_industry"


# Words that, when present in an article's <title>/<h1>, indicate the
# article is about a real defense engagement event (not just a general
# news piece). Used by _discover_event_from_article() to decide whether
# to auto-create an Event when an article doesn't match the seed.
_ENGAGEMENT_KEYWORDS_RE = re.compile(
    r"\b("
    r"hackathon|hack-a-thon|prize\s+challenge|prize|sprint|tem|rca|"
    r"industry\s+day|proposers?\s+day|demo\s+day|industry\s+session|"
    r"information\s+session|innovation\s+forum|innovation\s+challenge|"
    r"innovation\s+foundry|"
    r"competition|cohort|finalists?|qualifying\s+event|"
    r"showcase|tech\s+sprint|tech\s+challenge|"
    r"collaboration\s+event|assessment\s+event|"
    r"pitch\s+day|pitch\s+competition|"
    r"summit|expo|fair|forum|workshop|symposium|"
    r"mashup|crucible|ignitor|bridge\s+(?:industry|tech)|"
    r"(?:special\s+notice|combined\s+synopsis|sources\s+sought)|"
    # DIU / SAM.gov / "X companies selected" announcement patterns —
    # these are real participant-naming articles even though the
    # title doesn't say "challenge" or "hackathon".
    r"selected\s+(?:to|for|as)|"
    r"winners?\s+(?:announced|selected)|"
    r"awarded(?:\s+(?:to|prototype|ot|other\s+transaction))?"
    r")\b",
    re.I,
)


_CHROME_H1_BLACKLIST = frozenset({
    "contact us", "contact", "menu", "navigation", "home", "search",
    "sign in", "sign up", "login", "subscribe", "share", "main menu",
    "primary menu", "skip to content", "newsletter", "feedback",
})


def _extract_article_title(html: str) -> Optional[str]:
    """Return the article's most representative title.

    Tries in order: scoped H1s (article/main), unscoped H1, og:title
    meta, <title>. H1s matching the chrome blacklist (e.g. "contact
    us" on Strapi/SPA pages) are skipped so the real title in og:title
    or <title> wins. <title> values like "Real Title | Site Name" are
    split on common separators to drop the site-name suffix.
    """
    doc = HTMLParser(html)

    def _clean(s: str | None) -> str:
        return re.sub(r"\s+", " ", (s or "").strip())

    # 1-3. H1 variants
    for selector in ("article h1", "main h1", "h1"):
        el = doc.css_first(selector)
        if not el:
            continue
        txt = _clean(el.text(strip=True))
        if not txt or len(txt) < 6 or len(txt) > 250:
            continue
        if txt.lower() in _CHROME_H1_BLACKLIST:
            continue
        return txt

    # 4. og:title (set by most SPAs)
    og = doc.css_first('meta[property="og:title"]')
    if og:
        txt = _clean(og.attributes.get("content"))
        if txt and 6 <= len(txt) <= 250 and txt.lower() not in _CHROME_H1_BLACKLIST:
            return txt

    # 5. <title> — strip site-name suffix after | or -  if present
    title_el = doc.css_first("title")
    if title_el:
        raw = _clean(title_el.text(strip=True))
        if raw:
            for sep in (" | ", " — ", " - "):
                if sep in raw:
                    raw = raw.split(sep)[0].strip()
                    break
            if 6 <= len(raw) <= 250 and raw.lower() not in _CHROME_H1_BLACKLIST:
                return raw

    return None


def _discover_event_from_article(
    html: str,
    evidence_url: str,
    published_at: Optional[date] = None,
) -> Optional[dict]:
    """If the article title looks like a defense engagement event AND
    the event clears the defense-relevance gate, create (and upsert)
    an Event for it. Returns the event dict, or None if the article
    doesn't appear to be a U.S. defense-industry-facing engagement.

    Auto-discovered events get host="discovered:<domain>" so the
    report renderer can distinguish them from adapter-sourced ones.

    The defense-relevance gate (extract.defense_relevance) is the
    choke point. Without it, this function admits any article whose
    title contains "hackathon" / "sprint" / etc., which is how we
    end up with Hospitality 2030 Hackathon and Built-with-Opus-4.6
    Claude Code hackathon in a defense-events report.
    """
    title = _extract_article_title(html)
    if not title or not _ENGAGEMENT_KEYWORDS_RE.search(title):
        return None
    from schema.event import Event, make_event_id
    from sources.recap_scraper import _html_to_text
    from extract.defense_relevance import score_event

    host = urlparse(evidence_url).netloc or "discovered"
    ev_date = published_at or date.today()

    # Build a provisional event dict — defense-relevance scorer
    # looks at name, host, source_url, plus body text.
    provisional = {
        "name": title,
        "host": f"discovered:{host}",
        "source_url": evidence_url,
    }
    result = score_event(provisional, body_text=_html_to_text(html))
    if not result.passes:
        log.info(
            "[discover] defense-gate DROP score=%.2f reasons=%s url=%s",
            result.score, ",".join(result.reasons), evidence_url,
        )
        return None
    log.debug(
        "[discover] defense-gate PASS score=%.2f reasons=%s url=%s",
        result.score, ",".join(result.reasons), evidence_url,
    )

    ev = Event(
        id=make_event_id(host, ev_date, title),
        name=title,
        aliases=[],
        host=f"discovered:{host}",
        dates_start=ev_date,
        dates_end=None,
        location="",
        source_url=evidence_url,
    )
    store.upsert_event(ev.to_dict())
    return ev.to_dict()


@dataclass
class EditorialSource:
    name: str
    base_url: str
    site_filter: Optional[str]      # the site:<domain> to use
    query_template: str
    confidence_floor: str           # passed through to scraper
    weight: float = 1.0


def _load_config(path: Path = DEFAULT_CONFIG_PATH) -> tuple[list[EditorialSource], int, int]:
    """Parse recap_sources.yaml. Returns (sources, window_before, window_after).

    Hand-rolled YAML parser to avoid the pyyaml dep — the file is
    a fixed, small structure. If the format grows, swap in pyyaml.
    """
    if not path.exists():
        log.warning("recap_sources config not found at %s", path)
        return [], 3, 21

    text = path.read_text(encoding="utf-8")
    sources: list[EditorialSource] = []
    window_before = 3
    window_after = 21

    current: dict | None = None
    in_sources = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("editorial_sources:"):
            in_sources = True
            continue
        if line.startswith("window_before_days:"):
            in_sources = False
            window_before = int(line.split(":", 1)[1].strip())
            continue
        if line.startswith("window_after_days:"):
            in_sources = False
            window_after = int(line.split(":", 1)[1].strip())
            continue
        if not in_sources:
            continue

        stripped = line.lstrip()
        if stripped.startswith("- name:"):
            # Start of a new source
            if current:
                sources.append(_build_source(current))
            current = {"name": stripped.split(":", 1)[1].strip()}
        elif current is not None and ":" in stripped:
            key, _, val = stripped.partition(":")
            current[key.strip()] = val.strip().strip('"').strip("'")
    if current:
        sources.append(_build_source(current))
    return sources, window_before, window_after


def _build_source(d: dict) -> EditorialSource:
    # site_filter pulled out of query_template if it's "site:<x>"
    template = d.get("query_template", "")
    site = None
    if "site:" in template:
        # Last simple site: token wins
        for tok in template.split():
            if tok.startswith("site:"):
                site = tok[len("site:"):].rstrip(")")
                break
    return EditorialSource(
        name=d.get("name", "unnamed"),
        base_url=d.get("base_url", ""),
        site_filter=site,
        query_template=template,
        confidence_floor=d.get("confidence_floor", "ecosystem_associated"),
        weight=float(d.get("weight", 1.0)),
    )


# ---- HTTP fetcher (with cache + offline fixture override) ----

# When OFFLINE_FETCH_MAP=<path-to-json> is set, the fetcher first
# checks the JSON map of {url: local_fixture_path} and serves from
# disk for matching URLs. Used by the demo so the full discovery
# → fetch → scrape flow works without network access.

_HTTP = httpx.Client(
    headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    },
    timeout=30.0,
    follow_redirects=True,
)


def _offline_fetch_map() -> dict[str, str]:
    import json, os
    path = os.getenv("OFFLINE_FETCH_MAP")
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _fetch_with_cache(url: str) -> Optional[str]:
    # Offline override
    fmap = _offline_fetch_map()
    if url in fmap:
        try:
            with open(fmap[url], "r", encoding="utf-8") as f:
                return f.read()
        except OSError as exc:
            log.warning("offline fetch map miss for %s: %s", url, exc)

    cached = store.cache_get(url)
    if cached is not None:
        return cached.decode("utf-8", errors="replace")
    try:
        r = _HTTP.get(url)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("fetch failed %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


# ---- Public API ----

def discover_for_event(
    event_id: str,
    *,
    backend: SearchBackend | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    today: date | None = None,
    queries_by_source: dict[str, int] | None = None,
    max_queries_per_source: int | None = None,
) -> dict:
    """Run discovery for a single event.

    `queries_by_source` (optional) is a shared counter — typically
    provided by `discover_all` so the per-source cap is enforced
    across all events processed in a single run. Pass None when
    calling stand-alone (no cap).

    `max_queries_per_source` (optional) caps how many Brave queries
    we'll issue against any single editorial source across the whole
    run. Defaults to the MAX_QUERIES_PER_SOURCE env var if set,
    otherwise unlimited.

    Returns a summary dict:
      {
        "event_id": ...,
        "queries_run": N,
        "queries_skipped_capped": N,
        "hits": N,
        "in_window": N,
        "articles_processed": N,
        "participations_written": N,
        "discovered_events": N,
        "errors": [...]
      }
    """
    import os as _os
    today = today or date.today()
    backend = backend or get_backend()
    if max_queries_per_source is None:
        env_val = _os.getenv("MAX_QUERIES_PER_SOURCE", "").strip()
        max_queries_per_source = int(env_val) if env_val.isdigit() else None
    if queries_by_source is None:
        queries_by_source = {}

    event = store.load_event(event_id)
    if not event:
        raise ValueError(f"event {event_id} not in store")

    sources, win_before, win_after = _load_config(config_path)
    log.info("[discover] event=%s sources=%d cap=%s",
             event["name"], len(sources), max_queries_per_source or "off")

    # Build the search-term set: canonical name + aliases
    terms = [event["name"]] + list(event.get("aliases") or [])

    # Date window for filtering hits
    end_date = date.fromisoformat(event["dates_end"]) if event.get("dates_end") else date.fromisoformat(event["dates_start"])
    win_start = end_date - timedelta(days=win_before)
    win_end = end_date + timedelta(days=win_after)

    summary = {
        "event_id": event_id,
        "queries_run": 0,
        "queries_skipped_capped": 0,
        "hits": 0,
        "in_window": 0,
        "articles_processed": 0,
        "participations_written": 0,
        "discovered_events": 0,
        "errors": [],
    }
    seen_urls: set[str] = set()

    for source in sources:
        for term in terms:
            # Per-source cap check (shared across the entire run).
            if (
                max_queries_per_source is not None
                and queries_by_source.get(source.name, 0) >= max_queries_per_source
            ):
                summary["queries_skipped_capped"] += 1
                continue
            try:
                results = backend.search(
                    term,
                    site=source.site_filter,
                    limit=10,
                )
            except Exception as exc:
                log.error("search failed for %r on %s: %s", term, source.name, exc)
                summary["errors"].append(f"{source.name}/{term}: {exc}")
                continue

            queries_by_source[source.name] = queries_by_source.get(source.name, 0) + 1
            summary["queries_run"] += 1
            summary["hits"] += len(results)

            for result in results:
                if result.url in seen_urls:
                    continue
                # Skip URL shapes that aggregate many topics (author
                # pages, section indexes, digests, RSS) — they cause
                # company-mention misattribution to the seed event.
                if _is_non_article_url(result.url):
                    log.info("[discover] skip non-article URL: %s", result.url)
                    continue
                # Date filter — keep if we don't know the date, otherwise
                # enforce the window
                if result.published_at and not (win_start <= result.published_at <= win_end):
                    continue
                seen_urls.add(result.url)
                summary["in_window"] += 1

                html = _fetch_with_cache(result.url)
                if not html:
                    continue

                # Decide which event this article belongs to.
                #   1. If the body mentions a distinctive token from
                #      the seed event's terms → attribute to seed.
                #   2. Else if the article's title looks like a real
                #      defense engagement event → auto-create an
                #      Event from the title and attribute to that.
                #   3. Else drop the article (no participation).
                from sources.recap_scraper import (
                    _article_mentions_event,
                    _html_to_text,
                )
                text = _html_to_text(html)
                target_event_id: Optional[str]
                extractor_suffix = ""
                if _article_mentions_event(text, terms):
                    target_event_id = event_id
                else:
                    discovered = _discover_event_from_article(
                        html, result.url, published_at=result.published_at
                    )
                    if discovered is None:
                        log.info(
                            "[discover] skip: article doesn't mention seed "
                            "event and title isn't an engagement event: %s",
                            result.url,
                        )
                        continue
                    target_event_id = discovered["id"]
                    extractor_suffix = ":discovered"
                    summary["discovered_events"] += 1

                participations = process_html(
                    html=html,
                    evidence_url=result.url,
                    event_id=target_event_id,
                    extracted_by=f"recap_scraper:{source.name}{extractor_suffix}",
                    has_named_author=source.weight >= 1.0,
                )
                summary["articles_processed"] += 1
                summary["participations_written"] += len(participations)

    log.info("[discover] %s", summary)
    return summary


def discover_all(
    *,
    backend: SearchBackend | None = None,
    only_event_id: Optional[str] = None,
    since: Optional[date] = None,
    max_queries_per_source: int | None = None,
) -> list[dict]:
    """Run discovery across all events in the store.

    Filter to events whose dates_end is within `since` (defaults to
    last 30 days). Older events typically don't accumulate new recap
    coverage; tighten this to manage Brave quota.

    `max_queries_per_source` caps how many Brave queries we'll issue
    against any single editorial source across the whole run.
    Defaults to MAX_QUERIES_PER_SOURCE env var if set, otherwise
    unlimited. The cap is shared across all events processed.
    """
    import os as _os
    since = since or (date.today() - timedelta(days=30))
    backend = backend or get_backend()
    if max_queries_per_source is None:
        env_val = _os.getenv("MAX_QUERIES_PER_SOURCE", "").strip()
        max_queries_per_source = int(env_val) if env_val.isdigit() else None

    with store.connect() as conn:
        if only_event_id:
            rows = conn.execute(
                "SELECT id FROM events WHERE id = ?", (only_event_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM events WHERE "
                "COALESCE(dates_end, dates_start) >= ?",
                (since.isoformat(),),
            ).fetchall()

    log.info(
        "[discover_all] %d events in window since %s, cap=%s queries/source",
        len(rows), since, max_queries_per_source or "off",
    )

    # Shared counter so the cap is enforced across all events.
    queries_by_source: dict[str, int] = {}
    summaries: list[dict] = []
    for row in rows:
        try:
            summaries.append(discover_for_event(
                row["id"],
                backend=backend,
                queries_by_source=queries_by_source,
                max_queries_per_source=max_queries_per_source,
            ))
        except Exception as exc:
            log.exception("discover_for_event(%s) failed", row["id"])
            summaries.append({"event_id": row["id"], "error": str(exc)})

    log.info(
        "[discover_all] final per-source counts: %s",
        dict(sorted(queries_by_source.items(), key=lambda kv: -kv[1])),
    )
    return summaries

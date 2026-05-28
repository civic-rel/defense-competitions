"""xTech (Army FUZE) adapter.

xtech.army.mil publishes complete competition + participant data
on a WordPress backend. Two sitemaps drive this adapter:

  - wp-sitemap-posts-competition-1.xml: all xTech competitions
    (xTechSearch 1-8, xTech Hackathon, xTech Plugfest, xTechSBIR,
    xTechPrime, xTechHumanoid, etc.)

  - wp-sitemap-posts-participant-1.xml: every participant ever,
    encoded as /participant/<competition-slug>-<company-slug>/.
    ~770 entries at time of writing.

Because the participant URLs encode both the competition slug and
the company name, we can derive every (event, company) pair
directly from the sitemap — no per-participant page fetches and no
Brave discovery required for xTech. Every participation written by
this adapter has confidence=confirmed (program-official source).

Public API:
  fetch_events()             -> list[Event]    (called by cron/daily.sh)
  fetch_participations(...)  -> int            (called by a one-shot
                                                or weekly sync to
                                                populate participants)
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Optional

import httpx
from selectolax.parser import HTMLParser

from extract.company_match import match_or_queue
from extract.confidence import assign_confidence
from normalize_helpers import parse_date_loose
from schema.event import Event, make_event_id
from schema.participation import Participation, make_participation_id
from store import cache as store

log = logging.getLogger(__name__)

COMP_SITEMAP_URL = "https://xtech.army.mil/wp-sitemap-posts-competition-1.xml"
PART_SITEMAP_URL = "https://xtech.army.mil/wp-sitemap-posts-participant-1.xml"

# Regex on the raw sitemap XML — simpler than xml.etree for this.
_COMP_URL_RE = re.compile(
    r"<loc>(https://xtech\.army\.mil/competition/[^<]+)</loc>"
)
_PART_URL_RE = re.compile(
    r"<loc>(https://xtech\.army\.mil/participant/[^<]+)</loc>"
)
_DATE_RE = re.compile(
    r"((?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+"
    r"\d{1,2}(?:[-,]\s*\d{1,2})?,?\s+\d{4})",
    re.IGNORECASE,
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
        log.error("[xtech] fetch %s: %s", url, exc)
        return None
    store.cache_set(url, r.content)
    return r.text


# ---- Events ----

def fetch_events() -> list[Event]:
    """Return one Event per xTech competition from the sitemap.

    Each Event is also upserted into the store as a side effect so
    that fetch_participations() can reference it later in the same
    run without re-fetching.
    """
    sitemap_xml = _fetch(COMP_SITEMAP_URL)
    if not sitemap_xml:
        return []

    comp_urls = sorted(set(_COMP_URL_RE.findall(sitemap_xml)))
    log.info("[xtech] %d competition URLs from sitemap", len(comp_urls))

    events: list[Event] = []
    for url in comp_urls:
        e = _parse_competition_page(url)
        if e:
            events.append(e)
    log.info("[xtech] parsed %d events", len(events))
    return events


def _parse_competition_page(url: str) -> Event | None:
    html = _fetch(url)
    if not html:
        return None
    doc = HTMLParser(html)

    h1 = doc.css_first("h1")
    if not h1:
        return None
    # WordPress sometimes interleaves zero-width spaces in titles.
    name = re.sub(r"[​‌‍﻿]", "", h1.text(strip=True))
    name = re.sub(r"\s+", " ", name).strip()
    if not name or len(name) < 3:
        return None

    body = doc.css_first("body")
    text = re.sub(r"\s+", " ", body.text(separator=" ", strip=True)) if body else ""
    date_match = _DATE_RE.search(text)
    dates_start = (
        parse_date_loose(date_match.group(1).split("-")[0])
        if date_match
        else None
    )

    # WordPress meta tags often carry the published date even when
    # the visible body doesn't include a full "Month DD, YYYY" string.
    # Try these before falling back to slug-year or today.
    if not dates_start:
        for sel, attr in (
            ('meta[property="article:published_time"]', "content"),
            ('meta[name="article:published_time"]', "content"),
            ('meta[property="og:updated_time"]', "content"),
            ('time[datetime]', "datetime"),
        ):
            el = doc.css_first(sel)
            if el:
                dates_start = parse_date_loose(el.attributes.get(attr))
                if dates_start:
                    log.debug("[xtech] date from %s: %s", sel, dates_start)
                    break

    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not dates_start:
        # Slug-year fallback (xtechinternational2024).
        year_match = re.search(r"(20\d{2})", slug)
        if year_match:
            dates_start = date(int(year_match.group(1)), 1, 1)

    if not dates_start:
        # No date found anywhere. Skip the event rather than guess
        # — date.today() was causing 2025 events to show as 2026
        # and that corrupts the Section 1 ordering.
        log.warning(
            "[xtech] no parseable date for %s; skipping. "
            "Add a body-text date or a 4-digit year in the URL slug.",
            url,
        )
        return None
    return Event(
        id=make_event_id("U.S. Army FUZE xTech Program", dates_start, name),
        name=name,
        aliases=[slug],
        host="U.S. Army FUZE xTech Program",
        dates_start=dates_start,
        dates_end=None,
        location="",
        source_url=url,
    )


# ---- Participations (from the participant sitemap) ----

# Programmatic slug → display-name overrides for tokens that don't
# title-case cleanly (acronyms, etc.). Keys are lower-case slug tokens.
_SLUG_DISPLAY_OVERRIDES = {
    "inc": "Inc",
    "llc": "LLC",
    "ltd": "Ltd",
    "corp": "Corp",
    "ai": "AI",
    "io": "io",
    "isr": "ISR",
    "uav": "UAV",
    "uas": "UAS",
}


def _slug_to_company_name(company_slug: str) -> str:
    """Convert URL slug like 'adranos-inc' -> 'Adranos Inc'."""
    if not company_slug:
        return ""
    out = []
    for token in company_slug.split("-"):
        if not token:
            continue
        lower = token.lower()
        if lower in _SLUG_DISPLAY_OVERRIDES:
            out.append(_SLUG_DISPLAY_OVERRIDES[lower])
        elif token.isdigit():
            out.append(token)
        else:
            out.append(token.capitalize())
    return " ".join(out)


def _norm_slug(s: str) -> str:
    """Strip hyphens/underscores and lowercase so that the participant-
    URL convention (`xtech-edge-strike-foo`) matches the canonical
    competition slug convention (`xtechedgestrikeground`)."""
    return re.sub(r"[-_]", "", s.lower())


def _find_competition_for_participant(
    rest: str,
    sorted_known_slugs: list[str],
    slug_to_norm: dict[str, str],
) -> tuple[Optional[str], Optional[int]]:
    """Match a participant URL's `rest` (the final path segment) to one
    of the known competition slugs. Returns (matched_slug, offset_into_rest)
    where offset_into_rest points to the first char of the company slug.

    Strategy: normalize both sides (drop hyphens), then look for any
    known competition slug whose normalized form appears as a substring
    in the normalized rest. Prefer longest match. If a normalized hit
    is found, walk back to the original rest to compute the offset
    where the company portion begins.
    """
    norm_rest = _norm_slug(rest)
    best_ns: Optional[str] = None
    best_orig: Optional[str] = None
    for s in sorted_known_slugs:
        ns = slug_to_norm[s]
        if ns and ns in norm_rest:
            if best_ns is None or len(ns) > len(best_ns):
                best_ns = ns
                best_orig = s
    if not best_orig:
        return None, None
    # Walk the original `rest` until we've consumed `best_ns`-worth of
    # alphanumeric characters; the next index is where the company slug
    # begins (skip any leading hyphens).
    target_len = len(best_ns)
    consumed = 0
    cursor = 0
    started = False
    while cursor < len(rest) and consumed < target_len:
        ch = rest[cursor]
        if ch.isalnum():
            consumed += 1
            started = True
        elif not started:
            # Allow leading non-alnum (rare)
            pass
        cursor += 1
    # Skip the hyphen separator between competition and company
    while cursor < len(rest) and not rest[cursor].isalnum():
        cursor += 1
    return best_orig, cursor


def fetch_participations(
    *, events: list[Event] | None = None, max_participants: int | None = None
) -> int:
    """Read the participant sitemap and write Participation rows for
    every (competition, company) pair. Returns the number of rows
    successfully written.

    `events` lets callers pass already-loaded Events (avoids re-running
    fetch_events()). If None, we load them now.
    `max_participants` caps the number of participations processed —
    useful for testing.
    """
    events = events if events is not None else fetch_events()
    if not events:
        log.warning("[xtech] no events available — skipping participations")
        return 0

    # Build a slug -> event lookup. Use the slug stored as aliases[0].
    slug_to_event: dict[str, Event] = {}
    for e in events:
        for alias in e.aliases or []:
            slug_to_event[alias] = e
    # Pre-compute normalized forms of every known slug so the substring
    # match works across hyphenation conventions.
    slug_to_norm = {s: _norm_slug(s) for s in slug_to_event}
    sorted_slugs = sorted(slug_to_event.keys(), key=lambda s: len(slug_to_norm[s]), reverse=True)

    sitemap_xml = _fetch(PART_SITEMAP_URL)
    if not sitemap_xml:
        return 0
    part_urls = sorted(set(_PART_URL_RE.findall(sitemap_xml)))
    log.info("[xtech] %d participant URLs from sitemap", len(part_urls))
    if max_participants:
        part_urls = part_urls[:max_participants]

    written = 0
    skipped_unmatched = 0
    for url in part_urls:
        rest = url.rstrip("/").rsplit("/", 1)[-1]
        comp_slug, company_offset = _find_competition_for_participant(
            rest, sorted_slugs, slug_to_norm
        )
        if not comp_slug:
            skipped_unmatched += 1
            continue
        company_slug = rest[company_offset:] if company_offset else ""
        company_name = _slug_to_company_name(company_slug)
        if not company_name or len(company_name) < 2:
            continue
        event = slug_to_event[comp_slug]

        match = match_or_queue(
            company_name,
            event_id=event.id,
            evidence_url=url,
            evidence_excerpt=f"xTech program-official participant listing for "
                             f"{event.name}",
        )
        if match.company is None:
            continue

        confidence = assign_confidence(
            url,
            "xTech program-official participant listing",
            role_hint="participant",
        )
        p = Participation(
            id=make_participation_id(
                match.company["id"], event.id, "participant", url
            ),
            company_id=match.company["id"],
            event_id=event.id,
            role="participant",
            confidence=confidence,
            evidence_url=url,
            evidence_excerpt=(
                f"Listed as participant in {event.name} on the xTech program "
                f"site (xtech.army.mil)."
            ),
            extracted_by="xtech_adapter",
            extracted_at=datetime.utcnow(),
            notes="xtech_participant_sitemap",
        )
        store.upsert_participation(p.to_dict())
        written += 1

    log.info(
        "[xtech] participations: %d written, %d unmatched-to-competition",
        written, skipped_unmatched,
    )
    return written


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    print("=== Competitions ===")
    events = fetch_events()
    for e in events[:8]:
        print(f"  {e.dates_start}  {e.name[:60]}")
    print(f"  ... ({len(events)} total)")
    print()
    print("=== Participations (writing to store) ===")
    for e in events:
        store.upsert_event(e.to_dict())
    n = fetch_participations(events=events)
    print(f"  wrote {n} participations")

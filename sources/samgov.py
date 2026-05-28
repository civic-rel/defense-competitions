"""SAM.gov special-notices adapter.

api.sam.gov's Opportunities v2 endpoint exposes federal procurement
notices — solicitations, combined synopses, pre-solicitations, and
special notices. "Special notices" (notice type code 's') are
typically industry days, sources sought, RFI announcements, and
other engagement-style postings where defense companies show up.

Each notice we return becomes an Event in the store. The discovery
loop can then look for recap coverage of named industry days; for
sources-sought postings the body itself sometimes lists vendors
that pre-registered.

Requires SAM_API_KEY. Free key from https://api.sam.gov; renews
every 90 days. If the key isn't set, this adapter logs a warning
and returns an empty list — the rest of the pipeline keeps running.

Filters applied:
  - Notice type = 's' (Special Notice) by default; callers may pass
    other codes (e.g., 'k' for combined synopsis, 'p' for
    pre-solicitation).
  - postedFrom/postedTo: default window = last 90 days.
  - DoD-only filter applied client-side via organizationHierarchy
    string match (DEPT OF DEFENSE / DEPT OF THE ARMY / NAVY / AIR
    FORCE / SPACE FORCE / DEFENSE LOGISTICS AGENCY / DARPA / DIU,
    etc.). SAM.gov doesn't expose a single "DoD" parameter.

API docs:
  https://open.gsa.gov/api/get-opportunities-public-api/
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

import httpx

from normalize_helpers import parse_date_loose
from schema.event import Event, make_event_id
from store import cache as store

log = logging.getLogger(__name__)

OPPORTUNITIES_URL = "https://api.sam.gov/prod/opportunities/v2/search"

# Notice type codes per SAM.gov docs.
NOTICE_SPECIAL_NOTICE = "s"
NOTICE_COMBINED_SYNOPSIS = "k"
NOTICE_PRE_SOLICITATION = "p"
NOTICE_SOURCES_SOUGHT = "r"

# Defense agencies we accept. Match is case-insensitive substring
# on the organizationHierarchy string SAM returns.
_DOD_AGENCY_PATTERNS = re.compile(
    r"\b("
    r"department\s+of\s+defense|"
    r"department\s+of\s+(?:the\s+)?(?:army|navy|air\s+force)|"
    r"space\s+force|"
    r"darpa|defense\s+innovation\s+unit|diu|"
    r"defense\s+logistics\s+agency|dla|"
    r"defense\s+threat\s+reduction\s+agency|dtra|"
    r"missile\s+defense\s+agency|mda|"
    r"national\s+geospatial[- ]intelligence\s+agency|nga|"
    r"national\s+security\s+agency|nsa|"
    r"u\.?\s*s\.?\s+special\s+operations\s+command|socom|"
    r"u\.?\s*s\.?\s+marine\s+corps|usmc"
    r")\b",
    re.I,
)


def _fetch_json(params: dict[str, Any]) -> Any:
    """GET api.sam.gov with caching. Returns parsed JSON or None on error."""
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api_key")
    cache_key = f"{OPPORTUNITIES_URL}?{qs}"
    cached = store.cache_get(cache_key)
    if cached:
        import json as _json
        return _json.loads(cached.decode("utf-8"))
    try:
        r = httpx.get(OPPORTUNITIES_URL, params=params, timeout=60.0,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("[samgov] %s: %s", OPPORTUNITIES_URL, exc)
        return None
    store.cache_set(cache_key, r.content)
    return r.json()


def fetch_events(
    *,
    notice_types: tuple[str, ...] = (
        NOTICE_SPECIAL_NOTICE,
        NOTICE_SOURCES_SOUGHT,
        NOTICE_COMBINED_SYNOPSIS,
    ),
    posted_from: date | None = None,
    posted_to: date | None = None,
    limit: int = 100,
) -> list[Event]:
    """Return DoD special-notice / sources-sought / combined-synopsis
    opportunities as Events.

    No-ops with a warning if SAM_API_KEY isn't set.
    """
    api_key = os.getenv("SAM_API_KEY", "").strip()
    if not api_key:
        log.warning(
            "[samgov] SAM_API_KEY not set; skipping. "
            "Get a free key at https://api.sam.gov "
            "(renews every 90 days)."
        )
        return []

    posted_to = posted_to or date.today()
    posted_from = posted_from or (posted_to - timedelta(days=90))

    events: list[Event] = []
    for code in notice_types:
        data = _fetch_json({
            "api_key": api_key,
            "limit": limit,
            "noticeType": code,
            "postedFrom": posted_from.strftime("%m/%d/%Y"),
            "postedTo": posted_to.strftime("%m/%d/%Y"),
        })
        if not data:
            continue
        items = data.get("opportunitiesData") or data.get("data") or []
        log.info(
            "[samgov] notice_type=%s returned %d opportunities",
            code, len(items),
        )
        for item in items:
            ev = _to_event(item, notice_type=code)
            if ev:
                events.append(ev)
    log.info("[samgov] %d DoD opportunities parsed", len(events))
    return events


_ENGAGEMENT_TITLE_RE = re.compile(
    r"\b("
    r"industry\s+day|"
    r"proposers?\s+day|"
    r"proposer\s+day|"
    r"information\s+session|"
    r"innovation\s+forum|"
    r"innovation\s+challenge|"
    r"prize\s+challenge|"
    r"hackathon|"
    r"sprint|"
    r"capability\s+engagement|"
    r"assessment\s+event|"
    r"qualifying\s+event|"
    r"technical\s+exchange|"
    r"\btem\b|"
    r"pitch\s+day"
    r")\b",
    re.I,
)


def _to_event(item: dict, *, notice_type: str) -> Event | None:
    """Map a SAM.gov opportunity to an Event. Filters non-DoD AND
    filters non-engagement notices.

    SAM.gov returns many notice types (special notices, sources
    sought, combined synopses, pre-solicitations). The vast majority
    are procurement solicitations — bid opportunities, not
    engagement events where companies show up to compete. Only
    notices whose title indicates a real engagement (industry day,
    proposers day, hackathon, sprint, capability engagement, etc.)
    pass into the defense-events store.
    """
    title = (item.get("title") or "").strip()
    if not title:
        return None
    # Engagement-shape filter — drop pure procurement solicitations.
    if not _ENGAGEMENT_TITLE_RE.search(title):
        return None

    # Hierarchy: SAM returns "DEPT OF DEFENSE.DEPT OF THE ARMY.US ARMY..." etc.
    hierarchy = (
        item.get("organizationHierarchy")
        or item.get("fullParentPathName")
        or ""
    )
    if not isinstance(hierarchy, str):
        hierarchy = str(hierarchy)
    if not _DOD_AGENCY_PATTERNS.search(hierarchy):
        return None

    posted = parse_date_loose(item.get("postedDate"))
    if not posted:
        return None
    deadline = parse_date_loose(item.get("responseDeadLine"))

    # Pick the top-level org as the host name (e.g., "DEPT OF DEFENSE")
    host = hierarchy.split(".")[0].strip()[:80] if hierarchy else f"SAM.gov ({notice_type})"

    source_url = (
        item.get("uiLink")
        or item.get("additionalInfoLink")
        or "https://sam.gov/opp/"
    )

    return Event(
        id=make_event_id(host, posted, title),
        name=title,
        aliases=[item.get("noticeId", "")] if item.get("noticeId") else [],
        host=host,
        dates_start=posted,
        dates_end=deadline,
        location=item.get("placeOfPerformance", {}).get("city", {}).get("name", "")
                 if isinstance(item.get("placeOfPerformance"), dict) else "",
        source_url=source_url,
    )


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    events = fetch_events()
    print(f"Got {len(events)} DoD special notices")
    for e in events[:10]:
        print(f"  {e.dates_start}  {e.host[:30]:30}  {e.name[:60]}")

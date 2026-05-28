"""Drone Dominance Initiative adapter.

dronedominance.mil publishes the vendor list for each program
phase on a single page (anchors like #phase1Panel, #phase2Panel,
…). Each panel is a list of selected companies — these are real
participants in a U.S. defense evaluation, so they belong in the
ATO-outreach report.

This adapter is built to the same shape as `sources/afwerx.py`
and `sources/sofwerx.py`: a `fetch_events()` function that
returns a list of Events and writes per-company Participations
into the store via `recap_scraper.process_html`.

Two implementations behind one interface:

  - DroneDominanceBackend.FIXTURE  (default; reads from
        tests/fixtures/dronedominance_vendors.html). No network.
  - DroneDominanceBackend.LIVE     (opt-in; set
        DRONEDOMINANCE_BACKEND=live in the environment). Fetches
        https://dronedominance.mil/vendors.html with the standard
        cache wrapper.

The .mil call is gated behind an env var because we don't want
production batch jobs to start hammering DoD sites by default —
that's the same defensive posture cron/daily.sh uses for other
adapters.

Public API:
    fetch_events() -> list[Event]
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path

import httpx

from schema.event import Event, make_event_id
from store import cache as store

log = logging.getLogger(__name__)

LIVE_URL = "https://dronedominance.mil/vendors.html"
FIXTURE_PATH = (
    Path(__file__).parent.parent / "tests" / "fixtures"
    / "dronedominance_vendors.html"
)

# Each panel is a <section> or <div> with id like phase1Panel,
# phase2Panel. Inside, each vendor is in a structured element we
# can hand to the NER step. The regex below is a tolerant capture
# — the live page format may shift, so we fall back to letting
# recap_scraper do the heavy lifting on the raw panel HTML.
_PANEL_RE = re.compile(
    r'<(?:section|div)[^>]*\bid="(phase\d+Panel)"[^>]*>(.*?)</(?:section|div)>',
    re.IGNORECASE | re.DOTALL,
)
# Human-readable phase label. Each phase is its own Event in the
# store so participations cleanly attribute to the right round.
_PHASE_NAMES = {
    "phase1Panel": "Drone Dominance Initiative — Phase 1 Vendors",
    "phase2Panel": "Drone Dominance Initiative — Phase 2 Vendors",
    "phase3Panel": "Drone Dominance Initiative — Phase 3 Vendors",
}

HOST = "Drone Dominance Initiative (DoD)"


def _resolve_backend() -> str:
    """Return 'live' if explicitly opted in, otherwise 'fixture'."""
    val = os.getenv("DRONEDOMINANCE_BACKEND", "fixture").strip().lower()
    return "live" if val == "live" else "fixture"


def _fetch_live() -> str | None:
    """Pull vendors.html from dronedominance.mil with the cache."""
    cached = store.cache_get(LIVE_URL)
    if cached:
        return cached.decode("utf-8", errors="replace")
    try:
        r = httpx.get(LIVE_URL, headers={"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        )}, timeout=30.0, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("[dronedominance] live fetch failed: %s", exc)
        return None
    store.cache_set(LIVE_URL, r.content)
    return r.text


def _fetch_fixture() -> str | None:
    if not FIXTURE_PATH.exists():
        log.warning("[dronedominance] fixture not found at %s", FIXTURE_PATH)
        return None
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _fetch() -> str | None:
    backend = _resolve_backend()
    log.info("[dronedominance] backend=%s", backend)
    return _fetch_live() if backend == "live" else _fetch_fixture()


def fetch_events() -> list[Event]:
    """Parse dronedominance.mil vendors page → Events + Participations.

    One Event per phase panel. Returns the events written; the
    Participations are written into the store as a side effect via
    `recap_scraper.process_html`.
    """
    from sources.recap_scraper import process_html

    html = _fetch()
    if not html:
        return []

    panels = _PANEL_RE.findall(html)
    if not panels:
        log.warning("[dronedominance] no phase panels matched in HTML")
        return []
    log.info("[dronedominance] %d phase panels found", len(panels))

    today = date.today()
    events: list[Event] = []
    for panel_id, inner_html in panels:
        title = _PHASE_NAMES.get(
            panel_id, f"Drone Dominance Initiative — {panel_id}"
        )
        ev = Event(
            id=make_event_id(HOST, today, title),
            name=title,
            aliases=[panel_id],
            host=HOST,
            dates_start=today,
            dates_end=None,
            location="",
            source_url=f"{LIVE_URL}#{panel_id}",
        )
        store.upsert_event(ev.to_dict())

        # Recap scraper extracts company names from the panel HTML
        # and writes Participation rows. We mark these as
        # "confirmed" by passing has_named_author=True since the
        # source is the program's own page.
        process_html(
            html=inner_html,
            evidence_url=f"{LIVE_URL}#{panel_id}",
            event_id=ev.id,
            extracted_by="dronedominance_adapter",
            has_named_author=True,
        )
        events.append(ev)

    log.info("[dronedominance] %d events written", len(events))
    return events


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    print(f"Backend: {_resolve_backend()}")
    for e in fetch_events():
        print(f"  {e.dates_start}  {e.name}")

"""SBIR.gov adapter.

Two roles in the v2 pipeline:

  1. Solicitations endpoint → DoD SBIR/STTR solicitation calendar.
     Each solicitation is a forward-looking participation
     opportunity (not technically an "event" but in scope for the
     brief's "SBIR/STTR showcases").

  2. Awards endpoint → confirmed participation data. For every
     event in the store where companies won prizes (xTech, AFWERX
     SBIR-tier, etc.), we can cross-reference SBIR awards by firm
     name to confirm prize-to-contract conversion. This is more
     reliable than the USASpending lookup for SBIR-specific paths.

API base:
  https://api.www.sbir.gov/public/api/solicitations
  https://api.www.sbir.gov/public/api/awards

Both endpoints return JSON. No auth required. Free.

Note: at time of writing the API has occasional maintenance windows.
We tolerate failures and continue with whatever data we have.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from normalize_helpers import parse_date_loose
from schema.event import Event, make_event_id
from store import cache as store

log = logging.getLogger(__name__)

SOLICITATIONS_URL = "https://api.www.sbir.gov/public/api/solicitations"
AWARDS_URL = "https://api.www.sbir.gov/public/api/awards"


def _fetch_json(url: str, params: dict[str, Any]) -> Any:
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cache_key = f"{url}?{qs}"
    cached = store.cache_get(cache_key)
    if cached:
        import json as _json
        return _json.loads(cached.decode("utf-8"))
    try:
        r = httpx.get(url, params=params, timeout=30.0,
                      headers={"Accept": "application/json"})
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("[sbir] %s: %s", url, exc)
        return None
    store.cache_set(cache_key, r.content)
    return r.json()


# ---- Solicitations ----

def fetch_dod_solicitations(*, only_open: bool = True) -> list[Event]:
    """Return DoD solicitations as Event records.

    Filters to agency=DOD. The result is the universe of forward-
    looking SBIR/STTR participation opportunities — useful for the
    monthly tracking section's "hackathon → SBIR transitions" line.
    """
    params: dict[str, Any] = {"agency": "DOD", "rows": 100, "format": "json"}
    if only_open:
        params["open"] = "Y"

    data = _fetch_json(SOLICITATIONS_URL, params) or []
    events: list[Event] = []
    for item in data:
        title = item.get("solicitation_title") or item.get("title")
        if not title:
            continue
        open_d = parse_date_loose(item.get("open_date"))
        close_d = parse_date_loose(item.get("close_date"))
        if not open_d:
            continue
        host = f"SBIR DoD/{item.get('branch','')}".strip("/")
        events.append(Event(
            id=make_event_id(host, open_d, title),
            name=title,
            aliases=[item.get("solicitation_number", "")],
            host=host,
            dates_start=open_d,
            dates_end=close_d,
            location="",
            source_url=item.get("sbir_solicitation_link") or item.get("solicitation_agency_url", ""),
        ))
    log.info("[sbir] %d DoD solicitations", len(events))
    return events


# ---- Awards (for enrichment, not Event creation) ----

def lookup_awards_for_firm(
    firm_name: str,
    *,
    agency: str = "DOD",
    rows: int = 20,
) -> list[dict]:
    """Return recent SBIR/STTR awards for a given firm.

    Used by enrichment to confirm participation-to-award conversion.
    """
    data = _fetch_json(AWARDS_URL, {
        "firm": firm_name, "agency": agency, "rows": rows, "format": "json",
    }) or []
    return data if isinstance(data, list) else []


def fetch_recent_dod_awards(since_year: int) -> list[dict]:
    """Return all DoD SBIR/STTR awards from a given fiscal year forward.

    Used to populate the cross-event "this company won an SBIR after
    the xTech hackathon" detection in the monthly diff.
    """
    data = _fetch_json(AWARDS_URL, {
        "agency": "DOD", "year": since_year, "rows": 100, "format": "json",
    }) or []
    log.info("[sbir] %d DoD awards since %s", len(data), since_year)
    return data if isinstance(data, list) else []


if __name__ == "__main__":
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    print("=== DoD solicitations ===")
    for e in fetch_dod_solicitations(only_open=True)[:5]:
        print(f"  {e.dates_start} → {e.dates_end}  {e.host:18}  {e.name[:60]}")
    print("\n=== Sample award lookup: 'Hadrian' ===")
    for a in lookup_awards_for_firm("Hadrian", rows=3):
        print(f"  {a.get('award_year','?')}  ${a.get('award_amount','?')}  "
              f"{a.get('firm','?')[:40]} — {a.get('award_title','?')[:50]}")

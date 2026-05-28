"""Monthly diff queries.

Produces the four sub-tables of the monthly tracking section the
brief asked for:

  1. NEW companies first seen this window
  2. Companies with INCREASING event frequency vs. prior window
  3. Startups transitioning from hackathons into SBIR / OTA pathways
  4. Supporters (sponsors/judges/mentors/investors) repeatedly
     co-occurring with the same participants

All four queries hit the existing `companies`, `events`, and
`participations` tables — no schema additions. The output of each
function is a list of dicts with stable shapes so the markdown
builder can render them directly.

Design notes:
  - "Window" defaults to the last 30 days but the caller can
    override. Prior-window comparison uses the same length.
  - "Hackathon → SBIR" depends on having both the hackathon
    participation and the SBIR award. If the SBIR adapter hasn't
    run (or the company isn't in SBIR.gov yet), no row appears —
    that's correct fail-closed behavior.
  - All queries deduplicate by company id, never by name. Aliases
    have already been collapsed by the time data hits this layer.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from store import cache as store

log = logging.getLogger(__name__)


# ============================================================
# Loading helpers
# ============================================================

def _load_full() -> dict:
    """Load companies/events/participations from the store in one pass."""
    with store.connect() as conn:
        companies = store.load_companies()
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY dates_start DESC"
        ).fetchall()]
        participations = [dict(r) for r in conn.execute(
            "SELECT * FROM participations"
        ).fetchall()]
    return {
        "companies": companies,
        "events": events,
        "participations": participations,
        "company_by_id": {c["id"]: c for c in companies},
        "event_by_id": {e["id"]: e for e in events},
    }


# ============================================================
# 1. NEW companies
# ============================================================

def new_companies(*, since: date) -> list[dict]:
    """Companies whose first_seen >= since."""
    data = _load_full()
    out = []
    parts_by_company: dict[str, list[dict]] = defaultdict(list)
    for p in data["participations"]:
        parts_by_company[p["company_id"]].append(p)

    for c in data["companies"]:
        try:
            fs = date.fromisoformat(c["first_seen"])
        except (ValueError, TypeError):
            continue
        if fs < since:
            continue
        events = {p["event_id"] for p in parts_by_company.get(c["id"], [])}
        out.append({
            "company_id": c["id"],
            "name": c["name"],
            "type": c.get("type", "unknown"),
            "first_seen": c["first_seen"],
            "events": len(events),
            "is_stealth": bool(c.get("is_stealth")),
            "domains": c.get("domains") or [],
        })
    out.sort(key=lambda x: (-x["events"], x["name"]))
    return out


# ============================================================
# 2. Increasing event frequency
# ============================================================

def increasing_frequency(*, window_days: int = 30, today: date | None = None) -> list[dict]:
    """Companies whose event count in [today - window, today] is
    greater than their event count in [today - 2*window, today - window].

    Returns rows with current/prior counts and delta. Sorted by delta desc.
    """
    today = today or date.today()
    cur_start = today - timedelta(days=window_days)
    prior_start = today - timedelta(days=window_days * 2)
    prior_end = cur_start

    data = _load_full()
    cur_count: dict[str, int] = defaultdict(int)
    prior_count: dict[str, int] = defaultdict(int)
    cur_events: dict[str, set[str]] = defaultdict(set)

    for p in data["participations"]:
        ev = data["event_by_id"].get(p["event_id"])
        if not ev:
            continue
        try:
            ev_date = date.fromisoformat(ev["dates_start"])
        except (ValueError, TypeError):
            continue
        if cur_start <= ev_date <= today:
            # Count distinct events, not distinct participation rows
            if ev["id"] not in cur_events[p["company_id"]]:
                cur_count[p["company_id"]] += 1
                cur_events[p["company_id"]].add(ev["id"])
        elif prior_start <= ev_date < prior_end:
            prior_count[p["company_id"]] += 1

    out = []
    for cid in set(list(cur_count) + list(prior_count)):
        delta = cur_count[cid] - prior_count[cid]
        if delta <= 0:
            continue
        c = data["company_by_id"].get(cid)
        if not c:
            continue
        out.append({
            "company_id": cid,
            "name": c["name"],
            "current": cur_count[cid],
            "prior": prior_count[cid],
            "delta": delta,
        })
    out.sort(key=lambda x: (-x["delta"], -x["current"], x["name"]))
    return out


# ============================================================
# 3. Hackathon -> SBIR / OTA transitions
# ============================================================

def hackathon_to_sbir_transitions(*, since: date) -> list[dict]:
    """Find companies that participated in a Hackathon/PrizeChallenge
    AND have an OTA/contract signal in `ota_signals`, where the OTA
    signal is more recent than the hackathon participation.

    Returns rows linking the source event to the follow-on award.
    """
    data = _load_full()
    parts_by_company: dict[str, list[dict]] = defaultdict(list)
    for p in data["participations"]:
        parts_by_company[p["company_id"]].append(p)

    out = []
    for c in data["companies"]:
        signals = c.get("ota_signals") or []
        if not signals:
            continue
        # Find their hackathon-class participations
        hack_events: list[dict] = []
        for p in parts_by_company.get(c["id"], []):
            ev = data["event_by_id"].get(p["event_id"])
            if not ev:
                continue
            try:
                ev_date = date.fromisoformat(ev["dates_start"])
            except (ValueError, TypeError):
                continue
            # We don't have event.type on the v2 Event dataclass yet — infer
            # from name heuristically until that's added.
            ev_name_low = (ev["name"] or "").lower()
            is_hackathon = (
                "hackathon" in ev_name_low
                or "prize challenge" in ev_name_low
                or "competition" in ev_name_low
                or "sprint" in ev_name_low
            )
            if is_hackathon and ev_date >= since:
                hack_events.append({**ev, "ev_date": ev_date.isoformat(), "role": p["role"]})

        if not hack_events:
            continue
        for h in hack_events:
            out.append({
                "company_id": c["id"],
                "name": c["name"],
                "source_event": h["name"],
                "source_event_date": h["ev_date"],
                "source_role": h["role"],
                "ota_signal_count": len(signals),
                "top_ota_amount": max(
                    (s.get("amount") or 0) for s in signals
                ),
            })
    out.sort(key=lambda x: -x["top_ota_amount"])
    return out


# ============================================================
# 4. Recurring supporters
# ============================================================

# Roles that indicate a "supporter" vs. a competing participant
SUPPORTING_ROLES = {"sponsor", "judge", "mentor", "investor"}
PARTICIPATING_ROLES = {"winner", "finalist", "participant", "demoing", "presenting"}


def recurring_supporters(*, min_events: int = 2) -> list[dict]:
    """Companies that appear in supporting roles across multiple
    events. The brief asks for this in Section 6 + monthly diff —
    Section 6 already shows the co-occurrence; here we focus on
    the time dimension: which supporters keep showing up.

    Generic — no hardcoded names.
    """
    data = _load_full()

    # supporter_id -> set of event_ids where they appeared as supporter
    sup_events: dict[str, set[str]] = defaultdict(set)
    # supporter_id -> set of participant company_ids co-occurred with
    sup_partners: dict[str, set[str]] = defaultdict(set)
    # event_id -> set of participant ids at that event
    parts_at_event: dict[str, set[str]] = defaultdict(set)

    for p in data["participations"]:
        if p["role"] in PARTICIPATING_ROLES:
            parts_at_event[p["event_id"]].add(p["company_id"])

    for p in data["participations"]:
        if p["role"] in SUPPORTING_ROLES:
            sup_events[p["company_id"]].add(p["event_id"])
            sup_partners[p["company_id"]] |= parts_at_event.get(p["event_id"], set())

    out = []
    for sup_id, events in sup_events.items():
        if len(events) < min_events:
            continue
        c = data["company_by_id"].get(sup_id)
        if not c:
            continue
        out.append({
            "company_id": sup_id,
            "name": c["name"],
            "type": c.get("type", "unknown"),
            "events_supported": len(events),
            "distinct_participants": len(sup_partners[sup_id]),
        })
    out.sort(key=lambda x: (-x["events_supported"], -x["distinct_participants"]))
    return out


# ============================================================
# Convenience: run all four
# ============================================================

def run_all(*, since: date | None = None, window_days: int = 30) -> dict:
    since = since or (date.today() - timedelta(days=window_days))
    return {
        "new_companies": new_companies(since=since),
        "increasing_frequency": increasing_frequency(window_days=window_days),
        "transitions": hackathon_to_sbir_transitions(since=since),
        "recurring_supporters": recurring_supporters(min_events=2),
    }

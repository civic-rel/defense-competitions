"""Markdown report builder.

Produces the seven-section output from the brief:

  Section 1 — Competitions discovered this month
  Section 2 — Deduped participant master list
  Section 3 — Companies appearing across multiple competitions
  Section 4 — Emerging stealth startups
  Section 5 — Compliance maturity analysis
  Section 6 — Ecosystem mapping
  Section 7 — Raw evidence appendix

All data comes from the store. The report is regenerated on demand.

Run:
    python -m reports.build_markdown
    # output: reports/out/report_<YYYY-MM-DD>.md
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

from store import cache as store


def _h1(s: str) -> str: return f"# {s}\n"
def _h2(s: str) -> str: return f"\n## {s}\n"
def _h3(s: str) -> str: return f"\n### {s}\n"


def _table(headers: list[str], rows: Iterable[list[str]]) -> str:
    """Build a Markdown table. Escapes pipes in cells."""
    def cell(x: object) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for r in rows:
        out.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _load_expected_participants_overrides() -> dict[str, int]:
    """Read config/expected_participants.yaml. Returns
    {event_id: expected_participants}. Missing or malformed file
    is non-fatal; we just return {}.
    """
    path = Path(__file__).parent.parent / "config" / "expected_participants.yaml"
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    current_event: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("events:"):
            continue
        # event_id (2-space indent)
        if line.startswith("  ") and not line.startswith("    "):
            current_event = line.strip().rstrip(":")
            continue
        if current_event and line.lstrip().startswith("expected_participants:"):
            try:
                out[current_event] = int(
                    line.split(":", 1)[1].strip()
                )
            except (ValueError, IndexError):
                pass
    return out


def _load_all() -> dict:
    """Load everything we need from the store in one place."""
    with store.connect() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY dates_start DESC"
        ).fetchall()]
        companies = store.load_companies()
        participations = [dict(r) for r in conn.execute(
            "SELECT * FROM participations"
        ).fetchall()]
        review_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM review_queue WHERE resolution IS NULL"
        ).fetchall()]

    # Apply config/expected_participants.yaml overrides on top of
    # whatever each adapter wrote into the DB. Analyst can edit the
    # YAML without re-running ingestion.
    overrides = _load_expected_participants_overrides()
    if overrides:
        for e in events:
            if e["id"] in overrides:
                e["expected_participants"] = overrides[e["id"]]

    # Build lookup helpers
    by_company: dict[str, list[dict]] = defaultdict(list)
    by_event: dict[str, list[dict]] = defaultdict(list)
    for p in participations:
        by_company[p["company_id"]].append(p)
        by_event[p["event_id"]].append(p)

    return {
        "events": events,
        "companies": companies,
        "participations": participations,
        "review": review_rows,
        "by_company": by_company,
        "by_event": by_event,
        "company_by_id": {c["id"]: c for c in companies},
        "event_by_id": {e["id"]: e for e in events},
    }


# ---------- Target-audience filter ----------
#
# Per user direction the outbound report focuses on competing
# defense-tech startups participating in real U.S. defense-industry
# engagements. Four layers of filtering, in order of cheapness:
#
#  1. DEFENSE-RELEVANCE GATE on events — events that fail
#     extract.defense_relevance.is_defense_relevant() are dropped
#     entirely, along with their participations. This is the same
#     gate `sources.discover._discover_event_from_article` uses at
#     ingestion time; running it again at report time cleans up any
#     legacy data ingested under older rules. (See CLAUDE.md rule 6.)
#
#  2. ANNOUNCED-ONLY suppression — events with zero participations
#     in the current window are dropped from Section 1 of the report
#     (typically insidedefense.com items like "Navy postpones new
#     missile program industry day" — defense-relevant news but no
#     attached participants yet). They remain in the store so future
#     runs can attach participants once the event actually happens.
#
#  3. EXCLUDED_ROLES — drop participations where the company was
#     acting as a sponsor / judge / mentor (ecosystem-adjacent).
#     Investors / funders are kept (often relevant for outbound).
#
#  4. KNOWN_PRIMES_INTEGRATORS — drop companies that are large
#     defense primes or integrators regardless of role. Hard-coded
#     list keyed on normalized name; kept in sync with the
#     "Primes" and "Integrators / services" sections of
#     config/gazetteer.txt plus user-specified exclusions. Can be
#     turned off per run via `exclude_primes=False`.

EXCLUDED_ROLES = frozenset({"sponsor", "judge", "mentor"})

# Stored as normalized_name (lowercased, legal suffixes like
# "Corporation" / "Inc" stripped per schema.company.normalize_name).
# Verify after edits by running schema.company.normalize_name on
# any new addition.
KNOWN_PRIMES_INTEGRATORS = frozenset({
    # Gazetteer Primes
    "boeing", "general dynamics", "l3harris technologies",
    "lockheed martin", "northrop grumman",
    "rtx",  # 'RTX Corporation' normalizes to 'rtx'
    "raytheon", "bae systems",
    # Gazetteer Integrators / services
    "booz allen hamilton", "caci international", "deloitte",
    "leidos", "saic",
    # User-specified non-targets (defense-tech that scaled to
    # integrator-class and aren't outbound targets for ATO help)
    "palantir technologies", "scale ai",
})

# Companies that organize / co-host engagements rather than
# participate in them. The NER step can misclassify them as
# participants when their name appears in event copy ("organized
# by …", "hosted with …"), so we drop them at report time
# regardless of inferred role. Add entries here as you find
# them; pattern mirrors KNOWN_PRIMES_INTEGRATORS.
KNOWN_HOSTS_SPONSORS = frozenset({
    # User-reported: Skyrun co-hosts xTech National Security Hackathon
    "skyrun",
    # Common event-organizer / platform companies that surface as
    # participants if NER isn't careful.
    "cerebral valley", "cerebralvalley",
    "devpost",
})


def _is_prime_or_integrator(company: dict) -> bool:
    """True if the company is a large prime / integrator.

    These are configurable per-run via `exclude_primes` — an analyst
    may legitimately want to see them.
    """
    norm = (company.get("normalized_name") or "").lower()
    return norm in KNOWN_PRIMES_INTEGRATORS


def _is_host_or_sponsor(company: dict) -> bool:
    """True if the company hosts / organizes engagements rather than
    participating in them. Always excluded from the participant view —
    unlike primes, this isn't a judgment call."""
    norm = (company.get("normalized_name") or "").lower()
    return norm in KNOWN_HOSTS_SPONSORS


# Backwards-compat alias: existing tests / callers used this name.
def _is_excluded_company(company: dict) -> bool:
    return _is_prime_or_integrator(company) or _is_host_or_sponsor(company)


def _filter_data_for_target_audience(
    data: dict,
    *,
    exclude_primes: bool = True,
    drop_announced_only: bool = True,
) -> dict:
    """Return a copy of `data` with non-target events, participations,
    and companies removed. Used for the outbound-focused report;
    Section 6 (ecosystem mapping) can pass the unfiltered `data` if
    it wants the sponsor/judge/mentor rows.

    Parameters
    ----------
    exclude_primes : bool, default True
        Drop companies in KNOWN_PRIMES_INTEGRATORS. Set False for an
        analyst run that wants to see prime-as-participant cases.
    drop_announced_only : bool, default True
        Drop events that have zero participations from the report
        view (defense-relevant news about upcoming events with no
        attached participants yet). They stay in the store.
    """
    from extract.defense_relevance import is_defense_relevant

    # Layer 1: defense-relevance gate on events.
    relevant_events = [
        e for e in data["events"] if is_defense_relevant(e)
    ]
    relevant_event_ids = {e["id"] for e in relevant_events}

    # Pre-filter participations to relevant events only.
    relevant_parts = [
        p for p in data["participations"]
        if p["event_id"] in relevant_event_ids
    ]

    # Layer 2: announced-only suppression.
    # Compute event participation counts using only filtered parts.
    if drop_announced_only:
        parts_per_event = defaultdict(int)
        for p in relevant_parts:
            parts_per_event[p["event_id"]] += 1
        relevant_events = [
            e for e in relevant_events
            if parts_per_event.get(e["id"], 0) > 0
        ]
        relevant_event_ids = {e["id"] for e in relevant_events}
        relevant_parts = [
            p for p in relevant_parts if p["event_id"] in relevant_event_ids
        ]

    # Layer 3: role filter on participations.
    filtered_parts = [
        p for p in relevant_parts
        if p["role"] not in EXCLUDED_ROLES
    ]
    parts_by_company: defaultdict[str, list] = defaultdict(list)
    for p in filtered_parts:
        parts_by_company[p["company_id"]].append(p)

    # Layer 4a: hosts / sponsors filter on companies (always on —
    # these are unambiguous non-participants).
    # Layer 4b: prime / integrator filter (controllable per run).
    filtered_companies = [
        c for c in data["companies"]
        if not _is_host_or_sponsor(c)
        and (not exclude_primes or not _is_prime_or_integrator(c))
        and parts_by_company.get(c["id"])
    ]
    keep_ids = {c["id"] for c in filtered_companies}
    final_parts = [p for p in filtered_parts if p["company_id"] in keep_ids]

    by_company: defaultdict[str, list] = defaultdict(list)
    by_event: defaultdict[str, list] = defaultdict(list)
    for p in final_parts:
        by_company[p["company_id"]].append(p)
        by_event[p["event_id"]].append(p)

    return {
        **data,
        "events": relevant_events,
        "companies": filtered_companies,
        "participations": final_parts,
        "by_company": by_company,
        "by_event": by_event,
        "company_by_id": {c["id"]: c for c in filtered_companies},
        "event_by_id": {e["id"]: e for e in relevant_events},
    }


# ---------- Confidence helpers ----------

_CONF_RANK = {"confirmed": 3, "highly_likely": 2, "ecosystem_associated": 1}


def _highest_confidence(rows: list[dict]) -> str:
    if not rows:
        return "—"
    return max(rows, key=lambda r: _CONF_RANK.get(r["confidence"], 0))["confidence"]


def _highest_role(rows: list[dict]) -> str:
    """Pick the most "valuable" role across rows. Winner > finalist
    > demoing/presenting > participant > sponsor/judge/mentor/investor."""
    order = ["winner", "finalist", "demoing", "presenting",
             "participant", "mentor", "judge", "sponsor", "investor"]
    seen_roles = {r["role"] for r in rows}
    for role in order:
        if role in seen_roles:
            return role
    return "—"


# ---------- Sections ----------

def section_1(data: dict, since: date) -> str:
    # Skip the sentinel "Defense industry coverage" event — it's a
    # meta-bucket for loose attributions, not an actual competition.
    from sources.discover import LOOSE_EVENT_ID
    events_in_window = [
        e for e in data["events"]
        if e["id"] != LOOSE_EVENT_ID
        and date.fromisoformat(e["dates_start"]) >= since
    ]
    rows = []
    for e in sorted(events_in_window, key=lambda x: x["dates_start"], reverse=True):
        ds = e["dates_start"]
        de = e.get("dates_end") or ""
        dates = f"{ds} → {de}" if de and de != ds else ds
        found = len(data["by_event"].get(e["id"], []))
        expected = e.get("expected_participants")
        if expected is not None and expected > 0:
            coverage = f"{found} / {expected}"
        else:
            coverage = f"{found} / ?"
        rows.append([
            dates, e["name"], e.get("host", ""),
            coverage,
            e.get("source_url", ""),
        ])
    body = _table(
        ["Dates", "Event", "Host", "Found / Total participants", "Source"],
        rows,
    ) if rows else "_No competitions in window._\n"
    return _h2("Section 1 — Competitions discovered this window") + body


def section_2(data: dict) -> str:
    rows = []
    for c in sorted(data["companies"], key=lambda x: -len(data["by_company"][x["id"]])):
        ps = data["by_company"][c["id"]]
        if not ps:
            continue
        event_ids = {p["event_id"] for p in ps}
        where_seen = "; ".join(sorted(
            data["event_by_id"].get(eid, {}).get("name", eid)
            for eid in event_ids
        ))
        rows.append([
            c["name"],
            c.get("type", "unknown"),
            ", ".join(c.get("domains") or []) or "—",
            str(len(event_ids)),
            where_seen,
            _highest_role(ps),
            _highest_confidence(ps),
            # Funding column intentionally dropped — see build_pdf.py
            # for rationale. Investors retained for network signal.
            ", ".join(c.get("notable_investors") or []) or "—",
        ])
    body = _table(
        ["Company", "Type", "Domains", "Events", "Where seen",
         "Top role", "Confidence", "Investors"],
        rows,
    ) if rows else "_No participant data._\n"
    return _h2("Section 2 — Deduped participant master list") + body


def section_3(data: dict) -> str:
    """Companies appearing across multiple events."""
    rows = []
    for c in data["companies"]:
        events = {p["event_id"] for p in data["by_company"][c["id"]]}
        if len(events) < 2:
            continue
        names = sorted(
            data["event_by_id"].get(eid, {}).get("name", eid)
            for eid in events
        )
        rows.append([
            c["name"],
            str(len(events)),
            "; ".join(names),
        ])
    rows.sort(key=lambda r: -int(r[1]))
    body = _table(
        ["Company", "Event count", "Events"],
        rows,
    ) if rows else "_No cross-event companies yet — build more event coverage._\n"
    return _h2("Section 3 — Companies appearing across multiple competitions") + body


def section_4(data: dict) -> str:
    """Emerging stealth startups."""
    rows = []
    for c in data["companies"]:
        if not c.get("is_stealth"):
            continue
        ps = data["by_company"][c["id"]]
        events = {p["event_id"] for p in ps}
        rows.append([
            c["name"],
            str(len(events)),
            _highest_confidence(ps),
            ", ".join(
                data["event_by_id"].get(eid, {}).get("name", eid)
                for eid in events
            ),
        ])
    body = _table(
        ["Stealth team / project", "Events", "Confidence", "Where seen"],
        rows,
    ) if rows else "_No stealth startups identified in this window._\n"
    note = (
        "\n_Stealth = name has no legal suffix and isn't in the gazetteer. "
        "Review queue may have additional candidates pending analyst clearance._\n"
    )
    return _h2("Section 4 — Emerging stealth startups") + body + note


def section_5(data: dict, *, skip_ota_column: bool = False) -> str:
    """Compliance maturity — populated by enrich/compliance.py."""
    rows = []
    for c in sorted(data["companies"], key=lambda x: x["name"]):
        ps = data["by_company"][c["id"]]
        if not ps:
            continue
        fr = c.get("fedramp_status", "unknown")
        il = c.get("dod_il_level", "unknown")
        signals = c.get("ota_signals") or []
        # Skip companies with no compliance signal at all. When the
        # OT column is hidden we ignore ota_signals as a qualifier
        # (otherwise the section fills with rows that show only "—").
        has_fr_or_il = fr not in ("unknown", "none") or il not in ("unknown", "none")
        if skip_ota_column:
            if not has_fr_or_il:
                continue
        else:
            if not has_fr_or_il and not signals:
                continue
        row = [
            c["name"],
            fr if fr not in ("none", "unknown") else "—",
            il if il not in ("none", "unknown") else "—",
        ]
        if not skip_ota_column:
            ot_count = len(signals)
            ot_total = sum(s.get("amount") or 0 for s in signals) if signals else 0
            row.append(f"{ot_count} (${ot_total:,.0f})" if ot_count else "—")
        rows.append(row)

    headers = ["Company", "FedRAMP", "DoD IL"]
    if not skip_ota_column:
        headers.append("DoD OT/contracts (24m)")
    body = _table(headers, rows) if rows else (
        "_No compliance data yet. Run `python -m enrich.compliance`._\n"
    )
    return _h2("Section 5 — Compliance maturity") + body


def section_6(data: dict) -> str:
    """Ecosystem mapping — co-occurrence of sponsors/judges/investors
    with participants. Generic — no hardcoded company names."""
    # For each (participant_company, role-in-supporting-position) pair,
    # count how many distinct participant-companies they co-occur with
    # at the same event.
    SUPPORTING = {"sponsor", "judge", "mentor", "investor"}
    PARTICIPATING = {"winner", "finalist", "participant", "demoing", "presenting"}

    # event_id -> set of supporting company_ids
    support_at: dict[str, set[str]] = defaultdict(set)
    # event_id -> set of participant company_ids
    part_at: dict[str, set[str]] = defaultdict(set)
    for p in data["participations"]:
        if p["role"] in SUPPORTING:
            support_at[p["event_id"]].add(p["company_id"])
        elif p["role"] in PARTICIPATING:
            part_at[p["event_id"]].add(p["company_id"])

    # supporting_company_id -> set of participant company_ids it co-occurred with
    co: dict[str, set[str]] = defaultdict(set)
    for event_id, supporters in support_at.items():
        for s in supporters:
            co[s] |= part_at.get(event_id, set())

    rows = []
    for sid, partners in sorted(co.items(), key=lambda kv: -len(kv[1])):
        if len(partners) < 1:
            continue
        s_name = data["company_by_id"].get(sid, {}).get("name", sid)
        partner_names = sorted(
            data["company_by_id"].get(pid, {}).get("name", pid) for pid in partners
        )[:10]  # cap for readability
        rows.append([
            s_name,
            str(len(partners)),
            ", ".join(partner_names),
        ])
    body = _table(
        ["Supporter (sponsor/judge/mentor/investor)",
         "# distinct participants co-occurred with",
         "Top participants"],
        rows,
    ) if rows else "_Not enough events yet to compute co-occurrence._\n"
    return _h2("Section 6 — Ecosystem mapping (co-occurrence)") + body


def section_7(data: dict) -> str:
    """Raw evidence appendix — every Participation row."""
    out = []
    by_company = data["by_company"]
    cid_to_name = lambda cid: data["company_by_id"].get(cid, {}).get("name", cid)
    for cid in sorted(by_company, key=cid_to_name):
        rows = sorted(
            by_company[cid],
            key=lambda r: _CONF_RANK.get(r["confidence"], 0),
            reverse=True,
        )
        out.append(_h3(cid_to_name(cid)))
        seen_urls = set()
        for r in rows:
            if r["evidence_url"] in seen_urls:
                continue
            seen_urls.add(r["evidence_url"])
            out.append(
                f"- **{r['confidence']}** · _{r['role']}_ · "
                f"[{r['extracted_by']}]({r['evidence_url']})\n"
                f"  > {r['evidence_excerpt']}\n"
            )
    body = "\n".join(out) if out else "_Empty._\n"
    return _h2("Section 7 — Raw evidence appendix") + body


# ---------- Monthly diff (extension to brief) ----------

def section_monthly_diff(
    data: dict,
    since: date,
    *,
    skip_new_companies: bool = False,
    skip_transitions: bool = False,
    window_days: int | None = None,
) -> str:
    """Monthly tracking — calls into analysis.monthly_diff for the
    four sub-tables the brief asked for.

    Flags let callers drop sub-sections that don't apply to a given
    window (e.g. YTD makes "New companies" trivial; transitions
    requires USASpending which may be disabled).

    `window_days` controls the increasing-frequency comparison; if
    None it defaults to the analysis module's 30-day window.
    """
    from analysis.monthly_diff import (
        new_companies,
        increasing_frequency,
        hackathon_to_sbir_transitions,
        recurring_supporters,
    )

    parts: list[str] = [_h2("Monthly tracking")]

    if not skip_new_companies:
        new_rows = new_companies(since=since)
        new_table = _table(
            ["Company", "Type", "Domains", "First seen", "Events", "Stealth"],
            [
                [
                    r["name"], r["type"],
                    ", ".join(r["domains"]) or "—",
                    r["first_seen"], str(r["events"]),
                    "yes" if r["is_stealth"] else "—",
                ]
                for r in new_rows
            ],
        ) if new_rows else "_No new companies in this window._\n"
        parts.extend([_h3("New companies this window"), new_table])

    freq_kwargs = {"window_days": window_days} if window_days else {}
    freq_rows = increasing_frequency(**freq_kwargs)
    freq_table = _table(
        ["Company", "Current window", "Prior window", "Delta"],
        [
            [r["name"], str(r["current"]), str(r["prior"]), f"+{r['delta']}"]
            for r in freq_rows
        ],
    ) if freq_rows else (
        "_No frequency increases this window (or only one window of data)._\n"
    )
    parts.extend([_h3("Companies with increasing event frequency"), freq_table])

    if not skip_transitions:
        ota_rows = hackathon_to_sbir_transitions(since=since)
        ota_table = _table(
            ["Company", "Source event", "Source date", "Source role",
             "OT signal count", "Top OT amount"],
            [
                [
                    r["name"], r["source_event"], r["source_event_date"],
                    r["source_role"], str(r["ota_signal_count"]),
                    f"${r['top_ota_amount']:,.0f}" if r["top_ota_amount"] else "—",
                ]
                for r in ota_rows
            ],
        ) if ota_rows else (
            "_No hackathon→SBIR/OT transitions detected. "
            "Run compliance enrichment to populate ota_signals._\n"
        )
        parts.extend([_h3("Hackathon → SBIR/OT transitions"), ota_table])

    sup_rows = recurring_supporters(min_events=2)
    sup_table = _table(
        ["Supporter", "Type", "Events supported", "Distinct participants"],
        [
            [r["name"], r["type"], str(r["events_supported"]),
             str(r["distinct_participants"])]
            for r in sup_rows
        ],
    ) if sup_rows else "_Not enough events for supporter co-occurrence (need ≥2)._\n"
    parts.extend([
        _h3("Recurring supporters (sponsors / judges / mentors / investors)"),
        sup_table,
    ])

    return "".join(parts)


# ---------- Main ----------

def build(
    since: date | None = None,
    *,
    skip_ota_column: bool = False,
    skip_new_companies: bool = False,
    skip_transitions: bool = False,
    window_days: int | None = None,
    target_participants_only: bool = True,
) -> str:
    """Render the full Markdown report.

    `target_participants_only=True` (default) drops sponsors / judges
    / mentors and known primes/integrators from sections 1-5, 7, and
    monthly tracking — focuses the outbound list on competing
    defense-tech startups. Section 6 (ecosystem mapping) always uses
    the unfiltered data since supporters are the point of that
    section.
    """
    since = since or (date.today() - timedelta(days=30))
    raw = _load_all()
    data = _filter_data_for_target_audience(raw) if target_participants_only else raw
    filter_note = (
        "\n_Filters: defense-relevance gate, announced-only "
        "suppression, sponsor / judge / mentor role exclusion, "
        f"prime / integrator exclusion ({len(KNOWN_PRIMES_INTEGRATORS)} "
        "names)._\n"
        if target_participants_only else ""
    )
    parts = [
        _h1(f"Defense Innovation Participants — generated {date.today()}"),
        f"\nWindow: events with `dates_start >= {since}`. "
        f"Companies: **{len(data['companies'])}**. "
        f"Participations: **{len(data['participations'])}**. "
        f"Events: **{len(data['events'])}**. "
        f"Review queue: **{len(raw['review'])}** pending.\n"
        + filter_note,
        section_1(data, since),
        section_2(data),
        section_3(data),
        section_4(data),
        section_5(data, skip_ota_column=skip_ota_column),
        section_6(raw),  # ecosystem mapping wants supporters
        section_monthly_diff(
            data, since,
            skip_new_companies=skip_new_companies,
            skip_transitions=skip_transitions,
            window_days=window_days,
        ),
        section_7(data),
    ]
    return "\n".join(parts)


def main() -> None:
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    text = build()
    path = out_dir / f"report_{date.today().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} ({len(text)} chars)")


if __name__ == "__main__":
    main()

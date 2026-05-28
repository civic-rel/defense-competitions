"""PDF renderer for the monthly report.

Renders the same data the markdown builder produces, but directly to
PDF using reportlab. No external tooling (pandoc, wkhtmltopdf, cairo)
required — pure Python.

Run:
    python -m reports.build_pdf
    # writes reports/out/report_<YYYY-MM-DD>.pdf
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from reports.build_markdown import (
    _CONF_RANK,
    _filter_data_for_target_audience,
    _highest_confidence,
    _highest_role,
    _load_all,
)


# ---- ATO-outreach scoring helper ----
#
# Used by _section_outreach_priority() to rank companies that fit
# the outreach use case: they participated in real defense
# engagements, they don't already have FedRAMP / DoD IL coverage,
# and our evidence for the participation is strong.

def _is_ato_outreach_candidate(company: dict, participations: list[dict]) -> bool:
    """True if this company is a viable ATO-outreach target.

    Filters on FedRAMP only. We do not gate on DoD IL because no
    public API publishes per-company IL Provisional Authorization
    status — DISA's authoritative list lives behind PKI-gated
    public.cyber.mil/dccs/cso/. A company can be IL-authorized
    without us knowing. The assumption: long-tail startups are
    overwhelmingly NOT IL-authorized, so absence-of-data is the
    safe default for the outreach use case.

    A candidate is:
      - Has at least one Participation surviving the report filter
        (guaranteed by the caller).
      - Is NOT FedRAMP Certified (CR26 label; the legacy
        "authorized" value is treated equivalently). Companies in
        Preparation, In Process, or Remediation are kept — they
        are pursuing authorization and are the warmest leads.
    """
    fr = (company.get("fedramp_status") or "none").lower()
    return fr not in ("certified", "authorized")


def _fedramp_display(company: dict) -> str:
    """Render a company's FedRAMP status using CR26 nomenclature
    (per RFC-0020 / NTC-0004, Feb 2026).

    The CR26 Consolidated Rules retired "FedRAMP Authorized" in
    favor of "FedRAMP Certified" (single label for both Rev5 and
    20x — the Certified/Validated split was dropped). "Ready" is
    retiring. Marketplace lifecycle states are:

      None          — not listed on the FedRAMP marketplace at all.
                      Default for the outreach target population.
      Preparation   — provider preparing for assessment.
      In Process    — actively in assessment (covers Agency
                      Authorization In Process, Prioritized, and
                      Assessment by FedRAMP lifecycle states).
      FedRAMP Certified — Continuous Monitoring (Rev5) or
                      Persistent Validation (20x). Replaces the
                      retired "FedRAMP Authorized" label.
      Remediation   — provider correcting a significant issue.

    No "Unknown" — if enrichment ran and didn't find a record,
    the company is not on the marketplace, which is "None".
    """
    fr = (company.get("fedramp_status") or "none").lower().strip()
    return {
        "certified":   "FedRAMP Certified",
        "in_process":  "In Process",
        "preparation": "Preparation",
        "remediation": "Remediation",
        "none":        "None",
        "":            "None",
        # Backwards-compat for any rows still carrying the old
        # vocab; these get normalized on the next enrichment pass.
        "authorized":  "FedRAMP Certified",
        "ready":       "Preparation",
        "unknown":     "None",
    }.get(fr, "None")


def _website_display(company: dict) -> str:
    """Return the best contact route for a company.

    Priority:
      1. Real website (Crunchbase, GitHub profile blog, or repo
         homepage — populated by sources/github.py at ingestion).
      2. GitHub profile URL — for repo-discovered companies with
         no separate website, this is the analyst's contact route.
      3. Em-dash if neither exists.

    The single column doubles as "Website / contact" so an analyst
    has one click target per row regardless of company maturity.
    """
    website = (company.get("website") or "").strip()
    if website:
        return website
    # Look at the Crunchbase / extracted aliases for a github.com URL
    # we may have captured. (Aliases are JSON-encoded in the DB.)
    aliases = company.get("aliases") or []
    if isinstance(aliases, str):
        try:
            import json
            aliases = json.loads(aliases)
        except (ValueError, TypeError):
            aliases = []
    for a in aliases:
        if isinstance(a, str) and "github.com/" in a:
            return a
    return "—"


def _outreach_score(company: dict, participations: list[dict]) -> float:
    """Score how strong an outreach candidate this company is.

    Boosts: number of distinct events, highest confidence tier,
    presence of any 'winner'/'finalist' role, presence of a
    FedRAMP "ready"/"in_process" signal (means they've started
    the journey — very warm lead).
    """
    distinct_events = len({p["event_id"] for p in participations})
    roles = {p["role"] for p in participations}
    conf_max = max(
        (_CONF_RANK.get(p["confidence"], 0) for p in participations), default=0
    )
    score = distinct_events * 1.0 + conf_max * 0.5
    if "winner" in roles or "finalist" in roles:
        score += 1.5
    # Boost warm-lead score for any company actively engaged with
    # the FedRAMP process — Preparation / In Process / Remediation
    # under CR26, plus the retired "ready" value for back-compat.
    fr = (company.get("fedramp_status") or "").lower()
    if fr in ("preparation", "in_process", "remediation", "ready"):
        score += 2.0
    return score


# ---- Styles ----

_styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=_styles["Heading1"], fontSize=18, spaceAfter=8)
H2 = ParagraphStyle("H2", parent=_styles["Heading2"], fontSize=13,
                    spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#222"))
H3 = ParagraphStyle("H3", parent=_styles["Heading3"], fontSize=11,
                    spaceBefore=8, spaceAfter=4)
BODY = ParagraphStyle("Body", parent=_styles["BodyText"], fontSize=9,
                      leading=11, spaceAfter=4)
CELL = ParagraphStyle("Cell", parent=_styles["BodyText"], fontSize=7.5,
                      leading=9, wordWrap="CJK")
CELL_HEADER = ParagraphStyle("CellH", parent=CELL, fontName="Helvetica-Bold",
                             textColor=colors.white)
NOTE = ParagraphStyle("Note", parent=BODY, fontSize=8, textColor=colors.grey,
                      fontName="Helvetica-Oblique")


_TABLE_STYLE = TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333")),
    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbb")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("LEFTPADDING", (0, 0), (-1, -1), 3),
    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ("TOPPADDING", (0, 0), (-1, -1), 2),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1),
     [colors.white, colors.HexColor("#f6f6f6")]),
])


def _cell(text: str, header: bool = False) -> Paragraph:
    style = CELL_HEADER if header else CELL
    return Paragraph(str(text).replace("\n", " ").replace("&", "&amp;"), style)


def _table(headers: list[str], rows: Iterable[list[str]], col_widths: list[float]) -> LongTable:
    data = [[_cell(h, header=True) for h in headers]]
    for r in rows:
        data.append([_cell(c) for c in r])
    t = LongTable(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(_TABLE_STYLE)
    return t


# ---- Sections ----

def _section_header(text: str) -> Paragraph:
    return Paragraph(text, H2)


def _section_legend(flow: list) -> None:
    """Legend &amp; methodology — vocabulary, sources, and verification
    notes. Definitions, not commentary.
    """
    flow.append(_section_header("Legend &amp; methodology"))

    # What's in each PDF section
    flow.append(Paragraph(
        "<b>Sections in this PDF</b>", BODY))
    flow.append(_table(
        ["Section", "Contents"],
        [
            ["Outreach priority",
             "Defense-engagement participants not yet FedRAMP "
             "Certified, ranked by evidence strength and competition "
             "footprint. The action list this report is built to "
             "support."],
            ["Section 1 — Competitions discovered",
             "Engagement events in the active window, with "
             "\"Found / Total participants\" coverage indicator."],
            ["Section 4 — Emerging / unverified participants",
             "Project / team names found in evidence but not "
             "matched to a known company. Definition: not in the "
             "gazetteer, no Crunchbase enrichment match, name "
             "passes the non-company stoplist, AND either has a "
             "GitHub URL or has no separate website. Includes the "
             "GitHub URL column for direct outreach to repo owners."],
            ["Section 5 — Compliance maturity",
             "Companies with any FedRAMP signal (Preparation, "
             "In Process, FedRAMP Certified, Remediation). Excludes "
             "rows with no signal at all."],
            ["Section 7 — Raw evidence appendix",
             "One block per company listing every Participation "
             "row with confidence tier, role, extractor tag, and "
             "the clickable evidence URL."],
            ["Analyst playbook",
             "Ten ordered manual-verification steps for expanding "
             "the participant list beyond what automation found."],
            ["Legend &amp; methodology (this section)",
             "Vocabulary, sources used, sources evaluated and not "
             "used, and verification details."],
        ],
        [3.0 * inch, 6.7 * inch],
    ))
    flow.append(Paragraph(
        "Tabular data — master list, cross-event participants, and "
        "raw evidence in spreadsheet form — lives in the companion "
        "XLSX export (<i>report_&lt;date&gt;.xlsx</i>), one sheet "
        "per data view.",
        NOTE))
    flow.append(Spacer(1, 6))

    # Role taxonomy
    flow.append(Paragraph(
        "<b>Top role</b> — the most senior role assigned to a "
        "company across its participations.", BODY))
    flow.append(_table(
        ["Role", "Definition"],
        [
            ["winner", "Named winner of the event or a track."],
            ["finalist", "Reached the named final round; did not win."],
            ["demoing",
             "Demonstrated a product or capability at the event."],
            ["presenting",
             "Delivered a talk, pitch, or briefing at the event."],
            ["participant",
             "Registered or attended; no further placement data."],
            ["sponsor / judge / mentor",
             "Filtered out of the participant view."],
            ["investor",
             "Mentioned in an investor capacity."],
        ],
        [1.7 * inch, 8.0 * inch],
    ))

    # Confidence taxonomy
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>Confidence</b> — three tiers, assigned by the adapter "
        "that wrote the row.", BODY))
    flow.append(_table(
        ["Tier", "Evidence type"],
        [
            ["confirmed",
             "Program's own page (xtech.army.mil, diu.mil, "
             "afwerx.com, sofwerx.org, dronedominance.mil), official "
             "social, or host-org recap. Also: GitHub repos that opt "
             "into a configured event topic tag."],
            ["highly_likely",
             "Editorial recap with a named-author byline "
             "(DefenseScoop, Inside Defense, Breaking Defense, "
             "C4ISRNet); sponsor portfolio post; founder first-person "
             "account; or GitHub repo that matches an event by name "
             "but lacks the opt-in topic tag."],
            ["ecosystem_associated",
             "Photo coverage, third-party social, or co-occurrence "
             "with other named participants."],
        ],
        [1.7 * inch, 8.0 * inch],
    ))

    # Sources we use
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>Sources in use</b> — every row carries an "
        "<i>extracted_by</i> tag identifying its adapter "
        "(visible inline in Section 7).", BODY))
    flow.append(_table(
        ["Adapter", "Data source"],
        [
            ["xtech_official",
             "xtech.army.mil competition + participant sitemaps."],
            ["diu_adapter", "diu.mil/latest articles."],
            ["afwerx_adapter", "afwerx.com news sitemap."],
            ["sofwerx_adapter",
             "events.sofwerx.org sitemap. STEM / outreach events "
             "are filtered by the defense-relevance gate."],
            ["darpa_adapter", "darpa.mil /events sitemap."],
            ["dronedominance_adapter",
             "dronedominance.mil vendors page. Fixture by default; "
             "DRONEDOMINANCE_BACKEND=live to fetch."],
            ["github_adapter",
             "GitHub Search API. Queries configured per event in "
             "config/github_events.yaml. Repos whose derived name "
             "equals the event name are skipped."],
            ["recap_scraper:defensescoop / insidedefense / "
             "breakingdefense",
             "NER over editorial articles surfaced by Brave search."],
            ["samgov_adapter", "SAM.gov special-notices feed."],
            ["sbir_gov", "SBIR.gov solicitation feed (DoD topics only)."],
        ],
        [2.4 * inch, 7.3 * inch],
    ))

    # Sources evaluated and not used
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>Sources evaluated and not used</b> — surfaced during "
        "source research; not currently in the pipeline.", BODY))
    flow.append(_table(
        ["Source", "Status"],
        [
            ["Tradewind Marketplace (tradewindai.com)",
             "Gated behind government account. No public vendor list."],
            ["Capital Factory Defense (capitalfactory.com/government)",
             "No public cohort / alumni roster."],
            ["Plug and Play National Security",
             "No dedicated NS cohort directory."],
            ["AFWERX Spark / Refinery accelerator",
             "No structured cohort listing; announcements appear in "
             "afwerx.com/news (covered by afwerx_adapter)."],
            ["DISA DCCS / public.cyber.mil/dccs/cso/",
             "Authoritative DoD Cloud Service Catalog; PKI-gated. "
             "No public API for DoD IL Provisional Authorization."],
            ["HigherGov / GovTribe",
             "Paid commercial aggregators. Usable if licensed; "
             "not currently enabled."],
            ["LinkedIn / X / Meta automation",
             "ToS-restricted. Manual in-browser checking is "
             "described in the analyst playbook."],
            ["Crunchbase as a discovery source",
             "Contract restricts the paid feed to enrichment of "
             "known companies (already wired)."],
            ["HeroX live solver lists",
             "Solvers self-disclose; full lists not typically public."],
            ["Challenge.gov",
             "No documented public API; recently restructured."],
            ["DevPost /participants pages",
             "Login-gated. Submission galleries are scrapeable "
             "(candidate for future adapter)."],
        ],
        [3.0 * inch, 6.7 * inch],
    ))

    # FedRAMP verification
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>FedRAMP status</b> — labels follow the FedRAMP "
        "Consolidated Rules for 2026 (CR26), per RFC-0020 outcome "
        "<i>NTC-0004</i> (Feb 25 2026). The single official label "
        "is <i>FedRAMP Certification</i> / <i>FedRAMP Certified</i> "
        "— the legacy <i>FedRAMP Authorized</i> term is retired, "
        "as is <i>FedRAMP Ready</i>. Marketplace lifecycle states "
        "are sourced from <i>fedramp.gov/marketplace</i> JSON "
        "exports, refreshed weekly by <i>enrich/compliance.py</i>.",
        BODY))
    flow.append(_table(
        ["Label in report", "Definition"],
        [
            ["None",
             "Not listed on the FedRAMP marketplace under any "
             "lifecycle state. Default for the outreach target "
             "population. (Previously \"Unknown\" was a separate "
             "value; removed because enrichment vetting that "
             "returns no match IS \"None\".)"],
            ["Preparation",
             "Provider is carrying out the essential activities "
             "to prepare the organization for FedRAMP assessment. "
             "Closest successor to the retiring \"FedRAMP Ready\" "
             "state."],
            ["In Process",
             "Actively in assessment. Covers the marketplace "
             "lifecycle states Agency Authorization In Process "
             "(Rev5), Prioritized (20x), and Assessment by FedRAMP."],
            ["FedRAMP Certified",
             "Continuous Monitoring (Rev5) or Persistent "
             "Validation (20x) — completed FedRAMP Certification. "
             "Excluded from outreach priority. Replaces the "
             "legacy \"FedRAMP Authorized\" label."],
            ["Remediation",
             "Provider is correcting a significant underlying "
             "issue with the cloud service, typically as part of "
             "formal corrective action."],
        ],
        [1.7 * inch, 8.0 * inch],
    ))
    flow.append(Paragraph(
        "CR26 also retired the FIPS 199 Low/Moderate/High baseline "
        "labels in favor of Classes A–D (Class A pilot, Class B "
        "covering legacy Li-SaaS+Low, Class C covering Moderate, "
        "Class D covering High). FedRAMP 20x and Rev5 are the two "
        "Certification types under CR26 — RFC-0020 dropped the "
        "proposed Certified/Validated label split after public "
        "comment.",
        NOTE))

    # DoD IL — why no column
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>DoD Impact Level</b> — not shown as a column in this "
        "report. No public API publishes per-company DoD Provisional "
        "Authorization status. DISA's authoritative list "
        "(<i>public.cyber.mil/dccs/cso/</i>) requires DoD PKI / CAC "
        "authentication. The FedRAMP Marketplace surfaces IL only "
        "as a per-product badge on individual listing pages, with "
        "no documented contract API. CSP vendor pages "
        "(Azure Government, AWS GovCloud, Google Cloud) self-publish "
        "their IL status, but this covers only ~15 hyperscalers and "
        "does not scale to long-tail startups. Because the outreach "
        "target population is overwhelmingly NOT IL-authorized, "
        "absence of IL data is the safe default and not a column "
        "worth rendering.", BODY))

    # Website / contact resolution
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>Website / contact column</b> — resolved per company in "
        "this order: (1) Crunchbase website if enrichment matched "
        "the company; (2) for GitHub-discovered companies, the "
        "owner profile <i>blog</i> field; (3) the repo <i>homepage</i> "
        "field; (4) fallback to the owner GitHub profile URL "
        "(<i>https://github.com/&lt;owner&gt;</i>) so analysts have "
        "a contact route even for stealth / student / one-off "
        "submissions. Em-dash if no route is available.", BODY))

    # Coverage caveat
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<b>Found / Total participants (Section 1)</b> — numerator is "
        "what this pipeline attributed to the event. Denominator is "
        "the confirmed total when the source publishes one, or "
        "<i>?</i> when not. Set manual overrides in "
        "<i>config/expected_participants.yaml</i>. The analyst "
        "playbook describes manual steps for closing the gap.", BODY))


def _section_outreach_priority(data: dict, flow: list) -> None:
    """Top-of-report section: ATO-outreach candidates ranked by signal.

    The whole report exists to feed this list. A row here is a
    company that (a) participated in a U.S. defense-industry-facing
    engagement, (b) doesn't already have FedRAMP / DoD IL coverage,
    and (c) we have evidence for. Sorted so the warmest leads land
    at the top.
    """
    flow.append(_section_header(
        "Outreach priority — defense participants without FedRAMP authorization"
    ))
    flow.append(Paragraph(
        "Defense-engagement participants not yet FedRAMP Certified, "
        "ranked by evidence strength and competition footprint. "
        "FedRAMP status column uses CR26 marketplace labels "
        "(per <i>fedramp.gov/notices/0004</i>, Feb 2026): "
        "<i>None</i> (not on the FedRAMP marketplace), "
        "<i>Preparation</i>, <i>In Process</i>, "
        "<i>FedRAMP Certified</i>, or <i>Remediation</i>. "
        "DoD Impact Level is not shown — no public API publishes "
        "per-company IL Provisional Authorization (see the "
        "methodology section).",
        NOTE,
    ))
    candidates: list[tuple[float, dict, list[dict]]] = []
    for c in data["companies"]:
        ps = data["by_company"].get(c["id"], [])
        if not ps:
            continue
        if not _is_ato_outreach_candidate(c, ps):
            continue
        candidates.append((_outreach_score(c, ps), c, ps))
    candidates.sort(key=lambda t: -t[0])

    if not candidates:
        flow.append(Paragraph(
            "<i>No outreach candidates in this window.</i>",
            BODY,
        ))
        return

    rows = []
    for score, c, ps in candidates:
        event_ids = {p["event_id"] for p in ps}
        where_seen = "; ".join(sorted(
            data["event_by_id"].get(eid, {}).get("name", eid)
            for eid in event_ids
        ))
        rows.append([
            c["name"],
            ", ".join(c.get("domains") or []) or "—",
            str(len(event_ids)),
            _highest_role(ps),
            _highest_confidence(ps),
            _fedramp_display(c),
            c.get("last_seen") or "—",
            _website_display(c),
            where_seen,
        ])
    flow.append(_table(
        ["Company", "Domains", "Events", "Top role", "Confidence",
         "FedRAMP status", "Last seen", "Website / contact", "Where seen"],
        rows,
        [1.3 * inch, 1.0 * inch, 0.45 * inch, 0.7 * inch, 0.85 * inch,
         1.2 * inch, 0.7 * inch, 1.4 * inch, 2.7 * inch],
    ))


def _section_1(data: dict, since: date, flow: list) -> None:
    flow.append(_section_header("Section 1 — Competitions discovered this window"))
    from sources.discover import LOOSE_EVENT_ID
    events = [
        e for e in data["events"]
        if e["id"] != LOOSE_EVENT_ID
        and date.fromisoformat(e["dates_start"]) >= since
    ]
    if not events:
        flow.append(Paragraph("<i>No competitions in window.</i>", BODY))
        return
    rows = []
    for e in sorted(events, key=lambda x: x["dates_start"], reverse=True):
        ds = e["dates_start"]
        de = e.get("dates_end") or ""
        dates = f"{ds} → {de}" if de and de != ds else ds
        found = len(data["by_event"].get(e["id"], []))
        expected = e.get("expected_participants")
        # "Found / Total" — when the source publishes a confirmed
        # total we show it; otherwise the denominator is "?" so the
        # reader can see how much coverage we know we're missing.
        if expected is not None and expected > 0:
            coverage = f"{found} / {expected}"
        else:
            coverage = f"{found} / ?"
        rows.append([
            dates, e["name"], e.get("host", ""),
            coverage,
            e.get("source_url", ""),
        ])
    flow.append(_table(
        ["Dates", "Event", "Host", "Found / Total participants", "Source URL"],
        rows,
        [1.0 * inch, 2.0 * inch, 1.5 * inch, 1.2 * inch, 4.3 * inch],
    ))


def _section_2(data: dict, flow: list) -> None:
    flow.append(_section_header("Section 2 — Deduped participant master list"))
    rows = []
    for c in sorted(
        data["companies"], key=lambda x: -len(data["by_company"][x["id"]])
    ):
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
            # Funding column intentionally removed from Section 2 — we
            # don't independently validate Crunchbase totals and the
            # number is rarely useful for outreach decisions. Investors
            # stays because the network signal (who's backed them) is
            # outreach-relevant even if stale.
            ", ".join(c.get("notable_investors") or []) or "—",
        ])
    if not rows:
        flow.append(Paragraph("<i>No participant data.</i>", BODY))
        return
    flow.append(_table(
        ["Company", "Type", "Domains", "Events", "Where seen",
         "Top role", "Confidence", "Investors"],
        rows,
        [1.5 * inch, 0.7 * inch, 1.0 * inch, 0.4 * inch, 2.1 * inch,
         0.7 * inch, 0.9 * inch, 3.3 * inch],
    ))


def _section_3(data: dict, flow: list) -> None:
    flow.append(_section_header("Section 3 — Companies appearing across multiple competitions"))
    rows = []
    for c in data["companies"]:
        events = {p["event_id"] for p in data["by_company"][c["id"]]}
        if len(events) < 2:
            continue
        names = sorted(
            data["event_by_id"].get(eid, {}).get("name", eid) for eid in events
        )
        rows.append([c["name"], str(len(events)), "; ".join(names)])
    rows.sort(key=lambda r: -int(r[1]))
    if not rows:
        flow.append(Paragraph(
            "<i>No companies appear at more than one event in this "
            "window.</i>",
            BODY,
        ))
        return
    flow.append(_table(
        ["Company", "Event count", "Events"], rows,
        [2.0 * inch, 1.0 * inch, 7.0 * inch],
    ))


def _is_emerging_unverified(company: dict) -> bool:
    """Return True if the company qualifies for the Emerging /
    unverified section.

    Definition (per CR26 reporting rules):
      - Has at least one Participation surviving the report filter
        (caller guarantees this).
      - Not in the gazetteer (we don't recognize the name).
      - No Crunchbase enrichment (type is 'unknown' or empty —
        Crunchbase did not match).
      - Name passes the non-company stoplist (rules out fragments
        like \"Amm\", \"Cleantech\", \"Federal Systems\", etc.).
      - Either has a GitHub URL alias, or has no website (the
        \"can't find them on the public web\" signal).
    """
    from extract.company_match import _is_non_company

    name = company.get("name") or ""
    if not name or _is_non_company(name):
        return False
    type_ = (company.get("type") or "unknown").lower()
    if type_ not in ("unknown", ""):
        return False
    website = (company.get("website") or "").strip()
    aliases = company.get("aliases") or []
    if isinstance(aliases, str):
        try:
            import json
            aliases = json.loads(aliases)
        except (ValueError, TypeError):
            aliases = []
    has_github = any("github.com/" in (a or "") for a in aliases) or (
        "github.com/" in website
    )
    # Qualifies if it has a GitHub URL (real artifact) OR no website
    # at all (truly unverified). Companies with a real website but
    # no Crunchbase match are excluded — they're known to the web,
    # just not to our enrichment.
    return has_github or not website


def _emerging_github_url(company: dict) -> str:
    """Best GitHub URL for an emerging entry, or empty string."""
    website = (company.get("website") or "").strip()
    if "github.com/" in website:
        return website
    aliases = company.get("aliases") or []
    if isinstance(aliases, str):
        try:
            import json
            aliases = json.loads(aliases)
        except (ValueError, TypeError):
            aliases = []
    for a in aliases:
        if a and "github.com/" in a:
            return a
    return ""


def _section_4(data: dict, flow: list) -> None:
    flow.append(_section_header(
        "Section 4 — Emerging / unverified participants"
    ))
    rows = []
    for c in data["companies"]:
        if not _is_emerging_unverified(c):
            continue
        ps = data["by_company"][c["id"]]
        events = {p["event_id"] for p in ps}
        github_url = _emerging_github_url(c)
        rows.append([
            c["name"],
            str(len(events)),
            _highest_confidence(ps),
            github_url or "—",
            ", ".join(
                data["event_by_id"].get(eid, {}).get("name", eid)
                for eid in events
            ),
        ])
    if not rows:
        flow.append(Paragraph(
            "<i>No emerging / unverified participants in this window.</i>",
            BODY,
        ))
        return
    flow.append(_table(
        ["Project / team", "Events", "Confidence", "GitHub", "Where seen"],
        rows,
        [1.8 * inch, 0.5 * inch, 0.95 * inch, 1.9 * inch, 4.85 * inch],
    ))


def _section_5(data: dict, flow: list, *, skip_ota_column: bool = False) -> None:
    flow.append(_section_header("Section 5 — Compliance maturity"))
    rows = []
    for c in sorted(data["companies"], key=lambda x: x["name"]):
        ps = data["by_company"][c["id"]]
        if not ps:
            continue
        fr = (c.get("fedramp_status") or "none").lower()
        il = (c.get("dod_il_level") or "none").lower()
        signals = c.get("ota_signals") or []
        has_fr_or_il = (
            fr not in ("none", "unknown") or il not in ("none", "unknown")
        )
        if skip_ota_column:
            if not has_fr_or_il:
                continue
        else:
            if not has_fr_or_il and not signals:
                continue
        # Use the same display mapping as outreach priority so the
        # labels are consistent across the report.
        row = [
            c["name"],
            _fedramp_display(c),
            il.upper() if il not in ("none", "unknown") else "—",
        ]
        if not skip_ota_column:
            ot_count = len(signals)
            ot_total = sum(s.get("amount") or 0 for s in signals) if signals else 0
            row.append(f"{ot_count} (${ot_total:,.0f})" if ot_count else "—")
        rows.append(row)
    if not rows:
        flow.append(Paragraph("<i>No compliance data yet.</i>", BODY))
        return
    if skip_ota_column:
        headers = ["Company", "FedRAMP", "DoD IL"]
        widths = [4.0 * inch, 3.0 * inch, 3.0 * inch]
    else:
        headers = ["Company", "FedRAMP", "DoD IL", "DoD OT/contracts (24m)"]
        widths = [3.0 * inch, 1.2 * inch, 1.0 * inch, 4.8 * inch]
    flow.append(_table(headers, rows, widths))


_URL_JUNK_RE = __import__("re").compile(
    # Strip control / object-replacement / zero-width characters that
    # leak into evidence_urls when the scraped page has embedded images
    # or copy-pasted Unicode garbage. %EF%BF%BC (U+FFFC OBJECT
    # REPLACEMENT CHARACTER) is the common offender from defensescoop
    # articles. Also strip whitespace and stray angle brackets.
    r"(?:%EF%BF%BC|%E2%80%8B|%EF%BB%BF|[​‌‍﻿￼])+",
    __import__("re").IGNORECASE,
)


def _clean_url(url: str) -> str:
    """Strip junk characters from an evidence URL so links don't 404."""
    if not url:
        return url
    cleaned = _URL_JUNK_RE.sub("", url).strip().strip("<>").rstrip("/")
    # Re-add trailing slash if the original had one (some sites are
    # sensitive). But only if there's no query/fragment.
    if url.endswith("/") and "?" not in cleaned and "#" not in cleaned:
        cleaned += "/"
    return cleaned


# URL patterns that mark a row as fictional fixture data — used by
# the offline demo (`run_final_demo.py`) and tests. Any URL matching
# these is real evidence of pipeline behavior but NOT a real
# clickable web page. We mark these visibly in the report so a
# reader can tell demo output from production output at a glance.
_FIXTURE_URL_PATTERNS = (
    "xtech-natsec-hackathon-winners-illustrative",  # demo's simulated DefenseScoop article
    "/fixtures/",  # local file paths if ever leaked
)


def _is_fixture_url(url: str) -> bool:
    if not url:
        return False
    return any(p in url for p in _FIXTURE_URL_PATTERNS)


def _xml_escape(s: str) -> str:
    """Escape characters that would break ReportLab Paragraph XML."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _display_url(url: str, *, max_len: int = 70) -> str:
    """Shorten a URL for display while keeping the most useful parts.

    Keeps the domain and as much of the path as fits; trims from the
    middle if needed. The full URL is what gets clicked (via the link
    target), so this is purely cosmetic.
    """
    if len(url) <= max_len:
        return url
    head_keep = max_len // 2 - 1
    tail_keep = max_len - head_keep - 1
    return url[:head_keep] + "…" + url[-tail_keep:]


def _section_analyst_playbook(data: dict, flow: list) -> None:
    """Inline playbook — the manual steps to expand a participant list
    beyond what automation found. Lives in the PDF so the analyst
    always has it next to the data they're acting on.
    """
    flow.append(_section_header("Analyst playbook — manual verification"))
    flow.append(Paragraph(
        "Steps to expand a participant list beyond what automation "
        "found. The \"Found / Total\" column in Section 1 indicates "
        "remaining gap. Work the steps in order; each is roughly "
        "5–15 minutes per event.", BODY))

    flow.append(Paragraph("<b>1. Hit the event's own page.</b>", H3))
    flow.append(Paragraph(
        "Open the URL in Section 1's \"Source URL\" column. xTech, "
        "DIU, AFWERX, and SOFWERX competition pages routinely list "
        "finalists, semifinalists, and sometimes the full applicant "
        "set under sections labeled \"Finalists,\" \"Selected "
        "Companies,\" \"Cohort,\" or \"Participants.\" Copy names "
        "into a scratchpad. For xTech specifically, also check "
        "<i>xtech.army.mil/participants/?competition=&lt;slug&gt;</i> "
        "— it lists everyone, not just finalists.", BODY))

    flow.append(Paragraph("<b>2. Search the host's social.</b>", H3))
    flow.append(Paragraph(
        "On X/Twitter: <i>from:DoDDIU</i>, <i>from:AFWERX</i>, "
        "<i>from:USSOCOM_SOFWERX</i>, <i>from:DARPA</i>, "
        "<i>from:ArmyxTech</i> — filtered to the week of the event. "
        "Recap threads frequently quote-tweet or @-mention every "
        "selected company. LinkedIn: search the host org's company "
        "page for posts dated within ±7 days of the event.", BODY))

    flow.append(Paragraph("<b>3. YouTube event recaps.</b>", H3))
    flow.append(Paragraph(
        "Search YouTube for the event name + \"recap\" or \"demo "
        "day.\" Watch with the closed-captions panel open — team "
        "names appear in lower-thirds and on demo screens. "
        "DARPA, AFWERX, and DIU all publish hour-long recap videos "
        "that name every demoing team. ~10 minutes per video; "
        "scrub through the demo segments.", BODY))

    flow.append(Paragraph("<b>4. Trade-press recaps.</b>", H3))
    flow.append(Paragraph(
        "DefenseScoop, Inside Defense, Breaking Defense, and "
        "C4ISRNet publish recap articles for major events. The "
        "Brave-search adapter pulls some of these, but not all "
        "(per-source query cap). Manual search for "
        "<i>&lt;event name&gt; site:defensescoop.com</i> in any "
        "search engine usually turns up the article. Named "
        "companies in the body become new participation rows — add "
        "them manually via the review queue or the Python REPL.", BODY))

    flow.append(Paragraph("<b>5. Sponsor / partner pages.</b>", H3))
    flow.append(Paragraph(
        "Many engagements have a corporate sponsor (often a VC or "
        "an industry consortium) whose website includes a participant "
        "directory. Examples: Riot Ventures portfolio pages, "
        "Capital Factory Defense BBQ alumni lists, NSIN cohort "
        "directories.", BODY))

    flow.append(Paragraph("<b>6. Individual company press releases.</b>", H3))
    flow.append(Paragraph(
        "Startups publish their own \"we made the finalist round "
        "of …\" pieces. Search <i>\"&lt;event name&gt; finalist\" OR "
        "\"&lt;event name&gt; winner\" site:prnewswire.com OR "
        "site:businesswire.com</i>. Each hit is one or more named "
        "participants. Also check the company's own /news /press "
        "page when you have a known finalist — they often mention "
        "others by name.", BODY))

    flow.append(Paragraph("<b>7. GitHub for technical events.</b>", H3))
    flow.append(Paragraph(
        "For events with a software component (most hackathons, "
        "many tech sprints), search GitHub for repos tagged with "
        "the event slug or named after the event. Each repo's "
        "README usually credits the team; the owner's profile may "
        "name a company. The github adapter automates the "
        "highest-signal version of this; manual work catches repos "
        "that didn't opt into a topic tag.", BODY))

    flow.append(Paragraph("<b>8. Wikipedia / community wikis.</b>", H3))
    flow.append(Paragraph(
        "DEF CON Aerospace Village, the Army Software Factory, "
        "and a few of the larger DARPA challenges have Wikipedia "
        "pages with full finalist tables. Worth checking once per "
        "major event.", BODY))

    flow.append(Paragraph("<b>9. Adding what you find.</b>", H3))
    flow.append(Paragraph(
        "For each new name: confirm it's not already in the report "
        "under a slightly different spelling (Section 2's master "
        "list, sorted by name). Then either (a) add a manual "
        "Participation row through the Python REPL using "
        "<i>store.upsert_participation(...)</i> with "
        "<i>extracted_by=\"manual:&lt;your initials&gt;\"</i>, or "
        "(b) add the name to <i>config/gazetteer.txt</i> so future "
        "automated runs recognize it, then rerun the relevant "
        "adapter. Approach (a) is faster for one-offs; (b) compounds "
        "across runs.", BODY))

    flow.append(Paragraph("<b>10. Update Found/Total.</b>", H3))
    flow.append(Paragraph(
        "If you learn the event's true total participant count "
        "(e.g., xTech announced \"400 applicants, 11 finalists\"), "
        "set it in <i>config/expected_participants.yaml</i> — the "
        "next report will show the corrected denominator without "
        "any code change.", BODY))


def _section_7(data: dict, flow: list) -> None:
    flow.append(_section_header("Section 7 — Raw evidence appendix"))
    by_company = data["by_company"]
    name_of = lambda cid: data["company_by_id"].get(cid, {}).get("name", cid)
    for cid in sorted(by_company, key=name_of):
        rows = sorted(
            by_company[cid],
            key=lambda r: _CONF_RANK.get(r["confidence"], 0),
            reverse=True,
        )
        flow.append(Paragraph(name_of(cid), H3))
        seen_urls: set[str] = set()
        for r in rows:
            clean = _clean_url(r["evidence_url"])
            if clean in seen_urls:
                continue
            seen_urls.add(clean)
            url_attr = _xml_escape(clean)
            url_display = _xml_escape(_display_url(clean))
            fixture_badge = (
                " <font color='#aa5500'><b>[FIXTURE — not a real "
                "URL]</b></font>"
                if _is_fixture_url(clean) else ""
            )
            line = (
                f"<b>{r['confidence']}</b> · <i>{r['role']}</i> · "
                f"<font color='#0a58ca'>{_xml_escape(r['extracted_by'])}</font> · "
                f"<link href=\"{url_attr}\" color=\"#0a58ca\">"
                f"<u>{url_display}</u></link>{fixture_badge}<br/>"
                f"<font color='#666'>"
                f"“{_xml_escape(r['evidence_excerpt'])}”</font>"
            )
            flow.append(Paragraph(line, BODY))


# ---- Main ----

def build(
    pdf_path: Path | None = None,
    *,
    since: date | None = None,
    skip_ota_column: bool = False,
    target_participants_only: bool = True,
    exclude_primes: bool = True,
) -> Path:
    """Render the full monthly report to PDF. Returns the output path.

    Section ordering is ATO-outreach-first by design:

      0. Outreach priority — the action list (warmest leads).
      1. Competitions discovered this window — context.
      2. Deduped participant master list — full coverage.
      3. Cross-event participants — hot signal (showed up >1x).
      4. Stealth startups — emerging unknowns.
      5. Compliance maturity — companies already authorized
         (de-prioritized but visible).
      —. (Ecosystem mapping intentionally omitted from the PDF.
          It lives in the markdown report for analysts who need
          supporter co-occurrence; the PDF stays outreach-focused.)
      7. Raw evidence appendix — audit trail.
    """
    since = since or (date.today() - timedelta(days=30))
    raw = _load_all()
    data = (
        _filter_data_for_target_audience(raw, exclude_primes=exclude_primes)
        if target_participants_only else raw
    )
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_path or (out_dir / f"report_{date.today().isoformat()}.pdf")

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(letter),  # wider for the master-list table
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"Defense Innovation Participants — {date.today()}",
    )

    flow: list = []
    flow.append(Paragraph(
        f"Defense Innovation Participants — generated {date.today()}",
        H1,
    ))
    flow.append(Paragraph(
        f"Window: events with dates_start ≥ {since}. "
        f"Companies: <b>{len(data['companies'])}</b>. "
        f"Participations: <b>{len(data['participations'])}</b>. "
        f"Events: <b>{len(data['events'])}</b>. "
        f"Review queue: <b>{len(data['review'])}</b> pending.",
        BODY,
    ))
    flow.append(Paragraph(
        "Filters applied: defense-relevance gate, announced-only "
        "suppression, sponsor / judge / mentor role exclusion, "
        f"{'prime / integrator exclusion' if exclude_primes else 'primes and integrators retained'}.",
        NOTE,
    ))
    flow.append(Spacer(1, 6))

    # PDF section ordering — outreach-focused. Master list (former
    # Section 2) and cross-event (former Section 3) live in the
    # XLSX export instead of the PDF; they crowded the PDF without
    # adding action-relevant content beyond what Outreach priority
    # already shows.
    _section_outreach_priority(data, flow)
    flow.append(PageBreak())
    _section_1(data, since, flow)
    _section_4(data, flow)
    _section_5(data, flow, skip_ota_column=skip_ota_column)
    flow.append(PageBreak())
    _section_7(data, flow)
    flow.append(PageBreak())
    _section_analyst_playbook(data, flow)
    flow.append(PageBreak())
    _section_legend(flow)

    doc.build(flow)
    return pdf_path


def main() -> None:
    path = build()
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

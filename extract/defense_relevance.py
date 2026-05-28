"""Defense-relevance gate.

A single source of truth for the question: "Is this event a
U.S. defense-industry-facing engagement that belongs in the
ATO-outreach report?"

Used in two places, by design (architectural rule 6 in CLAUDE.md):

  1. Ingestion time — `sources.discover._discover_event_from_article`
     calls `is_defense_relevant(...)` before creating an Event from
     a search hit. Articles that don't pass are dropped, never
     entering the store.

  2. Report time — `reports.build_markdown._filter_data_for_target_audience`
     runs `score_event(...)` over every event in the store. Events
     below the threshold are removed from sections 1-7, along with
     any participations attached to them.

Defense-in-depth: ingestion stops new noise; report-time cleans up
anything legacy that was admitted under older rules.

The scorer is intentionally rule-based (not ML). Rules are
auditable, tunable per source, and don't drift between runs.

Public API
----------
    is_defense_relevant(event, *, body_text="") -> bool
    score_event(event, *, body_text="") -> RelevanceResult

RelevanceResult is a NamedTuple of (passes, score, reasons). The
reasons list is structured so the report can show why an event
was kept or dropped — important for analyst review.
"""

from __future__ import annotations

import re
from typing import NamedTuple


# ---- Tunable threshold ----
#
# An event must reach this score to survive the gate. Tune up
# (stricter) if the report still has too much noise; tune down
# (more inclusive) if real defense events are being dropped.
#
# Calibration: every signal below is engineered so that a single
# strong signal (host in DEFENSE_HOSTS, or URL on .mil) clears the
# threshold on its own. A bare engagement keyword does not.

THRESHOLD = 0.5


# ---- Strong-positive signals ----
#
# Domains / hosts where the entire site is defense-program-relevant
# by construction. If the event's host or source_url touches one of
# these, the event clears the gate.

DEFENSE_HOSTS = frozenset({
    # Program orgs (.mil / .com presence)
    "afwerx.com", "afwerx", "diu.mil", "sofwerx.org", "sofwerx",
    "xtech.army.mil", "xtech", "darpa.mil", "navalx.navy.mil", "navalx",
    "dronedominance.mil",
    # Service / DoD-component public sites
    "army.mil", "navy.mil", "af.mil", "spaceforce.mil", "defense.gov",
    "marines.mil", "uscg.mil", "socom.mil", "ussocom",
    # SBIR / Sam / Challenge.gov
    "sbir.gov", "sam.gov", "challenge.gov",
    # Trade press (named-byline, defense-only)
    "defensescoop.com", "insidedefense.com", "breakingdefense.com",
    "c4isrnet.com", "defensenews.com",
})


# Engagement keywords that, combined with general defense framing,
# indicate a real participant-naming event. These match
# discover._ENGAGEMENT_KEYWORDS_RE but are split out here so the
# relevance gate can require both engagement AND defense framing
# rather than engagement alone.
_ENGAGEMENT_RE = re.compile(
    r"\b("
    r"hackathon|hack[- ]a[- ]thon|prize\s+challenge|"
    r"sprint|tech\s+sprint|technical\s+exchange|tem|"
    r"industry\s+day|proposers?\s+day|demo\s+day|"
    r"information\s+session|innovation\s+forum|innovation\s+challenge|"
    r"innovation\s+foundry|capability\s+engagement|assessment\s+event|"
    r"qualifying\s+event|prize\s+competition|pitch\s+(?:day|competition)|"
    r"sources\s+sought|special\s+notice|combined\s+synopsis|"
    r"showcase|crucible|ignitor|mashup|"
    r"workshop|symposium|expo|forum|summit"
    r")\b",
    re.I,
)


# Explicit defense vocabulary. Hitting any of these earns a strong
# positive — these terms are essentially never used in commercial
# hackathon marketing.

_DEFENSE_VOCAB_RE = re.compile(
    r"\b("
    # Services & components
    r"department\s+of\s+defense|"
    r"dod|d\.o\.d\.|"
    r"u\.?s\.?\s+army|u\.?s\.?\s+navy|u\.?s\.?\s+marine\s+corps|"
    r"u\.?s\.?\s+air\s+force|u\.?s\.?\s+space\s+force|u\.?s\.?\s+coast\s+guard|"
    r"usaf|usn|usmc|uscg|usaf|"
    r"socom|ussocom|jsoc|cybercom|stratcom|"
    r"indo[- ]pacific\s+command|northern\s+command|africa\s+command|"
    r"central\s+command|european\s+command|"
    # Program offices
    r"afrl|arl|onr|nrl|navwar|space\s+systems\s+command|ssc|"
    r"darpa|diu|afwerx|sofwerx|navalx|navsea|navair|"
    r"xtech|sbir|sttr|"
    # Mission language
    r"warfighter|warfighting|battlefield|kinetic|"
    r"national\s+security|homeland\s+(?:security|defense)|defense\s+innovation|"
    r"military|defence|defense[- ]industry|"
    # Acquisition / contracting
    r"other\s+transaction|ot\s+agreement|ota\b|"
    r"contract\s+award|prototype\s+(?:agreement|contract)|"
    # ISR / C2 / autonomy in a defense framing
    r"c-?uas|counter[- ]uas|c4isr|c5isr|isr\b|"
    r"unmanned\s+(?:aerial|aircraft|surface|ground|undersea|underwater)|"
    r"uav|uas|usv|ugv|uuv|drone\s+(?:swarm|interceptor|defense|warfare)|"
    # Compliance / authorization
    r"fedramp|disa|cmmc|"
    r"impact\s+level|\bil[2-6]\b|"
    r"authorization\s+to\s+operate|\bato\b|"
    # Allied / mission-coded
    r"nato|five\s+eyes|five[- ]eyes|"
    # Munitions / weapons
    r"missile|munition|hypersonic|loitering\s+munition"
    r")\b",
    re.I,
)


# ---- Strong-negative signals ----
#
# Title/source patterns that mark an event as commercial / dev-only
# / consumer / explainer. These exist because the engagement-keyword
# regex correctly identifies "hackathon" in titles like "Built with
# Opus 4.6: a Claude Code hackathon" — the regex did its job; we
# need an opposing force to say "yes, but not for this report."

_COMMERCIAL_NEG_RE = re.compile(
    r"\b("
    # Vertical/industry hackathons (non-defense)
    r"hospitality|fintech|finance\s+hackathon|"
    r"crypto|defi|nft|web3|blockchain\s+hackathon|"
    r"airline\s+price|consumer\s+health|health\s+&?\s+benefits|"
    r"art\s+(?:challenge|hackathon)|tech\s+alchathon|"
    # AI lab / dev hackathons (legitimate but not defense)
    r"claude\s+code|opus\s+\d|gpt-?\d\s+(?:startup\s+)?hackathon|"
    r"llama\s+\d|llama\s+lounge|llamacon|gemini\s+\d|"
    r"vibe\s+code|openai\s+codex|mistral\s+(?:ai\s+)?mcp|"
    r"nous\s+research|"
    r"google\s+i/o|ted\s*ai|tedai|"
    r"openenv|machina\s+hackathon|"
    r"synthetic\s+data\s+hackathon|"
    r"agentic\s+orchestration|"
    r"silicon\s+valley\s+ai\s+hub|"
    r"executorch|"
    # YC / demo-day adjacent
    r"demo\s+day\s+afterparty|demo\s+day\s+after[- ]?party|"
    r"\bw\d{2}\s+demo\s+day|\bx\d{2}\s+demo\s+day|"
    # Explainer / personal blog content
    r"my\s+first\s+hackathon|"
    r"what(?:'?s|\s+is)\s+a\s+hackathon|"
    r"unveiling\s+\w+(?:'s)?\s+inaugural|"
    r"introducing\s+\w+[—-]|"
    # Misc commercial PR
    r"siemens\s+make\s*it\s+real|"
    r"qhack|machinehack"
    r")\b",
    re.I,
)


# Source domains where signal-to-noise is poor for our use case.
# Being on one of these domains does NOT auto-disqualify (Army
# announces things on cerebralvalley.ai too), but it suppresses the
# domain-bonus.
_LOW_SNR_DOMAINS = frozenset({
    "cerebralvalley.ai", "medium.com", "substack.com",
    "hackernoon.com", "dev.to",
})


# Outreach-meta keywords. SOFWERX uses its events platform to
# advertise its own outreach (STEM showcases, school career fairs,
# welding workshops). These aren't defense engagements with
# participant companies, so we drop them.
_OUTREACH_NEG_RE = re.compile(
    r"\b("
    r"stem\s+(?:showcase|night|career\s+fair)?|"
    r"(?:high\s+school|career)\s+fair|"
    r"college\s+career\s+fair|"
    r"think\s+big\s+for\s+kids|"
    r"women\s+ambassadors?|"
    r"welding\s+workshop|"
    r"medical\s+wearables\s+expo|"
    r"synapse\s+summit|"
    r"pinecrest\s+academy|wharton\s+high\s+school"
    r")\b",
    re.I,
)


class RelevanceResult(NamedTuple):
    passes: bool
    score: float
    reasons: list[str]


def _host_or_url_domain(event: dict) -> str:
    """Return the most specific domain we can find for the event."""
    h = (event.get("host") or "").lower()
    # `host` can be "discovered:<domain>" or a free-form org name.
    if h.startswith("discovered:"):
        return h.split(":", 1)[1].strip()
    # Fall back to source_url
    url = (event.get("source_url") or "").lower()
    m = re.search(r"https?://([^/]+)/?", url)
    return m.group(1) if m else h


def score_event(event: dict, *, body_text: str = "") -> RelevanceResult:
    """Score an event for defense relevance.

    Parameters
    ----------
    event : dict
        An Event row (or dict with at least name/host/source_url).
    body_text : str, optional
        Article/body text if available — boosts confidence by
        letting us see defense vocabulary that wasn't in the title.

    Returns
    -------
    RelevanceResult
        (passes, score, reasons). `passes` is True iff
        `score >= THRESHOLD`.
    """
    reasons: list[str] = []
    score = 0.0

    name = (event.get("name") or "")
    domain = _host_or_url_domain(event)
    url = (event.get("source_url") or "").lower()
    blob = (name + " " + body_text + " " + (event.get("host") or "")).strip()

    # --- Strong positives ---

    # Allowlisted host: clears the bar on its own.
    matched_host = next((d for d in DEFENSE_HOSTS if d in domain or d in url), None)
    if matched_host:
        score += 1.0
        reasons.append(f"defense-host:{matched_host}")

    # `.mil` or `.gov` in source URL: clears the bar on its own.
    if re.search(r"\.mil(?:/|$|:)", url) or re.search(r"\.gov(?:/|$|:)", url):
        score += 1.0
        reasons.append("url:gov-or-mil")

    # Defense vocab in name/host/body: each distinct term adds
    # incremental score, capped to avoid runaway from articles
    # that just talk about "the defense industry" in passing.
    vocab_hits = sorted({m.lower() for m in _DEFENSE_VOCAB_RE.findall(blob)})
    if vocab_hits:
        score += min(0.8, 0.25 * len(vocab_hits))
        reasons.append("defense-vocab:" + ",".join(vocab_hits[:5]))

    # Engagement keyword present? Required signal — without it,
    # even a defense-vocab match is just news, not an event.
    if _ENGAGEMENT_RE.search(name):
        reasons.append("engagement-kw")
    else:
        # No engagement keyword in the *title* — heavy penalty.
        # Body-only engagement signal is too weak to create an
        # event from.
        score -= 0.5
        reasons.append("no-engagement-kw-in-title")

    # --- Strong negatives ---

    if _COMMERCIAL_NEG_RE.search(name):
        score -= 1.5
        reasons.append("commercial-deny")

    if _OUTREACH_NEG_RE.search(name):
        score -= 1.5
        reasons.append("outreach-deny")

    # Low-SNR domain WITHOUT any defense vocab? Treat as commercial.
    if domain in _LOW_SNR_DOMAINS and not vocab_hits and not matched_host:
        score -= 0.5
        reasons.append(f"low-snr-domain:{domain}")

    passes = score >= THRESHOLD
    return RelevanceResult(passes=passes, score=round(score, 3), reasons=reasons)


def is_defense_relevant(event: dict, *, body_text: str = "") -> bool:
    """Convenience wrapper — just the boolean."""
    return score_event(event, body_text=body_text).passes

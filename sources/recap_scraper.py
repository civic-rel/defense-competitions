"""Recap scraper.

For each tracked event, search a configured list of editorial and
program-official domains for articles published within a window
around the event date. Each article is then run through:

  1. NER (extract.ner.extract_mentions) → list of company mentions
  2. Company matcher (extract.company_match.match_or_queue)
     → existing or new Company in the store
  3. Confidence assignment (extract.confidence.assign_confidence)
  4. Participation row written with the article URL + excerpt

The prototype supports two input modes:
  - `from_url(url, event_id)` — fetch live, then process
  - `from_text(text, url, event_id)` — process pre-loaded text
    (useful for testing against fixtures)

In production, a search step would precede this to *discover* the
URLs. For the prototype we accept URLs directly; the discovery
loop can be added once the extraction-side is proven.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

from selectolax.parser import HTMLParser

from extract.company_match import match_or_queue
from extract.confidence import assign_confidence
from extract.ner import Mention, extract_mentions
from schema.participation import Participation, make_participation_id
from store import cache as store

log = logging.getLogger(__name__)


# Generic event-type words. When checking whether an article mentions
# an event, these tokens carry almost no signal — many unrelated
# articles contain them. We strip them from each event term and
# require at least one *distinctive* remaining token (e.g. "xTech",
# "DICE", "STO") to appear in the article body.
_EVENT_GENERIC_WORDS = frozenset({
    "the", "a", "an", "of", "and", "for", "in", "on", "at", "to",
    "hackathon", "challenge", "day", "days", "summit", "conference",
    "demo", "forum", "expo", "fair", "workshop", "session",
    "industry", "national", "security", "innovation", "competition",
    "awards", "award", "proposers", "proposer", "program", "programs",
    "annual", "phase",
})


def _event_distinctive_tokens(term: str) -> list[str]:
    """Return the tokens from `term` that carry recognizable signal.

    Strips generic event-vocabulary words ("hackathon", "day",
    "national", etc.) so the filter keys on brand/acronym tokens like
    "xTech" or "DICE". Returns the original term as a single-element
    list when no distinctive tokens remain."""
    tokens = re.findall(r"[A-Za-z0-9]+", term)
    distinctive = [
        t for t in tokens
        if t.lower() not in _EVENT_GENERIC_WORDS and len(t) >= 3
    ]
    return distinctive or [term]


def _article_mentions_event(text: str, event_terms: list[str]) -> bool:
    """True if the article text contains at least one distinctive
    token from any of the event's name/aliases."""
    text_low = text.lower()
    for term in event_terms:
        if not term:
            continue
        for tok in _event_distinctive_tokens(term):
            if tok.lower() in text_low:
                return True
    return False


def _html_to_text(html: str) -> str:
    """Strip HTML, return readable text."""
    doc = HTMLParser(html)
    # Drop scripts/styles + page-chrome containers so they don't leak
    # tokens like "Subscribe", "Tweet", etc. into the NER input.
    for tag in doc.css("script, style, nav, footer, aside, header, form, noscript"):
        tag.decompose()
    text = doc.text(separator=" ", strip=True)
    # Collapse multi-whitespace runs (element-join artifacts) so the
    # heuristic regex doesn't pull "Company A   Company B" into one match.
    return re.sub(r"\s+", " ", text)


def _classify_role(window: str) -> str:
    """Guess a role for a mention based on nearby words."""
    w = window.lower()
    if any(k in w for k in ["first place", "1st place", "took first", "won the"]):
        return "winner"
    if "winner" in w:
        return "winner"
    if "finalist" in w or "advanced to the final" in w:
        return "finalist"
    if "sponsor" in w or "co-host" in w or "partner" in w:
        return "sponsor"
    if "judge" in w or "panel" in w:
        return "judge"
    if "mentor" in w:
        return "mentor"
    if "demo" in w or "presented" in w:
        return "demoing"
    return "participant"


def _excerpt(text: str, start: int, end: int, padding: int = 80) -> str:
    """Take a window around the mention for storage as evidence."""
    a = max(0, start - padding)
    b = min(len(text), end + padding)
    out = text[a:b].strip()
    # Compress whitespace
    out = re.sub(r"\s+", " ", out)
    return out[:200]


def process_recap(
    *,
    text: str,
    evidence_url: str,
    event_id: str,
    extracted_by: str,
    has_named_author: bool = False,
    use_spacy: bool = False,
) -> list[dict]:
    """Run a single recap article end-to-end.

    Returns the list of Participation dicts written. Idempotent —
    re-running on the same article re-upserts the same rows.

    The caller (typically sources.discover) is responsible for
    deciding which `event_id` to attribute mentions to — either the
    seed event (if the article confirms it) or an event auto-
    discovered from the article's title.
    """
    mentions: list[Mention] = extract_mentions(text, use_spacy=use_spacy)
    log.info("[%s] %d mentions in %d chars", extracted_by, len(mentions), len(text))

    out: list[dict] = []
    for m in mentions:
        match = match_or_queue(
            m.canonical,
            event_id=event_id,
            evidence_url=evidence_url,
            evidence_excerpt=_excerpt(text, m.start, m.end),
        )
        if match.company is None:
            # Either queued for review or rejected — no participation
            continue

        excerpt = _excerpt(text, m.start, m.end)
        role = _classify_role(text[max(0, m.start-120):m.end+40])
        confidence = assign_confidence(
            evidence_url, excerpt,
            role_hint=role,
            has_named_author=has_named_author,
        )

        p = Participation(
            id=make_participation_id(match.company["id"], event_id, role, evidence_url),
            company_id=match.company["id"],
            event_id=event_id,
            role=role,
            confidence=confidence,
            evidence_url=evidence_url,
            evidence_excerpt=excerpt,
            extracted_by=extracted_by,
            extracted_at=datetime.utcnow(),
            notes=f"ner_source={m.source}",
        )
        store.upsert_participation(p.to_dict())
        out.append(p.to_dict())
    return out


def process_html(
    *,
    html: str,
    evidence_url: str,
    event_id: str,
    extracted_by: str,
    has_named_author: bool = False,
    use_spacy: bool = False,
) -> list[dict]:
    """Convenience: feed raw HTML, scraper handles stripping."""
    return process_recap(
        text=_html_to_text(html),
        evidence_url=evidence_url,
        event_id=event_id,
        extracted_by=extracted_by,
        has_named_author=has_named_author,
        use_spacy=use_spacy,
    )

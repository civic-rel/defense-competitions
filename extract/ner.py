"""Company-name extraction from recap text.

Two-pronged approach:
  1. Gazetteer match — fast and high-precision for known companies.
     This is the primary path. The gazetteer is config/gazetteer.txt.
  2. Generic ORG NER — catches names we haven't seen before. We use
     spaCy if installed; otherwise fall back to a simple heuristic
     (capitalized multi-word phrases ending in legal suffixes).

Outputs a list of (mention_text, char_start, char_end, source) tuples.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

GAZETTEER_PATH = Path(__file__).parent.parent / "config" / "gazetteer.txt"


class Mention(NamedTuple):
    text: str          # the literal text as it appeared
    canonical: str     # what we believe the canonical name is
    start: int
    end: int
    source: str        # 'gazetteer' or 'spacy' or 'heuristic'


# ---- Gazetteer ----

def _load_gazetteer(path: Path = GAZETTEER_PATH) -> dict[str, str]:
    """Load gazetteer into a dict of {variant_lower: canonical}."""
    out: dict[str, str] = {}
    if not path.exists():
        log.warning("gazetteer file missing: %s", path)
        return out
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        canonical = parts[0]
        out[canonical.lower()] = canonical
        for alias in parts[1:]:
            if alias:
                out[alias.lower()] = canonical
    return out


# Cache the loaded gazetteer at module load. Cheap; under 200 entries
# in the seed.
_GAZETTEER = _load_gazetteer()


def reload_gazetteer() -> None:
    """For tests / editing during a run."""
    global _GAZETTEER
    _GAZETTEER = _load_gazetteer()


def _gazetteer_match(text: str) -> list[Mention]:
    """Find every gazetteer entry that appears in the text.

    Whole-word match, case-insensitive. Longer variants checked
    first so "L3Harris Technologies" beats "L3Harris" at the same
    position.
    """
    mentions: list[Mention] = []
    # Sort by length descending so longer matches win
    variants = sorted(_GAZETTEER.keys(), key=len, reverse=True)
    text_lower = text.lower()
    # Track occupied [start, end) ranges so we don't double-match
    occupied: list[tuple[int, int]] = []

    def _overlaps(a: tuple[int, int]) -> bool:
        for b in occupied:
            if not (a[1] <= b[0] or a[0] >= b[1]):
                return True
        return False

    for variant in variants:
        # Whole-word boundary on each side
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(variant) + r"(?![A-Za-z0-9])", re.I)
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            if _overlaps(span):
                continue
            occupied.append(span)
            mentions.append(Mention(
                text=text[m.start():m.end()],
                canonical=_GAZETTEER[variant],
                start=m.start(),
                end=m.end(),
                source="gazetteer",
            ))
    return mentions


# ---- Heuristic fallback ----

# Capitalized 1-4 word phrase ending in a legal suffix.
# `AI` and `Defense` were dropped — too generic as suffixes; they
# pulled in noise like "The Defense", "Inside AI", "Floor AI". Real
# AI/Defense-suffixed companies belong in the gazetteer.
_HEURISTIC_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&]*(?:\s+[A-Z][A-Za-z0-9&]*){0,3})"
    r"(?:\s+(?:Inc|Inc\.|LLC|Corp|Corporation|Ltd|Limited|Industries|Technologies|Systems|Labs))\b"
)

# First-word stoplist for heuristic matches. If the candidate starts
# with any of these (case-insensitive), it's almost always nav/ad
# chrome, an article modifier, or a generic phrase — not a company.
_NOISE_FIRST_WORDS = frozenset({
    # Articles
    "the", "a", "an",
    # Page chrome / nav / ads
    "subscribe", "close", "advertisement", "advert", "tweet", "share",
    "sign", "login", "newsletter", "click", "read", "watch", "listen",
    "inside", "breaking", "newsletter", "media",
    # Generic headline modifiers / common false-positive prefixes
    "cyber", "missile", "national", "disruptive", "trusted", "basic",
    "floor", "golden", "ukrainian", "deputy", "department", "pm",
    "easley",
    # DoD-program adjectives / weapon-system categories — almost
    # always part of a program name, not a company name
    "defense", "naval", "joint", "strategic", "tactical", "counter",
    "anti", "electronic", "geospatial", "common", "integrated",
    "modular", "uncrewed", "unmanned", "loitering", "signals",
    "information", "combat", "surface", "submarine", "marine",
    "marines",
    # Government / agency / program-office prefixes — these surface
    # in solicitation text and trick the legal-suffix regex into
    # extracting e.g. "Federal Systems" or "Executive Office
    # Tactical Information Systems" as a company name.
    "federal", "executive", "program", "office", "diu", "darpa",
    "afwerx", "sofwerx", "navalx", "ussocom", "afrl", "arl", "onr",
    "nrl", "navsea", "navair", "ssc", "afcec",
    # Document section / requirement words — appear in solicitation
    # bodies; legal-suffix regex misreads them as company names
    # when followed by Systems/Technologies/etc.
    "additional", "proposal", "submission", "evaluation", "desired",
    "required", "background", "general", "draft", "appendix",
    "section", "attachment", "deliverable", "deliverables",
    "phase", "rolling", "step", "task", "task1", "task2", "task3",
    "amm",  # observed fragment in user's formal run
    # Political / slogans / generic adjectives that don't start
    # real company names
    "america", "american", "foreign", "domestic", "enterprise",
    "advanced", "emerging", "next", "future", "high", "small",
    "large", "light", "heavy", "more", "budget", "fuse", "program",
    # Generic 2-word "<Adjective> Systems/Technologies" noise
    "space", "human", "control", "innovative", "ask", "beyond",
    "minerals", "test",
    # Domain-tag words observed bleeding into the name field
    # (e.g. "Cleantech" appearing before a stealth company)
    "cleantech", "biotech", "fintech", "edtech", "agritech",
    # Legal-suffix words appearing as the first token of a candidate
    # almost always indicate element-boundary concatenation from a
    # previous company name (e.g. "Inc Dunedain Systems Exia Labs"
    # is two companies glommed together by HTML stripping).
    "inc", "inc.", "llc", "corp", "corp.", "corporation", "ltd",
    "ltd.", "limited", "labs",
})

# Legal-suffix tokens we also reject when they appear MID-candidate —
# strong signal that two companies got concatenated (e.g. "Acme Inc
# Other Industries"). Checked in _add() below.
_LEGAL_SUFFIX_WORDS = frozenset({
    "inc", "inc.", "llc", "corp", "corp.", "corporation",
    "ltd", "ltd.", "limited",
})

# Quoted code-name / team name — the stealth-startup case.
# Catches: "Project Ironhide", 'Team Nighthawk', "Stealth Falcon"
_CODENAME_RE = re.compile(
    r'["\u201C]([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){1,3})["\u201D]'
)
# Also catch the "operating as <Name>" / "called <Name>" patterns
_OPERATING_AS_RE = re.compile(
    r"(?:operating as|called|team named|under the name)\s+"
    r'["\u201C]?([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){1,3})["\u201D]?'
)


def _heuristic_match(text: str, occupied: list[tuple[int, int]]) -> list[Mention]:
    out: list[Mention] = []

    def _add(text_match: str, span_start: int, span_end: int) -> None:
        span = (span_start, span_end)
        if any(not (span[1] <= o[0] or span[0] >= o[1]) for o in occupied):
            return
        candidate = text_match.strip().rstrip(",.;:")
        if not candidate:
            return
        # Filter out things that are obviously not company names
        bad = ["Army", "Navy", "Force", "DoW", "DoD", "DARPA", "AFWERX", "SpaceWERX",
               "United States", "U.S.", "Department"]
        if any(w in candidate for w in bad):
            return
        # Reject when the leading token is page-chrome / a generic
        # headline modifier — these almost never start a real company.
        words = candidate.split()
        first_word = words[0].lower().rstrip(",.;:")
        if first_word in _NOISE_FIRST_WORDS:
            return
        # Reject when a legal-suffix word (Inc, LLC, Corp, ...) appears
        # mid-candidate — strong signal that two companies got glommed
        # together by HTML element-boundary concatenation.
        for w in words[1:-1]:
            if w.lower().rstrip(",.;:") in _LEGAL_SUFFIX_WORDS:
                return
        out.append(Mention(
            text=candidate, canonical=candidate,
            start=span_start, end=span_end,
            source="heuristic",
        ))
        occupied.append(span)

    # Legal-suffix matches (Inc, LLC, etc.)
    for m in _HEURISTIC_RE.finditer(text):
        _add(m.group(0), m.start(), m.end())

    # Quoted code-names — "Project Ironhide" style
    for m in _CODENAME_RE.finditer(text):
        _add(m.group(1), m.start(1), m.end(1))

    # "operating as / called / team named" patterns
    for m in _OPERATING_AS_RE.finditer(text):
        _add(m.group(1), m.start(1), m.end(1))

    return out


# ---- spaCy fallback (optional) ----

def _spacy_match(text: str, occupied: list[tuple[int, int]]) -> list[Mention]:
    try:
        import spacy
    except ImportError:
        return []
    try:
        nlp = _spacy_nlp()
    except OSError:
        log.warning("spaCy model 'en_core_web_sm' not installed; skipping")
        return []
    doc = nlp(text)
    out: list[Mention] = []
    for ent in doc.ents:
        if ent.label_ != "ORG":
            continue
        span = (ent.start_char, ent.end_char)
        if any(not (span[1] <= o[0] or span[0] >= o[1]) for o in occupied):
            continue
        out.append(Mention(
            text=ent.text, canonical=ent.text,
            start=ent.start_char, end=ent.end_char,
            source="spacy",
        ))
    return out


_NLP = None
def _spacy_nlp():
    global _NLP
    if _NLP is None:
        import spacy
        _NLP = spacy.load("en_core_web_sm")
    return _NLP


# ---- Public API ----

def extract_mentions(text: str, *, use_spacy: bool = False) -> list[Mention]:
    """Return all company mentions found in `text`.

    Gazetteer matches always run. spaCy is opt-in because the model
    download is ~50MB and not always wanted. Heuristic fallback
    runs by default and catches obvious Inc/LLC patterns.
    """
    mentions = _gazetteer_match(text)
    occupied = [(m.start, m.end) for m in mentions]
    if use_spacy:
        mentions.extend(_spacy_match(text, occupied))
        occupied = [(m.start, m.end) for m in mentions]
    mentions.extend(_heuristic_match(text, occupied))
    mentions.sort(key=lambda m: m.start)
    return mentions

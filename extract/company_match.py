"""Match an extracted mention to a Company in the store.

Decision logic:
  - Exact normalized-name match → return existing.
  - Fuzzy match ≥ 0.90 (ratio of normalized names) → return existing.
  - Fuzzy match 0.75–0.90 → return None and queue for review.
  - Below 0.75 → return None; caller may create a new Company.

The fuzzy threshold is conservative on purpose. The brief said to
"dedupe aggressively" — but for distinct startups it's better to
err toward "new company" than to collapse different orgs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from schema.company import Company, from_name, normalize_name
from store import cache as store

log = logging.getLogger(__name__)

EXACT_THRESHOLD = 1.0
AUTO_MERGE_THRESHOLD = 0.90
REVIEW_THRESHOLD = 0.82  # raised from 0.75 to filter "X Technologies" vs "Y Technologies"-class false positives


# ---- Non-company stoplist ----
#
# The final safety net before a Company is created or matched.
# Catches strings that look company-shaped to the NER / slug
# parsers but are actually document section headers, government
# agency names, requirement labels, or short capitalized fragments
# that leaked through. Applied at match_or_queue() entry — works
# regardless of which extractor produced the candidate.
#
# Examples from production runs that this catches:
#   "Federal Systems", "Impact Level", "Additional Information",
#   "Proposal Submission Requirements", "Desired Solution Attributes",
#   "DIU Solicitations", "Executive Office Tactical Information Systems"

_NON_COMPANY_EXACT = frozenset(s.lower() for s in {
    # Document section headers
    "additional information", "proposal submission requirements",
    "desired solution attributes", "evaluation criteria",
    "submission requirements", "solution attributes",
    "background information", "executive summary",
    "table of contents", "general information",
    "deliverables", "deliverable list", "scope of work",
    "statement of work", "period of performance",
    # Compliance terms
    "impact level", "impact levels", "security level",
    # Government / agency descriptors
    "federal systems", "diu solicitations", "diu solicitation",
    "executive office", "program office",
    "department of defense", "department of the army",
    "department of the navy", "department of the air force",
    "small business administration",
    # Domain tags that bleed into the name field
    "cleantech", "biotech", "fintech", "edtech", "agritech",
    # Misc fragments observed in user reports
    "amm", "additional", "requirements", "attributes",
    "information", "submission", "proposal",
})

_NON_COMPANY_PATTERNS = re.compile(
    r"""
    ^(?:
        # "Executive Office <anything>" → program office, not company
        executive\s+office\s+|
        # "Federal <anything>" + Systems/Group → agency descriptor
        federal\s+\w+(?:\s+\w+)?$|
        # Document-section header patterns
        (?:proposal|submission|evaluation|response|technical)\s+
            (?:requirements?|criteria|approach|narrative)$|
        (?:additional|background|general|technical)\s+
            (?:information|requirements?|approach)$|
        (?:desired|required|target)\s+
            (?:solution|capabilities|attributes|outcomes)$|
        # FY / fiscal-year + number
        (?:fy|fiscal\s*year)\s*\d+|
        # Phase / Task / Section + number
        (?:phase|task|section|appendix|attachment)\s*\d+|
        # "<agency> Solicitations?"
        \w+\s+solicitations?$|
        # Impact / Security / Maturity Level
        (?:impact|security|maturity)\s+levels?$|
        # 1-2 letter "company" — almost never a real company
        [A-Za-z]{1,2}$
    )
    """,
    re.X | re.IGNORECASE,
)


def _is_non_company(name: str) -> bool:
    """Return True if `name` is almost certainly NOT a real company.

    Used as the final filter before a Company is created or matched.
    Catches NER artifacts, document headers, agency descriptors, and
    short fragments.
    """
    if not name:
        return True
    s = name.strip()
    if len(s) < 3:
        return True
    norm = s.lower().strip()
    if norm in _NON_COMPANY_EXACT:
        return True
    if _NON_COMPANY_PATTERNS.search(norm):
        return True
    return False


@dataclass
class MatchResult:
    company: Optional[dict]      # the matched company row, or None
    similarity: float            # 0.0–1.0
    decision: str                # 'exact' | 'fuzzy_merge' | 'review' | 'new'


def _all_normalized_names() -> list[tuple[str, dict]]:
    """Build a list of (normalized_name, company_row) including aliases.

    Loaded fresh on each call. Cheap because companies table is small
    (thousands, not millions). Optimize later if needed.
    """
    out: list[tuple[str, dict]] = []
    for c in store.load_companies():
        out.append((c["normalized_name"], c))
        for alias in c.get("aliases", []):
            out.append((normalize_name(alias), c))
    return out


def match_or_queue(
    candidate_name: str,
    *,
    event_id: str | None = None,
    evidence_url: str | None = None,
    evidence_excerpt: str | None = None,
    auto_create_below_review: bool = True,
) -> MatchResult:
    """Try to match a candidate name. Queue or create as needed.

    If no acceptable match is found and `auto_create_below_review`
    is True, a new Company is inserted and returned (marked is_stealth
    if the name lacks a legal suffix and isn't in the gazetteer).
    """
    normalized = normalize_name(candidate_name)
    if not normalized:
        return MatchResult(None, 0.0, "new")

    # Safety-net stoplist: reject anything that looks like a document
    # section header, government agency descriptor, requirement label,
    # or short capitalized fragment leaked from NER / slug parsing.
    # The check runs against the *raw* candidate too because the
    # normalized form sometimes drops the discriminating word
    # (e.g. "Federal Systems" → "federal systems").
    if _is_non_company(candidate_name) or _is_non_company(normalized):
        log.info("[match] dropped non-company candidate: %r", candidate_name)
        return MatchResult(None, 0.0, "rejected_non_company")

    # Exact match first
    exact = store.find_company_by_normalized(normalized)
    if exact:
        return MatchResult(exact, 1.0, "exact")

    # Fuzzy match
    best_sim = 0.0
    best_row: dict | None = None
    for normed, row in _all_normalized_names():
        sim = SequenceMatcher(None, normalized, normed).ratio()
        if sim > best_sim:
            best_sim = sim
            best_row = row

    if best_sim >= AUTO_MERGE_THRESHOLD and best_row:
        return MatchResult(best_row, best_sim, "fuzzy_merge")

    if best_sim >= REVIEW_THRESHOLD and best_row:
        store.queue_for_review({
            "candidate_name": candidate_name,
            "nearest_match": best_row["id"],
            "similarity": best_sim,
            "event_id": event_id,
            "evidence_url": evidence_url,
            "evidence_excerpt": evidence_excerpt,
        })
        log.info("queued for review: %r ~ %r (sim=%.2f)",
                 candidate_name, best_row["name"], best_sim)
        return MatchResult(None, best_sim, "review")

    # No match. Create a new Company if asked.
    if auto_create_below_review:
        from extract.ner import _GAZETTEER
        is_stealth = (
            candidate_name.lower() not in _GAZETTEER
            and not any(
                candidate_name.endswith(suf)
                for suf in (" Inc", " Inc.", " LLC", " Corp", " Industries")
            )
        )
        new_company = from_name(candidate_name, is_stealth=is_stealth)
        store.upsert_company(new_company.to_dict())
        return MatchResult(new_company.to_dict(), best_sim, "new")

    return MatchResult(None, best_sim, "review")

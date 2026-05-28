"""Assign confidence to a Participation based on the source.

Rules (from CLAUDE.md / brief):

  confirmed:
    - Source is the program's own page (winner/finalist list).
    - Source is the event's official social account.
    - Source is a primary-source recap from the host organization.

  highly_likely:
    - Source is an editorial recap from a named-author byline
      (DefenseScoop, Inside Defense, Breaking Defense, etc.).
    - Source is a sponsor's portfolio blog post.
    - Source is a founder's own first-person post about the event.

  ecosystem_associated:
    - Photo coverage of the venue (no first-person attribution).
    - Third-party social with no first-person verification.
    - Co-occurrence inferred from indirect signal.

Role hints can also adjust confidence — e.g., "winner" mentioned on
an official page = confirmed regardless of how we got there.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Domains that produce confirmed-tier evidence
OFFICIAL_DOMAINS = {
    "xtech.army.mil", "army.mil", "diu.mil", "af.mil", "afwerx.com",
    "spacewerx.us", "navy.mil", "navalx.navy.mil", "sofwerx.org",
    "events.sofwerx.org", "defensewerx.org", "darpa.mil", "socom.mil",
    "challenge.gov", "sbir.gov", "ssbir.gov", "dod.mil", "cto.mil",
    "ac.cto.mil", "sam.gov", "cerebralvalley.ai",  # event host site
}

# Domains that produce highly_likely-tier editorial recaps
EDITORIAL_DOMAINS = {
    "defensescoop.com", "breakingdefense.com", "insidedefense.com",
    "defensenews.com", "c4isrnet.com", "nationaldefensemagazine.org",
    "federalnewsnetwork.com",
}

# Patterns that suggest the source is a founder/organizer first-person
# post rather than third-party reporting.
FIRST_PERSON_HINTS = re.compile(
    r"\b(I (?:am|was|built|presented|demo'd|demoed)|our team|we built|we presented|"
    r"my team|excited to share|proud to announce|registration is now open)\b",
    re.I,
)


def assign_confidence(
    evidence_url: str,
    evidence_excerpt: str,
    *,
    role_hint: str | None = None,
    has_named_author: bool = False,
) -> str:
    """Return one of: confirmed | highly_likely | ecosystem_associated."""
    domain = (urlparse(evidence_url).netloc or "").lower()
    # Strip www. for matching
    if domain.startswith("www."):
        domain = domain[4:]

    # Confirmed: official source pages
    if domain in OFFICIAL_DOMAINS:
        return "confirmed"

    # Confirmed if role explicitly says winner/finalist AND the
    # excerpt looks like a published-by-host announcement
    if role_hint in ("winner", "finalist") and "announce" in evidence_excerpt.lower():
        if domain in EDITORIAL_DOMAINS or has_named_author:
            return "highly_likely"
        return "ecosystem_associated"

    # Editorial recap with byline → highly_likely
    if domain in EDITORIAL_DOMAINS:
        return "highly_likely"

    # First-person organizer / founder post → highly_likely
    if FIRST_PERSON_HINTS.search(evidence_excerpt):
        return "highly_likely"

    # Sponsor portfolio post → highly_likely (these come from
    # portfolio_scraper config; we recognize them by hostname
    # presence in the portfolio config — for the prototype we
    # don't have that config wired, so just default below.)

    # Default
    return "ecosystem_associated"

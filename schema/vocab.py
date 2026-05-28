"""Controlled vocabularies for v2.

Every literal that downstream code might check belongs here. If a
new value is needed (e.g., a new role like "demoer"), add it once
and every adapter / report picks it up.
"""

from __future__ import annotations

# ---- Companies ----

COMPANY_TYPES = {
    "startup",
    "prime",
    "VC",
    "university",
    "government",
    "integrator",
    "national_lab",
    "unknown",
}

# Domain tags from the brief. Lowercase, hyphen-separated.
DOMAINS = {
    "ai", "autonomy", "cyber", "drones", "sensing", "logistics",
    "biotech", "space", "communications", "robotics", "manufacturing",
    "isr", "ew", "human-performance", "energy", "materials",
    "command-and-control", "edge-compute",
}

# ---- Participations ----

# The role a company played at a specific event.
ROLES = {
    "winner",
    "finalist",
    "participant",      # registered, attended
    "demoing",          # showed a product/demo
    "presenting",       # gave a talk / pitch (non-competing)
    "sponsor",          # financial or in-kind sponsor
    "judge",
    "investor",         # observed in investor capacity
    "mentor",
}

# Per the brief's methodology rules — three discrete levels.
# Every Participation row has exactly one. Reports group by this.
CONFIDENCE = {
    # Named on the official program page (winner/finalist list),
    # on the event's official social, or in a primary-source recap
    # from the host organization.
    "confirmed",
    # Named in an editorial recap (DefenseScoop, Inside Defense,
    # Breaking Defense, Cerebral Valley), in a sponsor's portfolio
    # post, or in a founder's own first-person post.
    "highly_likely",
    # Appears in venue photo coverage, in third-party social with
    # no first-person verification, or co-occurs with other
    # participants in a way that suggests presence without proof.
    "ecosystem_associated",
}

# ---- Compliance ----

# FedRAMP status vocabulary, aligned with the CR26 Consolidated
# Rules for 2026 (NTC-0004, published Feb 25 2026). The RFC-0020
# outcome retired the term "FedRAMP authorization" in favor of
# "FedRAMP Certification" / "FedRAMP Certified" and the proposed
# "FedRAMP Validated" split was dropped. "Ready" is retiring.
#
# These map to the marketplace lifecycle states. Internal vocab
# is lowercase / snake_case; display labels live in
# reports/build_pdf.py:_fedramp_display().
#
#   "certified"   — Continuous Monitoring (Rev5) /
#                   Persistent Validation (20x). Replaces "authorized".
#   "in_process"  — Agency Authorization In Process (Rev5) /
#                   Prioritized (20x) / Assessment by FedRAMP.
#   "preparation" — Provider in preparation phase. Closest CR26
#                   successor to the retiring "Ready" state.
#   "remediation" — Provider correcting a significant issue.
#   "none"        — Not listed on the FedRAMP marketplace. This is
#                   the default; "unknown" was removed because if
#                   enrichment ran and didn't find a record, that
#                   IS "none".
FEDRAMP_STATUS = {
    "certified",
    "in_process",
    "preparation",
    "remediation",
    "none",
}

# DoD IL vocabulary kept for schema continuity, but no public API
# publishes per-company IL status; the report does not render this
# column. See extract.defense_relevance / compliance.py and the
# legend's "DoD Impact Level" section.
DOD_IL_LEVELS = {"IL2", "IL4", "IL5", "IL6", "none", "unknown"}


def coerce_role(value: str) -> str:
    """Map common variations to canonical role names."""
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "1st_place": "winner", "first_place": "winner",
        "2nd_place": "winner", "second_place": "winner",
        "runner_up": "finalist", "top_finalist": "finalist",
        "attendee": "participant", "competitor": "participant",
        "team": "participant", "demoer": "demoing",
        "speaker": "presenting", "presenter": "presenting",
    }
    return aliases.get(v, v if v in ROLES else "participant")


def coerce_confidence(value: str) -> str:
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "high": "highly_likely",
        "medium": "ecosystem_associated",
        "low": "ecosystem_associated",
        "verified": "confirmed",
        "official": "confirmed",
    }
    return aliases.get(v, v if v in CONFIDENCE else "ecosystem_associated")

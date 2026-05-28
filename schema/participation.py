"""Participation — the atomic evidence unit.

One row per assertion. To claim "company X was at event Y in role R",
the system requires a Participation with:
  - the source URL that supports the assertion
  - a short evidence excerpt from that source
  - a confidence level
  - which adapter or manual step created it

Section 7 of the monthly report is just a SELECT over this table.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from schema.vocab import (
    CONFIDENCE,
    ROLES,
    coerce_confidence,
    coerce_role,
)


@dataclass
class Participation:
    id: str
    company_id: str
    event_id: str
    role: str
    confidence: str
    evidence_url: str
    evidence_excerpt: str          # ≤200 chars
    extracted_by: str              # adapter name e.g. "recap_scraper:defensescoop"
                                   # or "manual:<analyst>"
    extracted_at: datetime = field(default_factory=datetime.utcnow)
    notes: str = ""

    def __post_init__(self) -> None:
        self.role = coerce_role(self.role)
        self.confidence = coerce_confidence(self.confidence)
        if len(self.evidence_excerpt) > 200:
            self.evidence_excerpt = self.evidence_excerpt[:197] + "..."

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["extracted_at"] = self.extracted_at.isoformat()
        return d


def make_participation_id(company_id: str, event_id: str, role: str, evidence_url: str) -> str:
    """ID = hash of the tuple. Same (company, event, role, evidence)
    upserts; different evidence URLs for the same triple stay as
    separate rows so the appendix shows all sources."""
    key = f"{company_id}|{event_id}|{coerce_role(role)}|{evidence_url}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

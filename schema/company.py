"""Company — first-class entity in v2.

One row per real-world organization. The `aliases` field carries
alternative names so the matcher can collapse "Anduril", "Anduril
Industries", and "Anduril Industries, Inc." to one record.

Compliance and funding fields are populated by enrichment jobs that
run on weekly (Crunchbase) or monthly (FedRAMP, USASpending)
cadences. They're allowed to be empty in the hot path.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from schema.vocab import COMPANY_TYPES, DOMAINS, DOD_IL_LEVELS, FEDRAMP_STATUS


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
_LEGAL_SUFFIXES = (
    " inc", " inc.", " incorporated",
    " llc", " l.l.c.",
    " ltd", " limited",
    " corp", " corporation",
    " co", " company",
    " plc", " gmbh", " ag", " sa", " bv",
)


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, drop legal suffixes, collapse ws.

    Used everywhere a name appears in a key — IDs, alias lookup,
    fuzzy matching. Two inputs that produce the same normalized
    form are treated as the same company until proven otherwise.
    """
    n = (name or "").lower()
    n = _PUNCT_RE.sub(" ", n)
    n = _WS_RE.sub(" ", n).strip()
    # Strip a single trailing legal suffix
    for suf in _LEGAL_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
            break
    return n


def make_company_id(name: str) -> str:
    return hashlib.sha1(normalize_name(name).encode("utf-8")).hexdigest()[:16]


@dataclass
class Company:
    id: str
    name: str                       # canonical display name
    normalized_name: str
    aliases: list[str] = field(default_factory=list)
    type: str = "unknown"
    domains: list[str] = field(default_factory=list)

    crunchbase_url: str = ""
    linkedin_url: str = ""
    website: str = ""
    parent_company_id: str | None = None

    # Compliance (monthly refresh)
    fedramp_status: str = "unknown"
    dod_il_level: str = "unknown"
    ota_signals: list[dict] = field(default_factory=list)

    # Funding (weekly refresh)
    notable_investors: list[str] = field(default_factory=list)
    total_funding_usd: float | None = None
    last_round: dict | None = None

    # Lifecycle
    is_stealth: bool = False
    first_seen: date = field(default_factory=date.today)
    last_seen: date = field(default_factory=date.today)

    def __post_init__(self) -> None:
        if self.type not in COMPANY_TYPES:
            self.type = "unknown"
        if self.fedramp_status not in FEDRAMP_STATUS:
            self.fedramp_status = "unknown"
        if self.dod_il_level not in DOD_IL_LEVELS:
            self.dod_il_level = "unknown"
        self.domains = [d for d in self.domains if d in DOMAINS]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_seen"] = self.first_seen.isoformat()
        d["last_seen"] = self.last_seen.isoformat()
        return d


def from_name(
    name: str,
    *,
    type: str = "unknown",
    aliases: list[str] | None = None,
    is_stealth: bool = False,
    today: date | None = None,
) -> Company:
    """Convenience factory used by extractors when they discover a
    new candidate company name in recap text."""
    today = today or date.today()
    return Company(
        id=make_company_id(name),
        name=name.strip(),
        normalized_name=normalize_name(name),
        aliases=aliases or [],
        type=type,
        is_stealth=is_stealth,
        first_seen=today,
        last_seen=today,
    )

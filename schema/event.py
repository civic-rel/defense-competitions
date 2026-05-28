"""Event — minimal carry-over from v1.

The v1 normalize.py has the full version with priority logic. For
the recap prototype we only need enough to identify an event when
running the scraper.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass
class Event:
    id: str
    name: str                       # canonical name
    aliases: list[str] = field(default_factory=list)
    host: str = ""
    dates_start: date = field(default_factory=date.today)
    dates_end: date | None = None
    location: str = ""
    source_url: str = ""
    # Confirmed total number of participants, when the source
    # publishes one (e.g., xTech competition pages say "11 finalists",
    # DIU "Four Companies Selected …"). None means "we don't know
    # the true total"; the report renders "9 / ?" in that case.
    # Set by adapters at ingestion or via a manual override in
    # config/expected_participants.yaml.
    expected_participants: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dates_start"] = self.dates_start.isoformat()
        d["dates_end"] = self.dates_end.isoformat() if self.dates_end else None
        return d


def make_event_id(host: str, dates_start: date, name: str) -> str:
    key = f"{host.strip().lower()}|{dates_start.isoformat()}|{name.strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

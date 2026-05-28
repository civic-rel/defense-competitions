"""Small shared helpers used by multiple source adapters."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)


def parse_date_loose(value: str | date | datetime | None) -> Optional[date]:
    """Forgiving date parser. Returns None for unparseable input."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        from dateutil import parser as dateparser
        return dateparser.parse(str(value), fuzzy=True).date()
    except Exception as exc:
        log.debug("parse_date_loose %r: %s", value, exc)
        return None

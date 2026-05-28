"""CLI: discover participants for every recent event in the store.

Usage:
    python -m pipeline.discover_all                # last 30 days
    python -m pipeline.discover_all --since 60d    # last 60 days
    python -m pipeline.discover_all --only <id>    # one event
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, timedelta

from sources.discover import discover_all


def _parse_since(s: str) -> date:
    m = re.match(r"(\d+)d$", s)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected '<N>d' or YYYY-MM-DD, got {s!r}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover participants for tracked events.")
    parser.add_argument("--since", type=_parse_since, default=None,
                        help="Look back N days (e.g. '30d') or ISO date.")
    parser.add_argument("--only", type=str, default=None,
                        help="Single event_id to discover.")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    summaries = discover_all(only_event_id=args.only, since=args.since)
    total_parts = sum(s.get("participations_written", 0) for s in summaries)
    print(f"\ndiscover_all: {len(summaries)} events processed, "
          f"{total_parts} participations written")
    return 0


if __name__ == "__main__":
    sys.exit(main())

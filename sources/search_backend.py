"""Search backend abstraction.

The article-discovery loop needs to ask "given event name X and a
window of dates, find candidate recap URLs from these domains."
That's a web search. We abstract over the search engine so:

  - In tests, an OfflineSearchBackend returns pre-canned results.
  - In production, BraveSearchBackend hits the Brave Search API.
  - Other backends (SerpAPI, Bing, Vertex AI) can be added as
    separate classes implementing the same `search()` contract.

Brave is the default for production because (1) its API is
permissive for OSINT use, (2) pricing scales linearly, and
(3) it's not subject to the Custom Search JSON API sunset.

Set SEARCH_BACKEND=brave + BRAVE_API_KEY=<key> in .env to enable
the live backend. Otherwise the OfflineSearchBackend is used.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """One organic search hit."""
    title: str
    url: str
    snippet: str
    published_at: Optional[date] = None
    rank: int = 0


class SearchBackend(ABC):
    """Search-engine adapter contract."""

    name: str = "base"

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        site: Optional[str] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Return up to `limit` organic results.

        If `site` is given, restrict to that domain. Implementations
        may either pass `site:` in the query or use a structured
        parameter — caller doesn't care which.
        """
        raise NotImplementedError


# ---- Brave Search API ----

class BraveSearchBackend(SearchBackend):
    """Brave Search API client.

    Docs: https://api.search.brave.com/app/documentation
    Free tier: 1 query/sec, 2000 queries/month.
    """
    name = "brave"
    API_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        self.api_key = api_key or os.getenv("BRAVE_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError(
                "BRAVE_API_KEY not set. Get one at https://api.search.brave.com "
                "or fall back to SEARCH_BACKEND=offline."
            )
        self.client = httpx.Client(timeout=timeout)
        self._last_request_at = 0.0

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def search(
        self,
        query: str,
        *,
        site: Optional[str] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        if site:
            query = f"{query} site:{site}"

        # Respect the 1 qps free-tier limit
        elapsed = time.time() - self._last_request_at
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        log.info("[brave] %s", query)
        r = self.client.get(
            self.API_URL,
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
            },
            params={"q": query, "count": min(limit, 20)},
        )
        self._last_request_at = time.time()
        r.raise_for_status()
        data = r.json()
        results: list[SearchResult] = []
        for rank, item in enumerate(data.get("web", {}).get("results", []), start=1):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                published_at=_parse_brave_date(item.get("age") or item.get("page_age")),
                rank=rank,
            ))
            if len(results) >= limit:
                break
        return results


def _parse_brave_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    # Brave returns ISO-ish strings; be forgiving
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


# ---- Offline backend for tests / demo ----

class OfflineSearchBackend(SearchBackend):
    """Returns canned results from a JSON fixture.

    Fixture format:
      { "<query>": [
          {"title": "...", "url": "...", "snippet": "...",
           "published_at": "2026-05-04"},
          ...
        ],
        ...
      }

    Queries are matched case-insensitively. If a query isn't in
    the fixture, returns []. The fixture path is configurable so
    tests can load different scenarios.
    """
    name = "offline"

    def __init__(self, fixture: dict[str, list[dict]] | None = None):
        self._fixture = fixture or {}

    @classmethod
    def from_file(cls, path: str) -> "OfflineSearchBackend":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def search(
        self,
        query: str,
        *,
        site: Optional[str] = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        # Build the same "<query> site:<domain>" key the live backend uses
        full_query = f"{query} site:{site}" if site else query
        key = _normalize_query(full_query)
        rows = self._fixture.get(key, [])
        if not rows:
            # Try without site restriction — useful when fixture is loosely keyed
            rows = self._fixture.get(_normalize_query(query), [])
        results: list[SearchResult] = []
        for rank, item in enumerate(rows[:limit], start=1):
            pub = item.get("published_at")
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
                published_at=date.fromisoformat(pub) if pub else None,
                rank=rank,
            ))
        return results


def _normalize_query(q: str) -> str:
    return " ".join(q.lower().split())


# ---- Factory ----

def get_backend() -> SearchBackend:
    """Return the configured backend per the SEARCH_BACKEND env var.

    Default: offline. Set SEARCH_BACKEND=brave for live.
    """
    name = os.getenv("SEARCH_BACKEND", "offline").lower()
    if name == "brave":
        return BraveSearchBackend()
    if name == "offline":
        # Look for a fixture path, otherwise return empty backend
        path = os.getenv("OFFLINE_SEARCH_FIXTURE")
        if path and os.path.exists(path):
            return OfflineSearchBackend.from_file(path)
        return OfflineSearchBackend()
    raise ValueError(f"unknown SEARCH_BACKEND: {name}")

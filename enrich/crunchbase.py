"""Crunchbase enrichment.

Looks up each company in the store and updates these fields:

  - total_funding_usd
  - notable_investors (list of names)
  - last_round (dict with type/amount/date)
  - type (refines 'unknown' → 'startup' / 'prime' / 'integrator' / etc.
    based on company size and category signals)
  - domains (maps Crunchbase categories to our DOMAINS vocab)
  - crunchbase_url, website (when missing)

Runs at the WEEKLY cadence (per the v2 plan). A single full run
hits the API once per stored company so the cost is bounded by
the store size, not the number of recap articles.

Two backends:
  - CrunchbaseAPI (live) — uses /v4/data/searches/organizations
    + /v4/entities/organizations/{permalink} with X-cb-user-key.
  - OfflineCrunchbase — returns canned results from a JSON fixture
    for tests and demo. Selected when CRUNCHBASE_BACKEND=offline.

API docs: https://data.crunchbase.com/docs
Pricing: paid only as of 2026. Set CRUNCHBASE_API_KEY in .env.
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from schema.company import normalize_name
from schema.vocab import COMPANY_TYPES, DOMAINS
from store import cache as store

log = logging.getLogger(__name__)


# ---- Result type ----

@dataclass
class CrunchbaseRecord:
    """Subset of Crunchbase data we actually use."""
    permalink: str
    name: str
    short_description: str
    website: str
    total_funding_usd: Optional[float]
    last_round_type: Optional[str]
    last_round_date: Optional[str]
    last_round_amount_usd: Optional[float]
    investors: list[str]
    categories: list[str]            # raw Crunchbase categories
    employee_count: Optional[str]    # e.g., "11-50"


# ---- Backend interface ----

class CrunchbaseBackend(ABC):
    name: str = "base"

    @abstractmethod
    def lookup(self, name: str) -> Optional[CrunchbaseRecord]:
        """Best-effort lookup by company name. Returns None if not found."""
        raise NotImplementedError


# ---- Live backend ----

class CrunchbaseAPI(CrunchbaseBackend):
    """Live Crunchbase v4 client.

    Strategy: hit /searches/organizations with a name predicate,
    pick the top result, then fetch /entities/organizations/{permalink}
    with the funding-rounds card for the detail.
    """
    name = "crunchbase_live"
    SEARCH_URL = "https://api.crunchbase.com/api/v4/searches/organizations"
    ENTITY_URL = "https://api.crunchbase.com/api/v4/entities/organizations"
    RATE_LIMIT_SLEEP = 0.4   # ~2.5 qps; well under enterprise limits

    def __init__(self, api_key: Optional[str] = None, timeout: float = 30.0):
        self.api_key = api_key or os.getenv("CRUNCHBASE_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError(
                "CRUNCHBASE_API_KEY not set. Either set it in .env or "
                "use CRUNCHBASE_BACKEND=offline."
            )
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "X-cb-user-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _post(self, url: str, body: dict) -> dict:
        time.sleep(self.RATE_LIMIT_SLEEP)
        r = self.client.post(url, json=body)
        r.raise_for_status()
        return r.json()

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _get(self, url: str, params: dict | None = None) -> dict:
        time.sleep(self.RATE_LIMIT_SLEEP)
        r = self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def lookup(self, name: str) -> Optional[CrunchbaseRecord]:
        # 1. Search for the org by name
        search_body = {
            "field_ids": [
                "identifier", "short_description", "website",
                "funding_total", "categories",
                "num_employees_enum",
            ],
            "query": [
                {"type": "predicate", "field_id": "identifier",
                 "operator_id": "contains", "values": [name]},
            ],
            "limit": 5,
        }
        try:
            data = self._post(self.SEARCH_URL, search_body)
        except httpx.HTTPError as exc:
            log.error("[crunchbase] search %r failed: %s", name, exc)
            return None

        entities = data.get("entities") or []
        if not entities:
            return None

        # Pick the best match by normalized-name equality (or first if no
        # exact match — caller should already have filtered)
        target_norm = normalize_name(name)
        best = entities[0]
        for e in entities:
            ident = (e.get("properties", {}).get("identifier") or {})
            cand_name = ident.get("value", "")
            if normalize_name(cand_name) == target_norm:
                best = e
                break

        props = best.get("properties", {}) or {}
        permalink = (props.get("identifier") or {}).get("permalink", "")
        if not permalink:
            return None

        # 2. Fetch the entity detail with funding rounds + investors
        detail_url = f"{self.ENTITY_URL}/{permalink}"
        try:
            detail = self._get(detail_url, {
                "card_ids": "raised_funding_rounds,investors",
                "field_ids": (
                    "identifier,short_description,website,"
                    "funding_total,categories,num_employees_enum"
                ),
            })
        except httpx.HTTPError as exc:
            log.error("[crunchbase] detail %r failed: %s", permalink, exc)
            detail = {}

        return _build_record_from_response(props, permalink, detail)


def _build_record_from_response(
    summary_props: dict,
    permalink: str,
    detail: dict,
) -> CrunchbaseRecord:
    cards = detail.get("cards", {}) or {}
    detail_props = (detail.get("properties") or summary_props or {})

    funding_total = (detail_props.get("funding_total") or {}).get("value_usd")
    categories = [
        c.get("value", "") for c in (detail_props.get("categories") or [])
    ]
    employees = detail_props.get("num_employees_enum")
    short_desc = detail_props.get("short_description", "") or ""
    website = detail_props.get("website", "") or ""

    # Funding rounds — pick most recent
    rounds = cards.get("raised_funding_rounds") or []
    rounds_sorted = sorted(
        rounds,
        key=lambda r: r.get("announced_on", "") or "",
        reverse=True,
    )
    last_type = last_date = None
    last_amount: Optional[float] = None
    if rounds_sorted:
        top = rounds_sorted[0]
        last_type = (top.get("investment_type") or
                     (top.get("identifier") or {}).get("value"))
        last_date = top.get("announced_on")
        last_amount = (top.get("money_raised") or {}).get("value_usd")

    # Investors — flatten investor card
    investor_card = cards.get("investors") or []
    investors: list[str] = []
    for inv in investor_card:
        name = (inv.get("identifier") or {}).get("value", "")
        if name and name not in investors:
            investors.append(name)

    return CrunchbaseRecord(
        permalink=permalink,
        name=(detail_props.get("identifier") or summary_props.get("identifier") or {}).get("value", ""),
        short_description=short_desc,
        website=website,
        total_funding_usd=funding_total,
        last_round_type=last_type,
        last_round_date=last_date,
        last_round_amount_usd=last_amount,
        investors=investors,
        categories=categories,
        employee_count=employees,
    )


# ---- Offline backend for tests / demo ----

class OfflineCrunchbase(CrunchbaseBackend):
    """Returns canned responses from a JSON fixture.

    Fixture format:
      { "<normalized_name>": {
          "permalink": "...", "name": "...", "website": "...",
          "total_funding_usd": 1000000, "last_round_type": "Series A",
          "last_round_date": "2025-06-01", "last_round_amount_usd": 5000000,
          "investors": ["Investor X", "Investor Y"],
          "categories": ["Defense", "AI"], "employee_count": "11-50",
          "short_description": "..."
        }, ...
      }
    """
    name = "crunchbase_offline"

    def __init__(self, fixture: dict[str, dict] | None = None):
        self._fixture = fixture or {}

    @classmethod
    def from_file(cls, path: str | Path) -> "OfflineCrunchbase":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def lookup(self, name: str) -> Optional[CrunchbaseRecord]:
        key = normalize_name(name)
        data = self._fixture.get(key)
        if not data:
            return None
        return CrunchbaseRecord(
            permalink=data.get("permalink", key.replace(" ", "-")),
            name=data.get("name", name),
            short_description=data.get("short_description", ""),
            website=data.get("website", ""),
            total_funding_usd=data.get("total_funding_usd"),
            last_round_type=data.get("last_round_type"),
            last_round_date=data.get("last_round_date"),
            last_round_amount_usd=data.get("last_round_amount_usd"),
            investors=data.get("investors", []),
            categories=data.get("categories", []),
            employee_count=data.get("employee_count"),
        )


def get_backend() -> CrunchbaseBackend:
    name = os.getenv("CRUNCHBASE_BACKEND", "offline").lower()
    if name == "live":
        return CrunchbaseAPI()
    if name == "offline":
        path = os.getenv("OFFLINE_CRUNCHBASE_FIXTURE")
        if path and os.path.exists(path):
            return OfflineCrunchbase.from_file(path)
        return OfflineCrunchbase()
    raise ValueError(f"unknown CRUNCHBASE_BACKEND: {name}")


# ---- Category → domain mapping ----

# Crunchbase category strings are not perfectly aligned with our
# DOMAINS vocab, so we apply a substring-based mapping. Multiple
# Crunchbase categories may map to the same domain.
_CATEGORY_TO_DOMAIN = [
    ("artificial intelligence", "ai"),
    ("machine learning", "ai"),
    ("autonomous", "autonomy"),
    ("robotics", "robotics"),
    ("drone", "drones"),
    ("uav", "drones"),
    ("cyber", "cyber"),
    ("information security", "cyber"),
    ("space", "space"),
    ("satellite", "space"),
    ("aerospace", "space"),
    ("logistics", "logistics"),
    ("supply chain", "logistics"),
    ("manufacturing", "manufacturing"),
    ("hardware", "manufacturing"),
    ("sensor", "sensing"),
    ("biotech", "biotech"),
    ("communications", "communications"),
    ("wireless", "communications"),
    ("electronic warfare", "ew"),
    ("intelligence, surveillance, and reconnaissance", "isr"),
    ("isr", "isr"),
    ("command and control", "command-and-control"),
    ("c2", "command-and-control"),
    ("edge", "edge-compute"),
]


def _domains_from_categories(categories: list[str]) -> list[str]:
    out: list[str] = []
    blob = " ".join(c.lower() for c in categories)
    for needle, domain in _CATEGORY_TO_DOMAIN:
        if needle in blob and domain not in out:
            out.append(domain)
    return [d for d in out if d in DOMAINS]


# ---- Type inference ----

def _infer_type(record: CrunchbaseRecord) -> str:
    """Refine company type from Crunchbase signals.

    Tier 1 primes (Lockheed, Northrop, etc.) are 10000+; integrators
    (Leidos, Booz Allen) are similar. Most defense-tech startups are
    1-1000. VCs aren't in this enrichment path — they don't appear
    as participants — so we don't have to distinguish them here.
    """
    # Categories that scream "prime/integrator"
    blob = " ".join(c.lower() for c in record.categories)
    if "government" in blob or "defense" in blob:
        if record.employee_count in ("10001+", "5001-10000"):
            return "prime"
        if record.employee_count in ("1001-5000", "501-1000"):
            return "integrator"
    if record.employee_count and record.employee_count.startswith(
        ("1-10", "11-50", "51-100", "101-250")
    ):
        return "startup"
    return "unknown"


# ---- The enrichment pass ----

def enrich_all(*, backend: CrunchbaseBackend | None = None,
               only_unknown_type: bool = False,
               max_companies: int | None = None) -> dict:
    """Iterate every Company in the store and enrich from Crunchbase.

    Args:
        backend: explicit backend; defaults to get_backend().
        only_unknown_type: if True, skip companies whose `type` is
          already set to something other than 'unknown'. Useful for
          incremental runs.
        max_companies: cap the run length, helpful for API-quota-
          constrained backfills.

    Returns a summary dict.
    """
    backend = backend or get_backend()
    companies = store.load_companies()
    if only_unknown_type:
        companies = [c for c in companies if c.get("type") == "unknown"]
    if max_companies:
        companies = companies[:max_companies]

    summary = {
        "considered": len(companies),
        "matched": 0,
        "no_match": 0,
        "errors": 0,
        "fields_updated": 0,
    }

    for c in companies:
        try:
            record = backend.lookup(c["name"])
        except Exception as exc:  # broad on purpose — one bad lookup shouldn't abort the run
            log.exception("crunchbase lookup %r failed", c["name"])
            summary["errors"] += 1
            continue

        if record is None:
            summary["no_match"] += 1
            log.debug("no crunchbase match for %r", c["name"])
            continue

        summary["matched"] += 1
        updated = _apply_record(c, record)
        if updated:
            store.upsert_company(c)
            summary["fields_updated"] += updated
    log.info("[crunchbase] %s", summary)
    return summary


def _apply_record(company: dict, record: CrunchbaseRecord) -> int:
    """Mutate `company` in place from `record`. Returns the number of
    fields actually changed."""
    changed = 0

    def _set(key: str, value: Any) -> None:
        nonlocal changed
        if value in (None, "", [], {}):
            return
        if company.get(key) == value:
            return
        company[key] = value
        changed += 1

    _set("total_funding_usd", record.total_funding_usd)
    _set("notable_investors", record.investors)
    if record.last_round_type or record.last_round_date or record.last_round_amount_usd:
        _set("last_round", {
            "type": record.last_round_type,
            "date": record.last_round_date,
            "amount_usd": record.last_round_amount_usd,
        })
    if record.website:
        _set("website", record.website)
    if record.permalink:
        _set("crunchbase_url", f"https://www.crunchbase.com/organization/{record.permalink}")
    # Domains: extend, don't replace (recap-derived domains stay)
    new_domains = _domains_from_categories(record.categories)
    merged = list(dict.fromkeys((company.get("domains") or []) + new_domains))
    if merged != company.get("domains"):
        company["domains"] = merged
        changed += 1
    # Type: only promote 'unknown' → something concrete
    if company.get("type") == "unknown":
        new_type = _infer_type(record)
        if new_type != "unknown":
            company["type"] = new_type
            changed += 1

    return changed

"""Compliance enrichment.

Runs at the MONTHLY cadence (per the v2 plan). Three signals per
company:

  1. FedRAMP status (Authorized / In Process / Ready / none)
       Source: fedramp.gov/marketplace JSON export
       Filtered by product/vendor name match against our store

  2. DoD Impact Level (IL2 / IL4 / IL5 / IL6 / none)
       Source: text mined from FedRAMP entries + vendor websites
       (no single authoritative public API). We look for the IL
       string in the FedRAMP product description.

  3. OT / contract signals (yes / no)
       Source: USASpending.gov contracts API filtered to OTA
       and prototype-OT award types for our company name.

Each signal is computed independently. Missing data stays
'unknown' — we don't infer. The brief's Section 5 reads directly
from these fields.

Note: this is the place where many companies will return no data,
because they're either too early-stage to have FedRAMP / IL
authorization, or because they sell on-prem rather than cloud.
That's not a defect — it's the signal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from schema.company import normalize_name
from schema.vocab import DOD_IL_LEVELS, FEDRAMP_STATUS
from store import cache as store

log = logging.getLogger(__name__)


# ============================================================
# FedRAMP — backend abstraction (offline fixture + live)
# ============================================================

@dataclass
class FedrampRecord:
    csp_name: str
    csp_normalized: str
    product_name: str
    status: str               # 'authorized' | 'in_process' | 'ready' | 'none'
    impact_level: str         # 'high' | 'moderate' | 'low' | 'li-saas'
    dod_il_level: str         # 'IL2' | 'IL4' | 'IL5' | 'IL6' | 'none'
    authorization_date: Optional[str]


class FedrampBackend(ABC):
    name: str = "base"

    @abstractmethod
    def all_records(self) -> list[FedrampRecord]:
        raise NotImplementedError


# The official marketplace.fedramp.gov/api/v1/products endpoint has
# been flaky / schema-shifting. FedRAMP also publishes a stable JSON
# export on GitHub (795+ products, versioned, no auth). Override with
# FEDRAMP_PRODUCTS_URL=... if you want to point elsewhere.
FEDRAMP_PRODUCTS_URL = os.getenv(
    "FEDRAMP_PRODUCTS_URL",
    "https://raw.githubusercontent.com/FedRAMP/marketplace-fedramp-gov-data/refs/heads/main/fedramp-products.json",
)

_IL_RE = re.compile(
    r"(?:DoD\s*)?(?:Impact\s+Level\s*[-]?\s*|IL\s*[-]?\s*)([2-6])",
    re.I,
)


def _normalize_fedramp_status(raw: str) -> str:
    """Map raw FedRAMP marketplace status strings to the CR26
    vocabulary in schema/vocab.py:FEDRAMP_STATUS.

    Inputs may come from the live marketplace JSON export (using
    today's lifecycle labels) or from older fixtures (using the
    pre-CR26 vocab). Mapping per RFC-0020 / NTC-0004:

      "Continuous Monitoring" / "Persistent Validation" /
      legacy "Authorized" / legacy "FedRAMP Certified"  → certified
      "Agency Authorization In Process" / "Prioritized" /
      "Assessment by FedRAMP" / legacy "In Process"     → in_process
      "Preparation" / legacy "Ready"                    → preparation
      "Remediation"                                     → remediation
      anything else                                     → none
    """
    s = (raw or "").strip().lower()
    if not s:
        return "none"
    # CR26 marketplace lifecycle end-states ⇒ Certified.
    if (
        "continuous monitoring" in s
        or "persistent validation" in s
        or "certified" in s
        or "authorized" in s
    ):
        return "certified"
    # CR26 in-progress states.
    if (
        "agency authorization in process" in s
        or "prioritized" in s
        or "assessment by fedramp" in s
        or "in process" in s
        or "in-process" in s
    ):
        return "in_process"
    if "remediation" in s:
        return "remediation"
    # Preparation absorbs the retiring "Ready" label.
    if "preparation" in s or "ready" in s:
        return "preparation"
    return "none"


def _extract_dod_il(text: str) -> str:
    if not text:
        return "none"
    m = _IL_RE.search(text)
    if not m:
        return "none"
    level = m.group(1)
    return f"IL{level}" if f"IL{level}" in DOD_IL_LEVELS else "none"


class FedrampAPI(FedrampBackend):
    name = "fedramp_live"

    def __init__(self, timeout: float = 60.0):
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        )
        self._cached: list[FedrampRecord] | None = None

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def all_records(self) -> list[FedrampRecord]:
        if self._cached is not None:
            return self._cached
        try:
            r = self.client.get(FEDRAMP_PRODUCTS_URL)
            r.raise_for_status()
            payload = r.json()
        except httpx.HTTPError as exc:
            log.error("[fedramp] marketplace fetch failed: %s", exc)
            self._cached = []
            return []

        # The GitHub export wraps records in {"metadata": ..., "products": [...]}.
        # The legacy marketplace API returned either a list or {"data": [...]}.
        # Both shapes handled here.
        if isinstance(payload, dict):
            rows = payload.get("products") or payload.get("data") or []
        else:
            rows = payload or []
        records: list[FedrampRecord] = []
        for row in rows:
            attrs = row.get("attributes", row) if isinstance(row, dict) else {}
            # GitHub export uses `csp` / `cso` / `public_status` / `auth_date`.
            # Legacy marketplace API used `csp_name` / `product_name` / `status` / `authorization_date`.
            csp = (
                attrs.get("csp")
                or attrs.get("csp_name")
                or attrs.get("vendor")
                or ""
            ).strip()
            if not csp:
                continue
            product = (
                attrs.get("cso")
                or attrs.get("product_name")
                or attrs.get("name")
                or ""
            ).strip()
            status = _normalize_fedramp_status(
                attrs.get("public_status") or attrs.get("status", "")
            )
            description = (
                attrs.get("description", "")
                or attrs.get("service_description", "")
                or ""
            )
            auth_date = attrs.get("auth_date") or attrs.get("authorization_date")
            # GitHub export gives an ISO timestamp; keep just the date portion.
            if isinstance(auth_date, str) and "T" in auth_date:
                auth_date = auth_date.split("T", 1)[0]
            records.append(FedrampRecord(
                csp_name=csp,
                csp_normalized=normalize_name(csp),
                product_name=product,
                status=status,
                impact_level=(attrs.get("impact_level") or "").lower(),
                dod_il_level=_extract_dod_il(f"{product} {description}"),
                authorization_date=auth_date,
            ))
        self._cached = records
        log.info("[fedramp] loaded %d marketplace records", len(records))
        return records


class OfflineFedramp(FedrampBackend):
    name = "fedramp_offline"

    def __init__(self, fixture: list[dict] | None = None):
        self._fixture = fixture or []

    @classmethod
    def from_file(cls, path: str | Path) -> "OfflineFedramp":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def all_records(self) -> list[FedrampRecord]:
        out: list[FedrampRecord] = []
        for d in self._fixture:
            csp = d.get("csp_name", "")
            out.append(FedrampRecord(
                csp_name=csp,
                csp_normalized=normalize_name(csp),
                product_name=d.get("product_name", ""),
                status=_normalize_fedramp_status(d.get("status", "")),
                impact_level=d.get("impact_level", "").lower(),
                dod_il_level=d.get("dod_il_level") or _extract_dod_il(
                    f"{d.get('product_name','')} {d.get('description','')}"
                ),
                authorization_date=d.get("authorization_date"),
            ))
        return out


def get_fedramp_backend() -> FedrampBackend:
    name = os.getenv("FEDRAMP_BACKEND", "offline").lower()
    if name == "live":
        return FedrampAPI()
    if name == "offline":
        path = os.getenv("OFFLINE_FEDRAMP_FIXTURE")
        if path and os.path.exists(path):
            return OfflineFedramp.from_file(path)
        return OfflineFedramp()
    raise ValueError(f"unknown FEDRAMP_BACKEND: {name}")


# ============================================================
# USASpending — OT / contract signal lookup
# ============================================================

USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
CONTRACT_CODES = ["A", "B", "C", "D"]


def lookup_ota_signals(recipient_name: str, *, fiscal_years: list[int] | None = None,
                      timeout: float = 30.0) -> list[dict]:
    from datetime import date
    fiscal_years = fiscal_years or [date.today().year, date.today().year - 1]
    payload = {
        "filters": {
            "recipient_search_text": [recipient_name],
            "time_period": [
                {"start_date": f"{fy - 1}-10-01", "end_date": f"{fy}-09-30"}
                for fy in fiscal_years
            ],
            "award_type_codes": CONTRACT_CODES,
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Awarding Agency", "Awarding Sub Agency",
            "Award Type", "Description",
        ],
        "page": 1,
        "limit": 10,
        "sort": "Award Amount",
        "order": "desc",
    }
    try:
        r = httpx.post(USASPENDING_URL, json=payload, timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("usaspending %r: %s", recipient_name, exc)
        return []
    return r.json().get("results", [])


# ============================================================
# The enrichment pass
# ============================================================

def enrich_all(
    *,
    fedramp_backend: FedrampBackend | None = None,
    skip_usaspending: bool = False,
    max_companies: int | None = None,
) -> dict:
    fedramp_backend = fedramp_backend or get_fedramp_backend()
    fedramp_records = fedramp_backend.all_records()
    fr_index: dict[str, list[FedrampRecord]] = {}
    for rec in fedramp_records:
        fr_index.setdefault(rec.csp_normalized, []).append(rec)

    companies = store.load_companies()
    if max_companies:
        companies = companies[:max_companies]

    summary = {
        "considered": len(companies),
        "fedramp_matched": 0,
        "il_detected": 0,
        "ota_signals_found": 0,
        "fields_updated": 0,
    }

    for c in companies:
        names_to_try = [c["name"]] + (c.get("aliases") or [])
        recs: list[FedrampRecord] = []
        for n in names_to_try:
            recs.extend(fr_index.get(normalize_name(n), []))
        if recs:
            # CR26 vocab ordering — pick the most advanced status
            # when a single CSP has multiple marketplace entries.
            order = {
                "certified":   4,
                "in_process":  3,
                "preparation": 2,
                "remediation": 1,
                # Back-compat for any old data still using legacy
                # vocab; these get rewritten on the next enrichment.
                "authorized":  4,
                "ready":       2,
                "none":        0,
            }
            best = max(recs, key=lambda r: order.get(r.status, 0))
            if c.get("fedramp_status") != best.status:
                c["fedramp_status"] = best.status
                summary["fields_updated"] += 1
            if best.dod_il_level != "none" and c.get("dod_il_level") != best.dod_il_level:
                c["dod_il_level"] = best.dod_il_level
                summary["fields_updated"] += 1
                summary["il_detected"] += 1
            summary["fedramp_matched"] += 1

        if not skip_usaspending:
            awards = lookup_ota_signals(c["name"])
            if awards:
                signals = [
                    {
                        "award_id": a.get("Award ID"),
                        "amount": a.get("Award Amount"),
                        "agency": a.get("Awarding Agency"),
                        "type": a.get("Award Type"),
                        "description": (a.get("Description") or "")[:200],
                    }
                    for a in awards[:5]
                ]
                if c.get("ota_signals") != signals:
                    c["ota_signals"] = signals
                    summary["fields_updated"] += 1
                summary["ota_signals_found"] += 1
            time.sleep(0.3)

        store.upsert_company(c)

    log.info("[compliance] %s", summary)
    return summary

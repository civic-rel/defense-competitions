"""SQLite store for v2.

Three tables: events, companies, participations. Plus http_cache
(shared with v1) and a review_queue for analyst-cleared stealth
matches.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "events.sqlite"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_TTL_SECONDS = 24 * 60 * 60


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    # Look up DB_PATH at call time (not function-definition time) so
    # tests can monkey-patch the module-level attribute to point at a
    # temporary SQLite file without polluting the production store.
    # Production callers omit `path` and get the default.
    p = Path(path) if path is not None else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        _migrate_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """In-place migrations for existing DBs that predate newer columns.

    SQLite's CREATE TABLE IF NOT EXISTS won't add new columns to an
    existing table, so we check column existence via PRAGMA and ADD
    COLUMN whatever's missing. Idempotent — safe to run on every
    connect.
    """
    existing_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(events)").fetchall()
    }
    if "expected_participants" not in existing_cols:
        log.info("[store] migrating: ADD COLUMN events.expected_participants")
        conn.execute(
            "ALTER TABLE events ADD COLUMN expected_participants INTEGER"
        )


# ---- HTTP cache ----

def cache_get(url: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bytes | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT body, fetched_at FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
    if not row or time.time() - row["fetched_at"] > ttl_seconds:
        return None
    return row["body"]


def cache_set(url: str, body: bytes) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO http_cache (url, body, fetched_at) VALUES (?, ?, ?)",
            (url, body, time.time()),
        )


# ---- Events ----

def upsert_event(event: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO events "
            "(id, name, aliases, host, dates_start, dates_end, "
            "location, source_url, expected_participants) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["id"], event["name"],
                json.dumps(event.get("aliases", [])),
                event.get("host", ""),
                event["dates_start"], event.get("dates_end"),
                event.get("location", ""), event.get("source_url", ""),
                event.get("expected_participants"),
            ),
        )


def load_event(event_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["aliases"] = json.loads(d["aliases"] or "[]")
    return d


# ---- Companies ----

def upsert_company(company: dict) -> bool:
    """Returns True if inserted, False if updated."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM companies WHERE id = ?", (company["id"],)
        ).fetchone()
        payload = (
            company["id"], company["name"], company["normalized_name"],
            json.dumps(company.get("aliases", [])),
            company.get("type", "unknown"),
            json.dumps(company.get("domains", [])),
            company.get("crunchbase_url", ""), company.get("linkedin_url", ""),
            company.get("website", ""), company.get("parent_company_id"),
            company.get("fedramp_status", "unknown"),
            company.get("dod_il_level", "unknown"),
            json.dumps(company.get("ota_signals", [])),
            json.dumps(company.get("notable_investors", [])),
            company.get("total_funding_usd"),
            json.dumps(company.get("last_round")) if company.get("last_round") else None,
            1 if company.get("is_stealth") else 0,
            company["first_seen"], company["last_seen"],
        )
        if existing:
            conn.execute(
                "UPDATE companies SET name=?, normalized_name=?, aliases=?, "
                "type=?, domains=?, crunchbase_url=?, linkedin_url=?, website=?, "
                "parent_company_id=?, fedramp_status=?, dod_il_level=?, "
                "ota_signals=?, notable_investors=?, total_funding_usd=?, "
                "last_round=?, is_stealth=?, first_seen=?, last_seen=? "
                "WHERE id=?",
                payload[1:] + (company["id"],),
            )
            return False
        else:
            conn.execute(
                "INSERT INTO companies VALUES " + "(" + ",".join("?" * 19) + ")",
                payload,
            )
            return True


def load_companies() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM companies").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["aliases"] = json.loads(d["aliases"] or "[]")
        d["domains"] = json.loads(d["domains"] or "[]")
        d["ota_signals"] = json.loads(d["ota_signals"] or "[]")
        d["notable_investors"] = json.loads(d["notable_investors"] or "[]")
        d["last_round"] = json.loads(d["last_round"]) if d["last_round"] else None
        d["is_stealth"] = bool(d["is_stealth"])
        out.append(d)
    return out


def find_company_by_normalized(normalized_name: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE normalized_name = ?", (normalized_name,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["aliases"] = json.loads(d["aliases"] or "[]")
    return d


# ---- Participations ----

def upsert_participation(p: dict) -> bool:
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM participations WHERE id = ?", (p["id"],)
        ).fetchone()
        payload = (
            p["id"], p["company_id"], p["event_id"], p["role"],
            p["confidence"], p["evidence_url"], p.get("evidence_excerpt", ""),
            p["extracted_by"], p["extracted_at"], p.get("notes", ""),
        )
        if existing:
            conn.execute(
                "UPDATE participations SET company_id=?, event_id=?, role=?, "
                "confidence=?, evidence_url=?, evidence_excerpt=?, "
                "extracted_by=?, extracted_at=?, notes=? WHERE id=?",
                payload[1:] + (p["id"],),
            )
            return False
        conn.execute(
            "INSERT INTO participations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        return True


def load_participations(event_id: str | None = None) -> list[dict]:
    with connect() as conn:
        if event_id:
            rows = conn.execute(
                "SELECT * FROM participations WHERE event_id = ?", (event_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM participations").fetchall()
    return [dict(r) for r in rows]


# ---- Review queue ----

def queue_for_review(item: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO review_queue "
            "(candidate_name, nearest_match, similarity, event_id, "
            "evidence_url, evidence_excerpt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                item["candidate_name"], item.get("nearest_match"),
                item.get("similarity"), item.get("event_id"),
                item.get("evidence_url"), item.get("evidence_excerpt"),
                datetime.utcnow().isoformat(),
            ),
        )

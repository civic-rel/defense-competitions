-- v2 schema. Companies first-class; participations is the
-- evidence backbone.

CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    body       BLOB NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    aliases               TEXT,        -- JSON array
    host                  TEXT,
    dates_start           TEXT NOT NULL,
    dates_end             TEXT,
    location              TEXT,
    source_url            TEXT,
    expected_participants INTEGER     -- confirmed total when known; NULL = unknown
);
CREATE INDEX IF NOT EXISTS idx_events_dates ON events(dates_start);

-- Backfill the new column on existing databases. SQLite skips
-- ADD COLUMN if the column already exists (via the "OR IGNORE"
-- error contract handled by the trigger pattern), so it's safe
-- to run on every connect.
-- (Implemented in code via PRAGMA table_info check in store.cache;
-- this comment documents why the column ordering doesn't matter.)

CREATE TABLE IF NOT EXISTS companies (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    normalized_name    TEXT NOT NULL,
    aliases            TEXT,        -- JSON array
    type               TEXT NOT NULL,
    domains            TEXT,        -- JSON array
    crunchbase_url     TEXT,
    linkedin_url       TEXT,
    website            TEXT,
    parent_company_id  TEXT,
    fedramp_status     TEXT,
    dod_il_level       TEXT,
    ota_signals        TEXT,        -- JSON array
    notable_investors  TEXT,        -- JSON array
    total_funding_usd  REAL,
    last_round         TEXT,        -- JSON
    is_stealth         INTEGER,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT NOT NULL,
    FOREIGN KEY(parent_company_id) REFERENCES companies(id)
);
CREATE INDEX IF NOT EXISTS idx_companies_norm ON companies(normalized_name);
CREATE INDEX IF NOT EXISTS idx_companies_last_seen ON companies(last_seen);

CREATE TABLE IF NOT EXISTS participations (
    id               TEXT PRIMARY KEY,
    company_id       TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    role             TEXT NOT NULL,
    confidence       TEXT NOT NULL,
    evidence_url     TEXT NOT NULL,
    evidence_excerpt TEXT,
    extracted_by     TEXT NOT NULL,
    extracted_at     TEXT NOT NULL,
    notes            TEXT,
    FOREIGN KEY(company_id) REFERENCES companies(id),
    FOREIGN KEY(event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_part_company ON participations(company_id);
CREATE INDEX IF NOT EXISTS idx_part_event   ON participations(event_id);
CREATE INDEX IF NOT EXISTS idx_part_confidence ON participations(confidence);

-- Review queue for candidate stealth-startup matches the matcher
-- flagged but didn't auto-promote.
CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_name  TEXT NOT NULL,
    nearest_match   TEXT,      -- existing company id, if any
    similarity      REAL,
    event_id        TEXT,
    evidence_url    TEXT,
    evidence_excerpt TEXT,
    created_at      TEXT NOT NULL,
    resolution      TEXT       -- 'merged' | 'created' | 'rejected' | NULL (pending)
);

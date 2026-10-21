"""SQLite storage layer: schema + connection helper.

SQLite was chosen over flat JSON files because several guardrails (cooldown
checks, "no duplicate active draft", idempotent send) are fundamentally
uniqueness/lookup constraints, and a real database lets us enforce some of
them with constraints instead of re-implementing locking by hand. It is also
zero-setup for a local MVP.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    tech_name TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'dismissed')),
    created_at TEXT NOT NULL,
    last_contact_at TEXT
);

-- Raw event log, deduplicated by event_id. This is intentionally an
-- append-only ingestion of the source-of-truth event stream; derived signals
-- (last viewed, last replied, etc.) are computed from this table at read
-- time rather than cached, so ordering/duplication bugs can't leak into a
-- stale denormalized column.
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    channel TEXT,
    direction TEXT,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_quote ON events(quote_id);
CREATE INDEX IF NOT EXISTS idx_events_quote_type ON events(quote_id, type);

-- Our own draft -> approved -> sent pipeline. This is distinct from the
-- imported `message_sent` events above, which represent sends that happened
-- before/outside this system (e.g. seed history). At most one 'draft' or
-- 'approved' row may be open per quote at a time (enforced by the partial
-- unique index below), which is what makes re-running draft generation safe.
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL REFERENCES quotes(id),
    customer_phone TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'sent', 'failed')),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_quote ON messages(quote_id);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(customer_phone);
-- Guardrail as a constraint, not just application logic: a quote can only
-- have one un-sent (draft/approved) message outstanding at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_message_per_quote
    ON messages(quote_id)
    WHERE status IN ('draft', 'approved');

-- The "send" side effect. message_id is the idempotency key: sending is
-- `INSERT OR IGNORE` into this table, so retrying a send (crash, timeout,
-- at-least-once webhook redelivery, re-running the whole engine) can never
-- produce a second outbound message for the same draft. A real SMS/email
-- provider would sit behind write_outbox() in service.py.
CREATE TABLE IF NOT EXISTS outbox (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    quote_id TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    channel TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn

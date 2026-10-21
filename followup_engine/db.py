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
    amount INTEGER NOT NULL CHECK (amount >= 0),
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
    quote_id TEXT NOT NULL REFERENCES quotes(id),
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
    sent_at TEXT,
    -- A message can't reach approved/sent/failed without having been
    -- approved first (failed only happens to a previously-approved send
    -- attempt -- see service.send()), and can't be 'sent' without a
    -- sent_at. These make a half-populated audit trail impossible rather
    -- than just "shouldn't happen".
    CHECK (status = 'draft' OR approved_at IS NOT NULL),
    CHECK (status != 'sent' OR sent_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_messages_quote ON messages(quote_id);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(customer_phone);
-- Guardrail as a constraint, not just application logic: a quote can only
-- have one un-sent (draft/approved) message outstanding at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_message_per_quote
    ON messages(quote_id)
    WHERE status IN ('draft', 'approved');

-- Hard guardrail, enforced at the schema level so it cannot be bypassed by
-- any future code path: a message can never be drafted or approved for a
-- quote that isn't open, independent of whatever policy.py/service.py
-- happen to check in Python. (status IS NOT 'open' also catches an
-- unrecognized/missing quote_id, since the subquery then returns NULL.)
CREATE TRIGGER IF NOT EXISTS block_draft_for_non_open_quote
BEFORE INSERT ON messages
FOR EACH ROW
WHEN (SELECT status FROM quotes WHERE id = NEW.quote_id) IS NOT 'open'
BEGIN
    SELECT RAISE(ABORT, 'quote is not open: cannot create a message for it');
END;

CREATE TRIGGER IF NOT EXISTS block_approve_for_non_open_quote
BEFORE UPDATE OF status ON messages
FOR EACH ROW
WHEN NEW.status = 'approved' AND (SELECT status FROM quotes WHERE id = NEW.quote_id) IS NOT 'open'
BEGIN
    SELECT RAISE(ABORT, 'quote is not open: cannot approve a message for it');
END;

-- The "send" side effect. message_id is the idempotency key: sending is
-- `INSERT OR IGNORE` into this table, so retrying a send (crash, timeout,
-- at-least-once webhook redelivery, re-running the whole engine) can never
-- produce a second outbound message for the same draft. A real SMS/email
-- provider would sit behind write_outbox() in service.py.
CREATE TABLE IF NOT EXISTS outbox (
    message_id TEXT PRIMARY KEY REFERENCES messages(id),
    quote_id TEXT NOT NULL REFERENCES quotes(id),
    customer_phone TEXT NOT NULL,
    channel TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

-- Same hard guardrail as above, at the actual send boundary. service.send()
-- already re-checks quote status in Python before this insert, so in normal
-- operation this trigger never fires -- it exists purely so a future bug or
-- a direct write can never produce a real outbound send for a closed quote.
CREATE TRIGGER IF NOT EXISTS block_send_for_non_open_quote
BEFORE INSERT ON outbox
FOR EACH ROW
WHEN (SELECT status FROM quotes WHERE id = NEW.quote_id) IS NOT 'open'
BEGIN
    SELECT RAISE(ABORT, 'quote is not open: refusing to send');
END;
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn

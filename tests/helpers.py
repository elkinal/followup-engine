from __future__ import annotations

import sqlite3

from followup_engine import db


def make_db(testcase=None) -> sqlite3.Connection:
    conn = db.connect(":memory:")
    if testcase is not None:
        testcase.addCleanup(conn.close)
    return conn


def insert_quote(
    conn: sqlite3.Connection,
    id: str,
    customer_name: str = "Test Customer",
    customer_phone: str = "+19175550000",
    tech_name: str = "Tech",
    amount: int = 1000,
    status: str = "open",
    created_at: str = "2026-08-01T00:00:00Z",
    last_contact_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO quotes (id, customer_name, customer_phone, tech_name, amount,
                             status, created_at, last_contact_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (id, customer_name, customer_phone, tech_name, amount, status, created_at, last_contact_at),
    )
    conn.commit()


def insert_event(
    conn: sqlite3.Connection,
    event_id: str,
    type: str,
    quote_id: str,
    timestamp: str,
    channel: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO events (event_id, type, quote_id, timestamp, channel, direction, raw)
        VALUES (?, ?, ?, ?, ?, NULL, '{}')
        """,
        (event_id, type, quote_id, timestamp, channel),
    )
    conn.commit()

"""Load the seed quotes.json / events.jsonl files into SQLite.

Designed to be safe to re-run against a growing/changing export:
- quotes are upserted by id (a quote's status can legitimately change between
  runs in the real system, e.g. a tech marks it accepted)
- events are inserted keyed by event_id, so duplicate lines are no-ops
- malformed lines/records and events referencing an unknown quote are
  skipped and counted rather than aborting the whole ingest, since this
  stream is explicitly modeled on messy webhook delivery
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import parse_ts


@dataclass
class IngestReport:
    quotes_upserted: int = 0
    quotes_skipped: int = 0
    events_inserted: int = 0
    events_duplicate: int = 0
    events_skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"quotes: {self.quotes_upserted} upserted, {self.quotes_skipped} skipped | "
            f"events: {self.events_inserted} inserted, {self.events_duplicate} duplicate, "
            f"{len(self.events_skipped)} skipped"
        )


REQUIRED_QUOTE_FIELDS = {
    "id", "customer_name", "customer_phone", "tech_name",
    "amount", "status", "created_at",
}
VALID_STATUSES = {"open", "accepted", "dismissed"}
REQUIRED_EVENT_FIELDS = {"event_id", "type", "quote_id", "timestamp"}


def _valid_quote_row(row: object) -> bool:
    """Check both field *presence* and that values are actually usable --
    a key existing with a null/empty/wrong-typed value would otherwise slip
    past a plain issubset() check and only fail later, deep in a NOT NULL
    constraint or a crash in policy.py."""
    if not isinstance(row, dict) or not REQUIRED_QUOTE_FIELDS.issubset(row):
        return False
    if row["status"] not in VALID_STATUSES:
        return False
    if not isinstance(row["id"], str) or not row["id"]:
        return False
    if not isinstance(row["customer_name"], str) or not row["customer_name"].strip():
        return False
    if not isinstance(row["customer_phone"], str) or not row["customer_phone"]:
        return False
    if not isinstance(row["amount"], (int, float)) or isinstance(row["amount"], bool) or row["amount"] < 0:
        return False
    try:
        parse_ts(row["created_at"])
    except (TypeError, ValueError):
        return False
    last_contact = row.get("last_contact_at")
    if last_contact is not None:
        try:
            parse_ts(last_contact)
        except (TypeError, ValueError):
            return False
    return True


def ingest_quotes(conn: sqlite3.Connection, path: str | Path) -> IngestReport:
    report = IngestReport()
    data = json.loads(Path(path).read_text())
    for row in data:
        if not _valid_quote_row(row):
            report.quotes_skipped += 1
            continue
        try:
            conn.execute(
                """
                INSERT INTO quotes (id, customer_name, customer_phone, tech_name,
                                     amount, status, created_at, last_contact_at)
                VALUES (:id, :customer_name, :customer_phone, :tech_name,
                        :amount, :status, :created_at, :last_contact_at)
                ON CONFLICT(id) DO UPDATE SET
                    customer_name=excluded.customer_name,
                    customer_phone=excluded.customer_phone,
                    tech_name=excluded.tech_name,
                    amount=excluded.amount,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    last_contact_at=excluded.last_contact_at
                """,
                {**row, "last_contact_at": row.get("last_contact_at")},
            )
        except sqlite3.IntegrityError:
            # Belt and suspenders: _valid_quote_row() should have already
            # caught anything that would fail a schema constraint, but one
            # bad row must never abort the rows around it or lose whatever
            # was already staged in this transaction.
            report.quotes_skipped += 1
            continue
        report.quotes_upserted += 1
    conn.commit()
    return report


def ingest_events(conn: sqlite3.Connection, path: str | Path, report: IngestReport | None = None) -> IngestReport:
    report = report or IngestReport()
    known_quote_ids = {r["id"] for r in conn.execute("SELECT id FROM quotes")}

    with open(path) as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                report.events_skipped.append(f"line {lineno}: invalid JSON")
                continue

            if not isinstance(evt, dict) or not REQUIRED_EVENT_FIELDS.issubset(evt):
                report.events_skipped.append(f"line {lineno}: missing required field")
                continue
            if not isinstance(evt["event_id"], str) or not evt["event_id"]:
                report.events_skipped.append(f"line {lineno}: invalid event_id")
                continue
            if not isinstance(evt["type"], str) or not evt["type"]:
                report.events_skipped.append(f"line {lineno}: invalid type")
                continue
            if evt["quote_id"] not in known_quote_ids:
                report.events_skipped.append(
                    f"line {lineno}: unknown quote_id {evt['quote_id']!r}"
                )
                continue
            try:
                parse_ts(evt["timestamp"])
            except (TypeError, ValueError):
                report.events_skipped.append(
                    f"line {lineno}: unparseable timestamp {evt['timestamp']!r}"
                )
                continue

            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO events
                        (event_id, type, quote_id, timestamp, channel, direction, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evt["event_id"],
                        evt["type"],
                        evt["quote_id"],
                        evt["timestamp"],
                        evt.get("channel"),
                        evt.get("direction"),
                        json.dumps(evt, sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError:
                # Belt and suspenders, same reasoning as ingest_quotes().
                report.events_skipped.append(f"line {lineno}: rejected by the database")
                continue
            if cur.rowcount == 1:
                report.events_inserted += 1
            else:
                report.events_duplicate += 1
    conn.commit()
    return report


def ingest_all(conn: sqlite3.Connection, quotes_path: str | Path, events_path: str | Path) -> IngestReport:
    report = ingest_quotes(conn, quotes_path)
    ingest_events(conn, events_path, report)
    return report

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

from .phone import normalize_phone
from .timeutil import format_ts, parse_ts


class IngestError(Exception):
    """Raised for problems with the input itself (missing file, JSON that
    doesn't even parse, wrong top-level shape) -- distinct from a single bad
    row, which is skipped and logged rather than treated as fatal."""


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


def _normalize_quote_row(row: object) -> dict | None:
    """Validate a raw quote record and return a cleaned dict ready to bind
    into the INSERT, or None if the row isn't usable. Combines validation
    and normalization in one pass: a key existing with a null/empty/
    wrong-typed/wrong-shaped value would otherwise slip past a plain
    issubset() presence check and only fail later, deep in a NOT NULL
    constraint or a crash in policy.py."""
    if not isinstance(row, dict) or not REQUIRED_QUOTE_FIELDS.issubset(row):
        return None
    if row["status"] not in VALID_STATUSES:
        return None
    if not isinstance(row["id"], str) or not row["id"]:
        return None
    if not isinstance(row["customer_name"], str) or not row["customer_name"].strip():
        return None
    if not isinstance(row["amount"], (int, float)) or isinstance(row["amount"], bool) or row["amount"] < 0:
        return None

    # Normalize to the canonical E.164-ish phone form so the same customer
    # texted/entered in two different formats (e.g. "+19175552003" vs
    # "(917) 555-2003") resolves to one identity -- the cross-quote cooldown
    # guardrail keys on this field, so an un-normalized phone number would
    # silently let it be bypassed.
    phone = normalize_phone(row["customer_phone"])
    if phone is None:
        return None

    # Normalize to the canonical Z-suffixed UTC form before storing. Every
    # timestamp comparison/ordering in policy.py is a SQL string comparison
    # (`WHERE timestamp <= ?`, `ORDER BY timestamp`), which only agrees with
    # chronological order if every stored row uses the exact same format --
    # a validly-parseable but differently-shaped timestamp (e.g. a "+08:00"
    # offset instead of "Z") would otherwise sort incorrectly and silently
    # corrupt signal computation.
    try:
        created_at = format_ts(parse_ts(row["created_at"]))
    except (TypeError, ValueError):
        return None
    last_contact = row.get("last_contact_at")
    if last_contact is not None:
        try:
            last_contact = format_ts(parse_ts(last_contact))
        except (TypeError, ValueError):
            return None

    return {**row, "customer_phone": phone, "created_at": created_at, "last_contact_at": last_contact}


def _load_json_list(path: str | Path, what: str) -> list:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        raise IngestError(f"{what} file not found: {path}")
    except OSError as e:
        raise IngestError(f"could not read {what} file {path}: {e}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise IngestError(f"{what} file {path} is not valid JSON: {e}")
    if not isinstance(data, list):
        raise IngestError(f"{what} file {path} must contain a JSON array at the top level")
    return data


def ingest_quotes(conn: sqlite3.Connection, path: str | Path) -> IngestReport:
    report = IngestReport()
    data = _load_json_list(path, "quotes")
    for row in data:
        params = _normalize_quote_row(row)
        if params is None:
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
                params,
            )
        except sqlite3.IntegrityError:
            # Belt and suspenders: _normalize_quote_row() should have
            # already caught anything that would fail a schema constraint,
            # but one bad row must never abort the rows around it or lose
            # whatever was already staged in this transaction.
            report.quotes_skipped += 1
            continue
        report.quotes_upserted += 1
    conn.commit()
    return report


def ingest_events(conn: sqlite3.Connection, path: str | Path, report: IngestReport | None = None) -> IngestReport:
    report = report or IngestReport()
    known_quote_ids = {r["id"] for r in conn.execute("SELECT id FROM quotes")}

    try:
        f = open(path)
    except FileNotFoundError:
        raise IngestError(f"events file not found: {path}")
    except OSError as e:
        raise IngestError(f"could not read events file {path}: {e}")
    with f:
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
                normalized_ts = format_ts(parse_ts(evt["timestamp"]))
            except (TypeError, ValueError):
                report.events_skipped.append(
                    f"line {lineno}: unparseable timestamp {evt['timestamp']!r}"
                )
                continue

            raw = json.dumps(evt, sort_keys=True)
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
                        normalized_ts,  # not the raw string -- see ingest_quotes() for why
                        evt.get("channel"),
                        evt.get("direction"),
                        raw,
                    ),
                )
            except sqlite3.IntegrityError:
                # Belt and suspenders, same reasoning as ingest_quotes().
                report.events_skipped.append(f"line {lineno}: rejected by the database")
                continue
            if cur.rowcount == 1:
                report.events_inserted += 1
            else:
                # event_id already existed. An event_id is only supposed to
                # be reused for a byte-identical redelivery of the same
                # event (the "duplicate webhook" scenario this is meant to
                # handle) -- if the content actually differs, this is two
                # different events colliding on one id, which would
                # otherwise silently vanish into the "duplicate" count with
                # no trace. Surface it instead of guessing which one is
                # right; we keep whichever arrived first.
                existing_raw = conn.execute(
                    "SELECT raw FROM events WHERE event_id = ?", (evt["event_id"],)
                ).fetchone()[0]
                if existing_raw == raw:
                    report.events_duplicate += 1
                else:
                    report.events_skipped.append(
                        f"line {lineno}: event_id {evt['event_id']!r} reused for different "
                        f"content -- ignored, kept the first version seen"
                    )
    conn.commit()
    return report


def ingest_all(conn: sqlite3.Connection, quotes_path: str | Path, events_path: str | Path) -> IngestReport:
    report = ingest_quotes(conn, quotes_path)
    ingest_events(conn, events_path, report)
    return report

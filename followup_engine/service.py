"""Core operations tying policy + drafting + guardrails together:
create drafts, approve, send. This is the only module that writes to the
`messages` / `outbox` tables, so every guardrail lives here in one place.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import policy
from .drafting import draft_message
from .timeutil import format_ts


class GuardrailViolation(Exception):
    """Raised when an operation would violate a safety guardrail."""


@dataclass
class DraftResult:
    created: list[str]   # message ids created
    skipped: list[tuple[str, str]]  # (quote_id, reason) not drafted


def create_drafts(conn: sqlite3.Connection, now: datetime) -> DraftResult:
    """Turn today's top candidates into draft messages.

    Safe to call repeatedly for the same (or a later) `now`: a quote that
    already has an open draft/approved message is skipped (enforced by the
    `uniq_open_message_per_quote` index as a backstop), and a customer who
    was messaged within the cooldown window is skipped even if a *different*
    one of their quotes is the one producing the candidate.
    """
    candidates = policy.get_candidates(conn, now)
    last_outbound = policy.last_outbound_by_phone(conn, now)

    result = DraftResult(created=[], skipped=[])
    now_str = format_ts(now)

    for c in candidates:
        last = last_outbound.get(c.customer_phone)
        if last is not None and (now - last) < policy.COOLDOWN:
            result.skipped.append((c.quote_id, "cooldown"))
            continue

        message_id = str(uuid.uuid4())
        body = draft_message(c)
        try:
            conn.execute(
                """
                INSERT INTO messages
                    (id, quote_id, customer_phone, reason_code, reason, body,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (message_id, c.quote_id, c.customer_phone, c.reason_code, c.reason, body, now_str),
            )
        except sqlite3.IntegrityError as e:
            # Two cases land here: another draft/approved message is already
            # outstanding for this quote (e.g. this function ran twice for
            # the same batch), or the `block_draft_for_non_open_quote`
            # trigger rejected it because the quote closed between
            # get_candidates() and this insert (a race condition backstop --
            # policy.py already filters to open quotes, so this is rare).
            reason = "quote no longer open" if "not open" in str(e) else "already has an open draft"
            result.skipped.append((c.quote_id, reason))
            continue

        # A draft for this customer counts toward their own cooldown for the
        # rest of *this* batch, so we don't draft two messages to the same
        # phone number in one run.
        last_outbound[c.customer_phone] = now
        result.created.append(message_id)

    conn.commit()
    return result


def approve(conn: sqlite3.Connection, message_id: str, now: datetime) -> None:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such message: {message_id}")
    if row["status"] != "draft":
        raise GuardrailViolation(
            f"message {message_id} is '{row['status']}', not 'draft' -- cannot approve"
        )
    try:
        conn.execute(
            "UPDATE messages SET status = 'approved', approved_at = ? WHERE id = ?",
            (format_ts(now), message_id),
        )
    except sqlite3.IntegrityError:
        # block_approve_for_non_open_quote trigger: the quote closed between
        # draft creation and this approval.
        conn.rollback()
        raise GuardrailViolation(
            f"quote {row['quote_id']} is no longer open -- cannot approve message {message_id}"
        )
    conn.commit()


@dataclass
class SendResult:
    message_id: str
    already_sent: bool


def send(conn: sqlite3.Connection, message_id: str, now: datetime, channel: str = "sms") -> SendResult:
    """Send an approved message. Idempotent: calling this again for a
    message that's already sent (e.g. a retried request after the network
    call to the real provider timed out, or re-running the whole engine) is
    a safe no-op -- it will never write a second outbox row or re-send.
    """
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such message: {message_id}")

    if row["status"] == "sent":
        return SendResult(message_id=message_id, already_sent=True)

    if row["status"] not in ("approved", "failed"):
        raise GuardrailViolation(
            f"message {message_id} is '{row['status']}' -- must be 'approved' before sending"
        )

    # Re-validate against current quote state: it may have changed (e.g. the
    # customer accepted elsewhere) between draft creation and this send.
    quote = conn.execute("SELECT status FROM quotes WHERE id = ?", (row["quote_id"],)).fetchone()
    if quote is None or quote["status"] != "open":
        conn.execute("UPDATE messages SET status = 'failed' WHERE id = ?", (message_id,))
        conn.commit()
        raise GuardrailViolation(
            f"quote {row['quote_id']} is no longer open ({quote['status'] if quote else 'missing'}) -- refusing to send"
        )

    now_str = format_ts(now)
    # INSERT OR IGNORE on a PRIMARY KEY is the idempotency boundary: this is
    # the one write that represents "a message actually went out", keyed by
    # message_id so a retry of this exact call can never produce a second
    # outbound send.
    conn.execute(
        """
        INSERT OR IGNORE INTO outbox (message_id, quote_id, customer_phone, channel, body, sent_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (message_id, row["quote_id"], row["customer_phone"], channel, row["body"], now_str),
    )
    conn.execute(
        "UPDATE messages SET status = 'sent', sent_at = COALESCE(sent_at, ?) WHERE id = ? AND status != 'sent'",
        (now_str, message_id),
    )
    conn.commit()
    return SendResult(message_id=message_id, already_sent=False)

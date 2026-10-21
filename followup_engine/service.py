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
from .timeutil import format_ts, parse_ts


class GuardrailViolation(Exception):
    """Raised when an operation would violate a safety guardrail."""


@dataclass
class DraftResult:
    created: list[str]   # message ids created
    skipped: list[tuple[str, str]]  # (quote_id, reason) not drafted


def create_drafts(conn: sqlite3.Connection, now: datetime) -> DraftResult:
    """Turn today's top candidates into draft messages.

    Safe to call repeatedly, including across separate invocations (e.g. a
    re-run before an earlier batch was approved), not just within one call:
    a quote that already has an open draft/approved message is skipped
    (enforced by the `uniq_open_message_per_quote` index as a backstop), and
    a customer who was messaged -- or who already has an open, unsent draft
    -- within the cooldown window is skipped even if a *different* one of
    their quotes is the one producing the candidate. send() independently
    re-checks cooldown too, since it's the function with the actual
    side effect and can't assume this function's prevention ran.
    """
    candidates = policy.get_candidates(conn, now)
    last_outbound = policy.last_outbound_by_phone(conn, now)

    # An open (not-yet-sent) draft/approved message to a phone counts as a
    # provisional contact too, not just an actual send: without this, two
    # *separate* calls to create_drafts() (e.g. today's run, then a re-run
    # tomorrow before today's drafts were approved) could each independently
    # draft a message to the same customer for two different quotes -- and
    # since send() only re-checks the quote's status, not cooldown, a human
    # approving both would actually double-message the customer. Treating an
    # open draft as "spoken for" here stops the conflicting draft from being
    # created in the first place, rather than only catching it at send time.
    open_quote_ids = set()
    for row in conn.execute("SELECT quote_id, customer_phone, created_at FROM messages WHERE status IN ('draft', 'approved')"):
        open_quote_ids.add(row["quote_id"])
        ts = parse_ts(row["created_at"])
        phone = row["customer_phone"]
        if phone not in last_outbound or ts > last_outbound[phone]:
            last_outbound[phone] = ts

    result = DraftResult(created=[], skipped=[])
    now_str = format_ts(now)

    for c in candidates:
        # Check "this exact quote already has an open draft" before the
        # broader phone-level cooldown check below -- both would correctly
        # skip it, but they mean different things to a human reading the
        # skip list ("you already have a draft for this one" vs. "a
        # *different* quote for this customer was just contacted"), and the
        # more specific reason is more useful.
        if c.quote_id in open_quote_ids:
            result.skipped.append((c.quote_id, "already has an open draft"))
            continue

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
                     channel, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (message_id, c.quote_id, c.customer_phone, c.reason_code, c.reason, body, c.channel, now_str),
            )
        except sqlite3.IntegrityError as e:
            # Both the "already has an open draft" and "quote no longer
            # open" cases are now checked *before* this insert is attempted
            # (open_quote_ids above, and policy.py only surfacing open
            # quotes), so in a single-threaded run this except is dead code
            # by design -- it's a concurrency backstop for a genuinely
            # concurrent write landing between our checks and this insert
            # (e.g. two CLI processes racing), not a path exercised by
            # normal single-process use.
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


def approve(conn: sqlite3.Connection, message_id: str, now: datetime, approved_by: str) -> None:
    if not approved_by or not approved_by.strip():
        raise ValueError("approved_by is required -- who is approving this message?")
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such message: {message_id}")
    if row["status"] != "draft":
        raise GuardrailViolation(
            f"message {message_id} is '{row['status']}', not 'draft' -- cannot approve"
        )
    try:
        conn.execute(
            "UPDATE messages SET status = 'approved', approved_at = ?, approved_by = ? WHERE id = ?",
            (format_ts(now), approved_by, message_id),
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


def send(conn: sqlite3.Connection, message_id: str, now: datetime, channel: str | None = None) -> SendResult:
    """Send an approved message. Idempotent: calling this again for a
    message that's already sent (e.g. a retried request after the network
    call to the real provider timed out, or re-running the whole engine) is
    a safe no-op -- it will never write a second outbox row or re-send.

    `channel` defaults to None, meaning "use the channel this draft was
    written for" (chosen in policy.py from the customer's own history --
    see QuoteSignals.preferred_channel). Pass an explicit channel only to
    override that choice for this particular send.

    Everything from the guardrail re-checks through the outbox write runs
    inside one `BEGIN IMMEDIATE` transaction. That's not just tidiness: the
    cooldown check reads committed state and then writes based on what it
    read, and under genuine concurrency (two real processes/threads calling
    send() for two different messages to the *same* customer at the same
    time) both could read "no recent contact" before either had written
    anything -- a classic check-then-act race that let two real sends go
    out to the same customer in testing. BEGIN IMMEDIATE acquires SQLite's
    write lock up front, so a second concurrent send() blocks (via
    busy_timeout) until the first fully commits, and then correctly sees
    it. A quick cheap pre-check happens before the transaction too, so a
    message that's obviously already sent or not yet approved fails fast
    without taking a write lock for no reason.
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

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-read everything inside the lock: another concurrent send() for
        # this exact message_id could have completed while we were waiting
        # for the write lock above.
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row["status"] == "sent":
            conn.rollback()
            return SendResult(message_id=message_id, already_sent=True)

        # Re-validate against current quote state: it may have changed
        # (e.g. the customer accepted elsewhere) between draft creation and
        # this send. Phone number is read live here too, not from the
        # message's own snapshot -- if it's been corrected since the draft
        # was written (e.g. a data fix, or the quote reassigned to a
        # different customer record), sending to the stale number captured
        # at draft time would be wrong, unlike every other guardrail here
        # which deliberately checks current state.
        quote = conn.execute(
            "SELECT status, customer_phone FROM quotes WHERE id = ?", (row["quote_id"],)
        ).fetchone()
        if quote is None or quote["status"] != "open":
            conn.execute("UPDATE messages SET status = 'failed' WHERE id = ?", (message_id,))
            conn.commit()
            raise GuardrailViolation(
                f"quote {row['quote_id']} is no longer open "
                f"({quote['status'] if quote else 'missing'}) -- refusing to send"
            )
        # quotes.status can lag the event stream (see
        # policy.quote_accepted_via_events) -- check that independently too.
        if policy.quote_accepted_via_events(conn, row["quote_id"], now):
            conn.execute("UPDATE messages SET status = 'failed' WHERE id = ?", (message_id,))
            conn.commit()
            raise GuardrailViolation(
                f"quote {row['quote_id']} was accepted per the event stream -- refusing to send"
            )
        customer_phone = quote["customer_phone"]

        # Re-validate cooldown too: create_drafts() blocks a *new* draft to
        # a phone that already has one outstanding, but two drafts for the
        # same customer can still both reach 'approved' (e.g. from two
        # separate draft-generation runs before either was sent). This is
        # the authoritative check -- the one that actually gates the real
        # outbound side effect -- mirroring the quote-status re-check above.
        last_outbound = policy.last_outbound_by_phone(conn, now)
        last_sent_to_this_phone = last_outbound.get(customer_phone)
        if last_sent_to_this_phone is not None and (now - last_sent_to_this_phone) < policy.COOLDOWN:
            conn.execute("UPDATE messages SET status = 'failed' WHERE id = ?", (message_id,))
            conn.commit()
            raise GuardrailViolation(
                f"customer {customer_phone} was already messaged within the cooldown window -- refusing to send"
            )

        now_str = format_ts(now)
        send_channel = channel if channel is not None else row["channel"]
        try:
            # INSERT OR IGNORE on a PRIMARY KEY is the idempotency boundary:
            # this is the one write that represents "a message actually
            # went out", keyed by message_id so a retry of this exact call
            # can never produce a second outbound send.
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox (message_id, quote_id, customer_phone, channel, body, sent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, row["quote_id"], customer_phone, send_channel, row["body"], now_str),
            )
        except sqlite3.IntegrityError:
            # block_send_for_non_open_quote trigger: a backstop that should
            # be unreachable given the checks above now run in the same
            # transaction, but caught cleanly rather than left to crash
            # raw, consistent with how the equivalent triggers are handled
            # in create_drafts()/approve().
            conn.execute("UPDATE messages SET status = 'failed' WHERE id = ?", (message_id,))
            conn.commit()
            raise GuardrailViolation(
                f"quote {row['quote_id']} is not open -- refusing to send"
            )
        conn.execute(
            "UPDATE messages SET status = 'sent', sent_at = COALESCE(sent_at, ?) WHERE id = ? AND status != 'sent'",
            (now_str, message_id),
        )
        conn.commit()
        return SendResult(message_id=message_id, already_sent=False)
    except BaseException:
        conn.rollback()
        raise

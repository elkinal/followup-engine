"""The follow-up policy: given "now", produce a prioritized list of open
quotes worth following up on, each with a machine-readable reason_code and a
human-readable reason.

Design notes (see README for the fuller rationale):

- Signals are computed fresh from the `events` table at call time, sorted by
  timestamp (not file/insertion order), so duplicate or out-of-order webhook
  delivery can't skew them.
- `quotes.last_contact_at` from the seed is deliberately NOT trusted as the
  basis for the policy: it disagrees with the event log for several quotes
  in the seed data (e.g. Q-1002 has later message_sent/customer_replied
  events than its last_contact_at, and Q-1005 has a message_sent event but
  last_contact_at=null). It's kept in storage for reference/debugging only.
  "Last contact" for policy purposes is derived from events instead.
- "Contact" = an outbound message_sent or an inbound customer_replied.
  quote_viewed is a strong interest signal but is passive (the customer
  didn't do anything to us), so it does not reset the contact clock on its
  own.
- Outbound-contact recency for the cooldown guardrail is tracked per
  *customer phone number*, not per quote, because the seed data includes two
  separate open quotes for the same customer (Q-1003 and Q-1025, both
  +19175552003 / Karen Nguyen). Texting that person about job A should count
  against texting her again about job B a few hours later -- the guardrail
  is "don't spam this human", not "don't spam this quote".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3

from .timeutil import parse_ts

# Tunable thresholds. Kept as module-level constants (not hidden magic
# numbers inline) so they're easy to find, justify, and override in tests.
REPLY_GRACE = timedelta(hours=2)        # give a human a moment before flagging an unanswered reply
VIEW_WINDOW = timedelta(hours=72)       # a view counts as "recent" for 3 days
STALE_DAYS_BIG = timedelta(days=4)      # big quotes get chased sooner
STALE_DAYS_GENERAL = timedelta(days=7)  # catch-all sweep for everything else
BIG_QUOTE_THRESHOLD = 5000              # dollars
COOLDOWN = timedelta(hours=48)          # minimum gap between outbound messages to the same customer

CONTACT_EVENT_TYPES = {"message_sent", "customer_replied"}

# Priority tiers: higher sorts first. Chosen order reflects urgency/risk, not
# just recency -- see README "follow-up policy and why".
TIER_REPLY_PENDING = 100
TIER_VIEWED_NO_RESPONSE = 70
TIER_STALE_BIG = 40
TIER_STALE_GENERAL = 10


DEFAULT_CHANNEL = "sms"
KNOWN_CHANNELS = {"sms", "email"}  # matches messages.channel's CHECK constraint in db.py


@dataclass
class QuoteSignals:
    last_viewed_at: datetime | None = None
    last_replied_at: datetime | None = None
    last_replied_channel: str | None = None
    last_outbound_at: datetime | None = None  # message_sent, this quote only
    last_outbound_channel: str | None = None

    @property
    def last_contact_at(self) -> datetime | None:
        candidates = [t for t in (self.last_replied_at, self.last_outbound_at) if t is not None]
        return max(candidates) if candidates else None

    @property
    def preferred_channel(self) -> str:
        """Best guess at which channel actually reaches this customer, from
        their own event history. A reply is the strongest signal -- they
        chose that channel themselves -- so it wins even if it's older than
        our last outbound attempt on a different channel; otherwise we go
        with whichever channel we last messaged them on. Falls back to
        DEFAULT_CHANNEL when there's no history at all (a brand-new quote
        we haven't contacted yet) or the recorded channel isn't one we
        actually know how to send on (unexpected event data)."""
        if self.last_replied_channel in KNOWN_CHANNELS:
            return self.last_replied_channel
        if self.last_outbound_channel in KNOWN_CHANNELS:
            return self.last_outbound_channel
        return DEFAULT_CHANNEL


@dataclass
class Candidate:
    quote_id: str
    customer_name: str
    customer_phone: str
    tech_name: str
    amount: int
    tier: int
    reason_code: str
    reason: str
    channel: str
    sort_key: tuple  # secondary ordering within a tier, descending


def compute_signals(conn: sqlite3.Connection, now: datetime) -> dict[str, QuoteSignals]:
    """Per-quote signals derived from the event log plus our own sent
    messages, as of `now`. Public entry point for callers (e.g. service.py)
    that need signals without re-deriving the whole candidate list."""
    signals = _compute_quote_signals(conn, now)
    _apply_our_sent_messages(conn, signals, now)
    return signals


def _compute_quote_signals(conn: sqlite3.Connection, now: datetime) -> dict[str, QuoteSignals]:
    signals: dict[str, QuoteSignals] = {}
    rows = conn.execute(
        "SELECT quote_id, type, timestamp, channel FROM events WHERE timestamp <= ? ORDER BY timestamp ASC, event_id ASC",
        (now.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    for row in rows:
        qid = row["quote_id"]
        sig = signals.setdefault(qid, QuoteSignals())
        ts = parse_ts(row["timestamp"])
        if row["type"] == "quote_viewed":
            if sig.last_viewed_at is None or ts > sig.last_viewed_at:
                sig.last_viewed_at = ts
        elif row["type"] == "customer_replied":
            if sig.last_replied_at is None or ts > sig.last_replied_at:
                sig.last_replied_at = ts
                sig.last_replied_channel = row["channel"]
        elif row["type"] == "message_sent":
            if sig.last_outbound_at is None or ts > sig.last_outbound_at:
                sig.last_outbound_at = ts
                sig.last_outbound_channel = row["channel"]
    return signals


def _apply_our_sent_messages(conn: sqlite3.Connection, signals: dict[str, QuoteSignals], now: datetime) -> None:
    """Fold in messages this engine has actually sent, so a follow-up we just
    sent immediately counts as contact (don't re-flag it next run)."""
    rows = conn.execute(
        "SELECT quote_id, sent_at, channel FROM messages WHERE status = 'sent' AND sent_at <= ?",
        (now.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    for row in rows:
        qid = row["quote_id"]
        sig = signals.setdefault(qid, QuoteSignals())
        ts = parse_ts(row["sent_at"])
        if sig.last_outbound_at is None or ts > sig.last_outbound_at:
            sig.last_outbound_at = ts
            sig.last_outbound_channel = row["channel"]


def last_outbound_by_phone(conn: sqlite3.Connection, now: datetime) -> dict[str, datetime]:
    """Most recent outbound contact per customer phone, across all of that
    customer's quotes. Used for the cross-quote cooldown guardrail."""
    signals = compute_signals(conn, now)
    phone_by_quote = {
        row["id"]: row["customer_phone"]
        for row in conn.execute("SELECT id, customer_phone FROM quotes")
    }
    result: dict[str, datetime] = {}
    for qid, sig in signals.items():
        if sig.last_outbound_at is None:
            continue
        phone = phone_by_quote.get(qid)
        if phone is None:
            continue
        if phone not in result or sig.last_outbound_at > result[phone]:
            result[phone] = sig.last_outbound_at
    return result


def _fmt_delta(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{delta.days}d"


def evaluate_quote(
    quote: sqlite3.Row,
    sig: QuoteSignals,
    now: datetime,
) -> Candidate | None:
    """Apply the rule set to a single open quote. Returns None if nothing
    currently warrants a follow-up."""
    amount = quote["amount"]

    # Rule 1: customer replied and we haven't responded since.
    if sig.last_replied_at is not None:
        if sig.last_outbound_at is None or sig.last_outbound_at < sig.last_replied_at:
            waited = now - sig.last_replied_at
            if waited >= REPLY_GRACE:
                return Candidate(
                    quote_id=quote["id"], customer_name=quote["customer_name"],
                    customer_phone=quote["customer_phone"], tech_name=quote["tech_name"],
                    amount=amount, tier=TIER_REPLY_PENDING, reason_code="reply_pending",
                    reason=f"Customer replied {_fmt_delta(waited)} ago and hasn't heard back.",
                    channel=sig.preferred_channel,
                    sort_key=(-waited,),  # longer unanswered sorts first
                )

    # Rule 2: viewed recently, no reply, and we haven't already nudged since that view.
    if sig.last_viewed_at is not None:
        since_view = now - sig.last_viewed_at
        if since_view <= VIEW_WINDOW:
            replied_after_view = sig.last_replied_at is not None and sig.last_replied_at > sig.last_viewed_at
            nudged_after_view = sig.last_outbound_at is not None and sig.last_outbound_at > sig.last_viewed_at
            if not replied_after_view and not nudged_after_view:
                return Candidate(
                    quote_id=quote["id"], customer_name=quote["customer_name"],
                    customer_phone=quote["customer_phone"], tech_name=quote["tech_name"],
                    amount=amount, tier=TIER_VIEWED_NO_RESPONSE, reason_code="viewed_no_response",
                    reason=f"Viewed {_fmt_delta(since_view)} ago, no reply yet — good moment to nudge.",
                    channel=sig.preferred_channel,
                    sort_key=(since_view,),  # more recent view sorts first
                )

    # Rules 3/4: no contact in a while. "Contact" = outbound message or reply;
    # falls back to created_at if the quote has never been contacted at all.
    last_contact = sig.last_contact_at or parse_ts(quote["created_at"])
    stale_for = now - last_contact
    if stale_for >= STALE_DAYS_BIG and amount >= BIG_QUOTE_THRESHOLD:
        return Candidate(
            quote_id=quote["id"], customer_name=quote["customer_name"],
            customer_phone=quote["customer_phone"], tech_name=quote["tech_name"],
            amount=amount, tier=TIER_STALE_BIG, reason_code="stale_big",
            reason=f"${amount:,} quote with no contact in {stale_for.days}d.",
            channel=sig.preferred_channel,
            sort_key=(-amount, -stale_for),  # bigger, staler quotes sort first
        )
    if stale_for >= STALE_DAYS_GENERAL:
        return Candidate(
            quote_id=quote["id"], customer_name=quote["customer_name"],
            customer_phone=quote["customer_phone"], tech_name=quote["tech_name"],
            amount=amount, tier=TIER_STALE_GENERAL, reason_code="stale_general",
            reason=f"No contact in {stale_for.days}d.",
            channel=sig.preferred_channel,
            sort_key=(-stale_for,),  # staler quotes sort first
        )

    return None


def get_candidates(conn: sqlite3.Connection, now: datetime) -> list[Candidate]:
    signals = compute_signals(conn, now)

    quotes = conn.execute("SELECT * FROM quotes WHERE status = 'open'").fetchall()
    candidates = []
    for quote in quotes:
        sig = signals.get(quote["id"], QuoteSignals())
        candidate = evaluate_quote(quote, sig, now)
        if candidate is not None:
            candidates.append(candidate)

    # sort_key entries are pre-signed so that ascending order is already
    # "most urgent first" (see evaluate_quote); tier differs first and
    # short-circuits the comparison whenever two candidates are in different
    # tiers, so mixing tuple shapes/types across tiers is safe here.
    candidates.sort(key=lambda c: (-c.tier, *c.sort_key))
    return candidates

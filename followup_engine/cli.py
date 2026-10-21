from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db, ingest, policy, service
from .timeutil import parse_ts

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "followup.db"
DEFAULT_SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def _connect(args) -> sqlite3.Connection:
    return db.connect(args.db)


def _now(args) -> datetime:
    if args.now:
        return parse_ts(args.now)
    return datetime.now(timezone.utc)


def cmd_ingest(args):
    conn = _connect(args)
    report = ingest.ingest_all(conn, args.quotes, args.events)
    print(report.summary())
    for msg in report.events_skipped:
        print(f"  skipped: {msg}")
    for msg in report.warnings:
        print(f"  warning: {msg}")


def cmd_candidates(args):
    conn = _connect(args)
    now = _now(args)
    candidates = policy.get_candidates(conn, now)
    if not candidates:
        print(f"No follow-up candidates as of {now.isoformat()}.")
        return
    print(f"{len(candidates)} follow-up candidate(s) as of {now.isoformat()}:\n")
    for c in candidates:
        print(f"[{c.reason_code:<18}] {c.quote_id}  {c.customer_name:<16} "
              f"${c.amount:>7,}  tech={c.tech_name:<8} -- {c.reason}")


def cmd_draft(args):
    conn = _connect(args)
    now = _now(args)
    result = service.create_drafts(conn, now)
    print(f"Created {len(result.created)} draft(s).")
    for mid in result.created:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        print(f"  [{mid}] {row['quote_id']} ({row['reason_code']}): {row['body']}")
    if result.skipped:
        print(f"Skipped {len(result.skipped)}:")
        for qid, why in result.skipped:
            print(f"  {qid}: {why}")


def cmd_drafts(args):
    conn = _connect(args)
    query = "SELECT * FROM messages"
    params = ()
    if args.status:
        query += " WHERE status = ?"
        params = (args.status,)
    query += " ORDER BY created_at ASC"
    rows = conn.execute(query, params).fetchall()
    if not rows:
        print("No messages.")
        return
    for row in rows:
        approver = f" approved_by={row['approved_by']}" if row["approved_by"] else ""
        print(f"[{row['status']:<9}] {row['id']}  {row['quote_id']}  "
              f"{row['customer_phone']}  [{row['channel']}]  ({row['reason_code']}): {row['body']}{approver}")


def cmd_approve(args):
    conn = _connect(args)
    now = _now(args)
    # Default to the OS user running the command -- a reasonable zero-config
    # identity for a local single-owner tool, overridable with --by for a
    # shared/multi-user setup.
    approved_by = args.by or getpass.getuser()
    service.approve(conn, args.message_id, now, approved_by=approved_by)
    print(f"Approved {args.message_id} (by {approved_by}).")


def cmd_send(args):
    conn = _connect(args)
    now = _now(args)
    result = service.send(conn, args.message_id, now, channel=args.channel)
    if result.already_sent:
        print(f"{args.message_id} was already sent -- no-op.")
    else:
        print(f"Sent {args.message_id}.")


def cmd_outbox(args):
    conn = _connect(args)
    rows = conn.execute("SELECT * FROM outbox ORDER BY sent_at ASC").fetchall()
    if not rows:
        print("Outbox is empty.")
        return
    for row in rows:
        print(f"{row['sent_at']}  {row['quote_id']}  {row['customer_phone']}  "
              f"[{row['channel']}]  {row['body']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="followup", description="Follow-up engine CLI")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="path to the sqlite db file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="load quotes.json / events.jsonl into the db")
    p.add_argument("--quotes", default=str(DEFAULT_SEED_DIR / "quotes.json"))
    p.add_argument("--events", default=str(DEFAULT_SEED_DIR / "events.jsonl"))
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("candidates", help="show the prioritized follow-up list (read-only)")
    p.add_argument("--now", help="ISO-8601 timestamp, e.g. 2026-08-16T00:00:00Z (default: real current time)")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("draft", help="materialize drafts for current candidates")
    p.add_argument("--now", help="ISO-8601 timestamp (default: real current time)")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("drafts", help="list messages (optionally filtered by status)")
    p.add_argument("--status", choices=["draft", "approved", "sent", "failed"])
    p.set_defaults(func=cmd_drafts)

    p = sub.add_parser("approve", help="approve a draft message")
    p.add_argument("message_id")
    p.add_argument("--now", help="ISO-8601 timestamp (default: real current time)")
    p.add_argument("--by", help="identity of the approver (default: current OS user)")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("send", help="send an approved message (idempotent)")
    p.add_argument("message_id")
    p.add_argument("--now", help="ISO-8601 timestamp (default: real current time)")
    p.add_argument("--channel", default=None,
                    help="override the channel the draft was written for (default: use the draft's own channel)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("outbox", help="show everything that has actually been sent")
    p.set_defaults(func=cmd_outbox)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (service.GuardrailViolation, ValueError, ingest.IngestError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            print(f"error: database {args.db!r} is busy (another process is writing to it) -- try again: {e}",
                  file=sys.stderr)
        else:
            print(f"error: could not open database {args.db!r}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

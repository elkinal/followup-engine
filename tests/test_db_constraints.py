"""Tests that invalid states are rejected by the schema itself (triggers,
CHECK constraints, FKs, unique indexes) -- independent of whatever Python in
policy.py/service.py happens to check. These insert directly with raw SQL
to simulate a bug or a future code path that bypasses service.py.
"""
import sqlite3
import unittest

from tests.helpers import insert_quote, make_db


def insert_message(conn, **overrides):
    row = {
        "id": "m1", "quote_id": "Q-1", "customer_phone": "+1",
        "reason_code": "x", "reason": "r", "body": "b",
        "status": "draft", "created_at": "2026-08-16T00:00:00Z",
    }
    row.update(overrides)
    cols = ", ".join(row)
    placeholders = ", ".join(f":{k}" for k in row)
    conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", row)


class TestNonOpenQuoteGuardrails(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)

    def test_cannot_insert_a_draft_for_an_accepted_quote(self):
        insert_quote(self.conn, "Q-1", status="accepted")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_message(self.conn, status="draft")

    def test_cannot_insert_a_draft_for_a_dismissed_quote(self):
        insert_quote(self.conn, "Q-1", status="dismissed")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_message(self.conn, status="draft")

    def test_cannot_approve_a_message_whose_quote_is_no_longer_open(self):
        insert_quote(self.conn, "Q-1", status="open")
        insert_message(self.conn, status="draft")
        self.conn.execute("UPDATE quotes SET status = 'accepted' WHERE id = 'Q-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE messages SET status='approved', approved_at='2026-08-16T01:00:00Z', "
                "approved_by='brad' WHERE id='m1'"
            )

    def test_can_still_approve_while_quote_remains_open(self):
        insert_quote(self.conn, "Q-1", status="open")
        insert_message(self.conn, status="draft")
        self.conn.execute(
            "UPDATE messages SET status='approved', approved_at='2026-08-16T01:00:00Z', "
            "approved_by='brad' WHERE id='m1'"
        )  # must not raise
        status = self.conn.execute("SELECT status FROM messages WHERE id='m1'").fetchone()["status"]
        self.assertEqual(status, "approved")

    def test_cannot_write_outbox_row_for_a_non_open_quote(self):
        insert_quote(self.conn, "Q-1", status="open")
        insert_message(self.conn, status="approved", approved_at="2026-08-16T01:00:00Z", approved_by="brad")
        self.conn.execute("UPDATE quotes SET status = 'dismissed' WHERE id = 'Q-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO outbox (message_id, quote_id, customer_phone, channel, body, sent_at) "
                "VALUES ('m1', 'Q-1', '+1', 'sms', 'b', '2026-08-16T02:00:00Z')"
            )


class TestFieldLevelConstraints(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert_quote(self.conn, "Q-1", amount=-100)

    def test_unknown_quote_status_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO quotes (id, customer_name, customer_phone, tech_name, amount, status, created_at) "
                "VALUES ('Q-1', 'Ann', '+1', 'Brad', 100, 'pending', '2026-08-01T00:00:00Z')"
            )

    def test_event_for_a_nonexistent_quote_is_rejected_by_foreign_key(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO events (event_id, type, quote_id, timestamp, raw) "
                "VALUES ('e1', 'quote_viewed', 'Q-DOES-NOT-EXIST', '2026-08-01T00:00:00Z', '{}')"
            )

    def test_sent_message_without_sent_at_is_rejected(self):
        insert_quote(self.conn, "Q-1", status="open")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_message(self.conn, status="sent", approved_at="2026-08-16T00:00:00Z",
                            approved_by="brad", sent_at=None)

    def test_non_draft_message_without_approved_at_is_rejected(self):
        insert_quote(self.conn, "Q-1", status="open")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_message(self.conn, status="approved", approved_at=None, approved_by="brad")

    def test_non_draft_message_without_approved_by_is_rejected(self):
        insert_quote(self.conn, "Q-1", status="open")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_message(self.conn, status="approved", approved_at="2026-08-16T00:00:00Z", approved_by=None)

    def test_message_channel_must_be_a_known_channel(self):
        insert_quote(self.conn, "Q-1", status="open")
        with self.assertRaises(sqlite3.IntegrityError):
            insert_message(self.conn, channel="carrier_pigeon")


if __name__ == "__main__":
    unittest.main()

import sqlite3
import unittest

from followup_engine import service
from followup_engine.timeutil import parse_ts
from tests.helpers import insert_event, insert_quote, make_db


NOW = parse_ts("2026-08-16T00:00:00Z")


class TestDraftGuardrails(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)

    def test_cooldown_applies_across_a_customers_quotes_not_just_one(self):
        # Same phone number, two different open quotes -- mirrors the seed
        # data's Q-1003/Q-1025 (Karen Nguyen) situation.
        phone = "+19175552003"
        insert_quote(self.conn, "Q-A", customer_phone=phone, amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_quote(self.conn, "Q-B", customer_phone=phone, amount=10000, created_at="2026-07-01T00:00:00Z")

        result = service.create_drafts(self.conn, NOW)
        self.assertEqual(len(result.created), 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0][1], "cooldown")

    def test_different_customers_are_not_cooled_down_by_each_other(self):
        insert_quote(self.conn, "Q-A", customer_phone="+1111", amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_quote(self.conn, "Q-B", customer_phone="+2222", amount=10000, created_at="2026-07-01T00:00:00Z")
        result = service.create_drafts(self.conn, NOW)
        self.assertEqual(len(result.created), 2)

    def test_recent_outbound_message_blocks_a_new_draft(self):
        # Outbound sent 1 day ago (within the 48h cooldown), then a view 12h
        # ago that would otherwise independently qualify as a candidate --
        # the cooldown guardrail must still block drafting a new message.
        insert_quote(self.conn, "Q-A", customer_phone="+1111", amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "message_sent", "Q-A", "2026-08-15T00:00:00Z")
        insert_event(self.conn, "e2", "quote_viewed", "Q-A", "2026-08-15T12:00:00Z")
        result = service.create_drafts(self.conn, NOW)
        self.assertEqual(result.created, [])
        self.assertEqual(result.skipped, [("Q-A", "cooldown")])

    def test_rerunning_draft_generation_does_not_duplicate_open_drafts(self):
        insert_quote(self.conn, "Q-A", amount=10000, created_at="2026-07-01T00:00:00Z")
        r1 = service.create_drafts(self.conn, NOW)
        r2 = service.create_drafts(self.conn, NOW)
        self.assertEqual(len(r1.created), 1)
        self.assertEqual(r2.created, [])
        count = self.conn.execute("SELECT count(*) FROM messages WHERE quote_id = 'Q-A'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_db_constraint_rejects_a_second_open_message_for_the_same_quote(self):
        # Defense in depth: even if application logic were bypassed, the
        # schema itself refuses a second outstanding draft/approved message.
        insert_quote(self.conn, "Q-A", amount=10000, created_at="2026-07-01T00:00:00Z")
        self.conn.execute(
            "INSERT INTO messages (id, quote_id, customer_phone, reason_code, reason, body, status, created_at) "
            "VALUES ('m1', 'Q-A', '+1', 'stale_big', 'r', 'b', 'draft', '2026-08-16T00:00:00Z')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO messages (id, quote_id, customer_phone, reason_code, reason, body, status, created_at) "
                "VALUES ('m2', 'Q-A', '+1', 'stale_big', 'r', 'b', 'approved', '2026-08-16T00:00:00Z')"
            )


class TestApproveSendGuardrails(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)
        insert_quote(self.conn, "Q-A", amount=10000, created_at="2026-07-01T00:00:00Z")
        result = service.create_drafts(self.conn, NOW)
        self.message_id = result.created[0]

    def test_cannot_send_a_message_that_has_not_been_approved(self):
        with self.assertRaises(service.GuardrailViolation):
            service.send(self.conn, self.message_id, NOW)

    def test_send_is_idempotent_under_retry(self):
        service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        r1 = service.send(self.conn, self.message_id, NOW)
        r2 = service.send(self.conn, self.message_id, NOW)
        self.assertFalse(r1.already_sent)
        self.assertTrue(r2.already_sent)
        count = self.conn.execute(
            "SELECT count(*) FROM outbox WHERE message_id = ?", (self.message_id,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_send_refuses_a_quote_that_became_accepted_after_approval(self):
        service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        self.conn.execute("UPDATE quotes SET status = 'accepted' WHERE id = 'Q-A'")
        self.conn.commit()
        with self.assertRaises(service.GuardrailViolation):
            service.send(self.conn, self.message_id, NOW)
        count = self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(count, 0)

    def test_cannot_approve_an_already_sent_message_again(self):
        service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        service.send(self.conn, self.message_id, NOW)
        with self.assertRaises(service.GuardrailViolation):
            service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")

    def test_approve_requires_an_approver_identity(self):
        with self.assertRaises(ValueError):
            service.approve(self.conn, self.message_id, NOW, approved_by="")
        with self.assertRaises(ValueError):
            service.approve(self.conn, self.message_id, NOW, approved_by="   ")

    def test_approver_identity_is_recorded(self):
        service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        row = self.conn.execute("SELECT approved_by FROM messages WHERE id = ?", (self.message_id,)).fetchone()
        self.assertEqual(row["approved_by"], "brad@example.com")

    def test_send_defaults_to_the_channel_the_draft_was_written_for(self):
        stored_channel = self.conn.execute(
            "SELECT channel FROM messages WHERE id = ?", (self.message_id,)
        ).fetchone()["channel"]
        service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        service.send(self.conn, self.message_id, NOW)  # no channel override
        outbox_channel = self.conn.execute(
            "SELECT channel FROM outbox WHERE message_id = ?", (self.message_id,)
        ).fetchone()["channel"]
        self.assertEqual(outbox_channel, stored_channel)

    def test_send_channel_can_be_explicitly_overridden(self):
        service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        service.send(self.conn, self.message_id, NOW, channel="email")
        outbox_channel = self.conn.execute(
            "SELECT channel FROM outbox WHERE message_id = ?", (self.message_id,)
        ).fetchone()["channel"]
        self.assertEqual(outbox_channel, "email")


if __name__ == "__main__":
    unittest.main()

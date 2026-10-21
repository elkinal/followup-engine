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
        # The specific reason matters to a human reading the skip list: this
        # quote has its own open draft, which is a more useful thing to say
        # than the generic "cooldown" (regression check -- see the test
        # below for why these two reasons must stay distinguishable).
        self.assertEqual(r2.skipped, [("Q-A", "already has an open draft")])

    def test_separate_draft_runs_cannot_create_two_open_drafts_for_the_same_customer(self):
        # This is the real bug this test is pinned to: found by actually
        # running the CLI against the full seed data twice, five minutes
        # apart -- Q-1003 and Q-1025 (Karen Nguyen, same phone) each
        # independently qualified as a candidate; the first run correctly
        # drafted one and skipped the other for cooldown, but a *second*,
        # separate create_drafts() call (simulating a re-run before the
        # first batch was approved) drafted the *other* one too, because
        # cooldown was only computed from actual sends + same-batch drafts,
        # never from a different run's still-open draft. Approving and
        # sending both then produced two real outbound messages to the same
        # customer a minute apart -- confirmed via the actual CLI before
        # this fix.
        phone = "+19175552003"
        insert_quote(self.conn, "Q-A", customer_phone=phone, amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_quote(self.conn, "Q-B", customer_phone=phone, amount=10000, created_at="2026-07-01T00:00:00Z")

        r1 = service.create_drafts(self.conn, NOW)
        self.assertEqual(len(r1.created), 1)

        later = parse_ts("2026-08-16T00:05:00Z")  # well within the 48h cooldown
        r2 = service.create_drafts(self.conn, later)
        self.assertEqual(r2.created, [], "a second run must not draft the other quote for the same customer")

        open_messages = self.conn.execute(
            "SELECT count(*) FROM messages WHERE status IN ('draft', 'approved')"
        ).fetchone()[0]
        self.assertEqual(open_messages, 1)

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

    def test_approve_refuses_a_quote_that_closed_between_draft_and_approval(self):
        # Different race than test_send_refuses_a_quote_that_became_accepted_
        # after_approval: here the quote closes *before* approval is ever
        # attempted, which exercises the block_approve_for_non_open_quote
        # trigger inside approve() itself (service.py's own except clause
        # that converts the raw IntegrityError into a clean
        # GuardrailViolation) rather than the Python-level check in send().
        self.conn.execute("UPDATE quotes SET status = 'dismissed' WHERE id = 'Q-A'")
        self.conn.commit()
        with self.assertRaises(service.GuardrailViolation):
            service.approve(self.conn, self.message_id, NOW, approved_by="brad@example.com")
        # must not be half-applied: still a plain draft, not stuck in limbo
        row = self.conn.execute("SELECT status, approved_by FROM messages WHERE id = ?", (self.message_id,)).fetchone()
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(row["approved_by"])

    def test_sending_a_nonexistent_message_raises_value_error(self):
        with self.assertRaises(ValueError):
            service.send(self.conn, "no-such-message-id", NOW)
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


class TestCooldownIsAuthoritativeAtSendTime(unittest.TestCase):
    """create_drafts() now prevents a second open draft to the same phone
    (see TestDraftGuardrails), but send() must independently refuse to
    double-message a customer too -- it's the function that actually
    produces the real side effect, so it can't rely on draft-time
    prevention alone ever having run. These tests insert two open messages
    for the same customer directly, bypassing create_drafts() entirely, to
    prove the guardrail holds at the one place that actually matters."""

    def setUp(self):
        self.conn = make_db(self)
        self.phone = "+19175552003"
        insert_quote(self.conn, "Q-A", customer_phone=self.phone, amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_quote(self.conn, "Q-B", customer_phone=self.phone, amount=10000, created_at="2026-07-01T00:00:00Z")
        for mid, qid in [("m-a", "Q-A"), ("m-b", "Q-B")]:
            self.conn.execute(
                "INSERT INTO messages (id, quote_id, customer_phone, reason_code, reason, body, "
                "channel, status, created_at) VALUES (?, ?, ?, 'x', 'r', 'b', 'sms', 'draft', ?)",
                (mid, qid, self.phone, "2026-08-16T00:00:00Z"),
            )
        self.conn.commit()
        service.approve(self.conn, "m-a", NOW, approved_by="brad")
        service.approve(self.conn, "m-b", NOW, approved_by="brad")

    def test_second_send_to_the_same_customer_within_cooldown_is_refused(self):
        service.send(self.conn, "m-a", NOW)
        with self.assertRaises(service.GuardrailViolation):
            service.send(self.conn, "m-b", parse_ts("2026-08-16T00:01:00Z"))
        count = self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(count, 1, "only the first send should have actually gone out")

    def test_refused_send_is_marked_failed_not_left_dangling(self):
        service.send(self.conn, "m-a", NOW)
        with self.assertRaises(service.GuardrailViolation):
            service.send(self.conn, "m-b", parse_ts("2026-08-16T00:01:00Z"))
        status = self.conn.execute("SELECT status FROM messages WHERE id = 'm-b'").fetchone()["status"]
        self.assertEqual(status, "failed")

    def test_send_after_cooldown_expires_is_allowed(self):
        service.send(self.conn, "m-a", NOW)
        later = parse_ts("2026-08-18T01:00:00Z")  # > 48h after the first send
        result = service.send(self.conn, "m-b", later)
        self.assertFalse(result.already_sent)
        count = self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()

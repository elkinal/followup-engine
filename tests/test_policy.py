import unittest

from followup_engine import policy
from followup_engine.timeutil import parse_ts
from tests.helpers import insert_event, insert_quote, make_db


NOW = parse_ts("2026-08-16T00:00:00Z")


def reason_codes(conn, now=NOW):
    return {c.quote_id: c.reason_code for c in policy.get_candidates(conn, now)}


class TestPolicyRules(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)

    def test_accepted_and_dismissed_quotes_are_never_candidates(self):
        insert_quote(self.conn, "Q-A", status="accepted", created_at="2026-08-01T00:00:00Z")
        insert_quote(self.conn, "Q-D", status="dismissed", created_at="2026-08-01T00:00:00Z")
        codes = reason_codes(self.conn)
        self.assertNotIn("Q-A", codes)
        self.assertNotIn("Q-D", codes)

    def test_brand_new_quote_with_no_activity_is_not_yet_a_candidate(self):
        insert_quote(self.conn, "Q-NEW", created_at="2026-08-15T12:00:00Z")  # 12h old
        self.assertNotIn("Q-NEW", reason_codes(self.conn))

    def test_reply_pending_outranks_everything_else(self):
        insert_quote(self.conn, "Q-1", amount=50000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "customer_replied", "Q-1", "2026-08-15T00:00:00Z")
        candidates = policy.get_candidates(self.conn, NOW)
        self.assertEqual(candidates[0].quote_id, "Q-1")
        self.assertEqual(candidates[0].reason_code, "reply_pending")

    def test_reply_followed_by_our_outbound_is_not_reply_pending(self):
        insert_quote(self.conn, "Q-1", created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "customer_replied", "Q-1", "2026-08-10T00:00:00Z")
        insert_event(self.conn, "e2", "message_sent", "Q-1", "2026-08-11T00:00:00Z")
        self.assertNotIn("Q-1", reason_codes(self.conn))

    def test_viewed_recently_no_reply_is_flagged(self):
        insert_quote(self.conn, "Q-1", created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_viewed", "Q-1", "2026-08-15T12:00:00Z")  # 12h before NOW
        self.assertEqual(reason_codes(self.conn)["Q-1"], "viewed_no_response")

    def test_viewed_long_ago_falls_back_to_stale_rules(self):
        insert_quote(self.conn, "Q-1", amount=200, created_at="2026-08-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_viewed", "Q-1", "2026-08-02T00:00:00Z")  # way outside the 72h window
        self.assertEqual(reason_codes(self.conn)["Q-1"], "stale_general")

    def test_big_quote_stale_threshold_is_shorter_than_general(self):
        insert_quote(self.conn, "Q-BIG", amount=10000, created_at="2026-08-11T00:00:00Z")  # 5 days old
        insert_quote(self.conn, "Q-SMALL", amount=200, created_at="2026-08-11T00:00:00Z")
        codes = reason_codes(self.conn)
        self.assertEqual(codes["Q-BIG"], "stale_big")
        self.assertNotIn("Q-SMALL", codes)  # 5 days < 7-day general threshold

    def test_quote_viewed_is_not_a_contact_for_cooldown_purposes(self):
        # Viewed 10 days ago but never messaged/replied to -> still "no contact",
        # should eventually surface as stale, not be silently satisfied by the view.
        insert_quote(self.conn, "Q-1", amount=10000, created_at="2026-08-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_viewed", "Q-1", "2026-08-06T00:00:00Z")
        self.assertEqual(reason_codes(self.conn)["Q-1"], "stale_big")

    def test_events_are_evaluated_by_timestamp_not_file_order(self):
        # Insert the *earlier* view after the *later* one, simulating an
        # out-of-order webhook stream. The later view should still win.
        insert_quote(self.conn, "Q-1", created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e_late", "quote_viewed", "Q-1", "2026-08-15T12:00:00Z")
        insert_event(self.conn, "e_early", "quote_viewed", "Q-1", "2026-08-01T00:00:00Z")
        sig = policy.compute_signals(self.conn, NOW)["Q-1"]
        self.assertEqual(sig.last_viewed_at, parse_ts("2026-08-15T12:00:00Z"))

    def test_future_events_relative_to_now_are_ignored(self):
        insert_quote(self.conn, "Q-1", created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_viewed", "Q-1", "2026-08-20T00:00:00Z")  # after NOW
        sig = policy.compute_signals(self.conn, NOW).get("Q-1", policy.QuoteSignals())
        self.assertIsNone(sig.last_viewed_at)
        self.assertEqual(reason_codes(self.conn)["Q-1"], "stale_general")


if __name__ == "__main__":
    unittest.main()

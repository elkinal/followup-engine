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

    def test_full_priority_order_across_all_four_tiers(self):
        # One quote per tier, plus a decoy that shouldn't qualify at all.
        # This pins down the *final sorted order*, not just which rule fires
        # for each quote in isolation -- a sort-key regression (e.g. a sign
        # flip) would only be caught by an assertion like this one.
        insert_quote(self.conn, "Q-REPLY", amount=1000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "customer_replied", "Q-REPLY", "2026-08-15T00:00:00Z")

        insert_quote(self.conn, "Q-VIEW", amount=1000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e2", "quote_viewed", "Q-VIEW", "2026-08-15T12:00:00Z")

        insert_quote(self.conn, "Q-BIG", amount=10000, created_at="2026-08-10T00:00:00Z")

        insert_quote(self.conn, "Q-GEN", amount=200, created_at="2026-08-05T00:00:00Z")

        insert_quote(self.conn, "Q-TOO-NEW", amount=10000, created_at="2026-08-15T18:00:00Z")

        ordered_ids = [c.quote_id for c in policy.get_candidates(self.conn, NOW)]
        self.assertEqual(ordered_ids, ["Q-REPLY", "Q-VIEW", "Q-BIG", "Q-GEN"])
        self.assertNotIn("Q-TOO-NEW", ordered_ids)

    def test_more_recently_viewed_quote_sorts_before_an_older_view(self):
        insert_quote(self.conn, "Q-OLDER-VIEW", created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_viewed", "Q-OLDER-VIEW", "2026-08-14T00:00:00Z")  # ~2 days ago
        insert_quote(self.conn, "Q-NEWER-VIEW", created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e2", "quote_viewed", "Q-NEWER-VIEW", "2026-08-15T18:00:00Z")  # 6h ago

        ordered_ids = [c.quote_id for c in policy.get_candidates(self.conn, NOW)]
        self.assertEqual(ordered_ids.index("Q-NEWER-VIEW"), 0)
        self.assertEqual(ordered_ids.index("Q-OLDER-VIEW"), 1)

    def test_stale_general_boundary_is_inclusive(self):
        insert_quote(self.conn, "Q-EXACT", amount=100, created_at="2026-08-09T00:00:00Z")  # exactly 7d before NOW
        insert_quote(self.conn, "Q-JUST-UNDER", amount=100, created_at="2026-08-09T00:00:01Z")  # 1s short of 7d
        codes = reason_codes(self.conn)
        self.assertEqual(codes["Q-EXACT"], "stale_general")
        self.assertNotIn("Q-JUST-UNDER", codes)


def channels(conn, now=NOW):
    return {c.quote_id: c.channel for c in policy.get_candidates(conn, now)}


class TestPreferredChannel(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)

    def test_defaults_to_sms_with_no_history(self):
        insert_quote(self.conn, "Q-1", amount=10000, created_at="2026-08-01T00:00:00Z")
        self.assertEqual(channels(self.conn)["Q-1"], "sms")

    def test_uses_the_channel_the_customer_last_replied_on(self):
        insert_quote(self.conn, "Q-1", amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "message_sent", "Q-1", "2026-07-02T00:00:00Z", channel="sms")
        insert_event(self.conn, "e2", "customer_replied", "Q-1", "2026-08-15T00:00:00Z", channel="email")
        self.assertEqual(channels(self.conn)["Q-1"], "email")

    def test_reply_channel_wins_even_if_it_is_older_than_a_later_outbound_send(self):
        insert_quote(self.conn, "Q-1", amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "customer_replied", "Q-1", "2026-07-05T00:00:00Z", channel="email")
        insert_event(self.conn, "e2", "message_sent", "Q-1", "2026-07-10T00:00:00Z", channel="sms")
        self.assertEqual(channels(self.conn)["Q-1"], "email")

    def test_falls_back_to_last_outbound_channel_when_no_reply_exists(self):
        insert_quote(self.conn, "Q-1", amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "message_sent", "Q-1", "2026-07-02T00:00:00Z", channel="email")
        self.assertEqual(channels(self.conn)["Q-1"], "email")

    def test_unrecognized_channel_value_falls_back_to_default(self):
        insert_quote(self.conn, "Q-1", amount=10000, created_at="2026-07-01T00:00:00Z")
        insert_event(self.conn, "e1", "message_sent", "Q-1", "2026-07-02T00:00:00Z", channel="carrier_pigeon")
        self.assertEqual(channels(self.conn)["Q-1"], "sms")


class TestQuoteAcceptedViaEvents(unittest.TestCase):
    """Regression tests for a real gap found by code review: quotes.json's
    `status` field is a batch/snapshot source that can lag the (closer to
    real-time) event stream. A `quote_accepted` event -- a type the seed
    data's own event stream names explicitly -- was parsed and stored by
    ingest.py but never consulted by policy.py, so a quote the event stream
    says was accepted could still surface as, and be sent, a follow-up."""

    def setUp(self):
        self.conn = make_db(self)

    def test_quote_with_an_accepted_event_is_excluded_even_though_status_still_says_open(self):
        insert_quote(self.conn, "Q-1", status="open", amount=10000, created_at="2026-08-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_accepted", "Q-1", "2026-08-14T00:00:00Z")
        self.assertNotIn("Q-1", reason_codes(self.conn))

    def test_accepted_event_in_the_future_relative_to_now_does_not_exclude_it_yet(self):
        insert_quote(self.conn, "Q-1", status="open", amount=10000, created_at="2026-08-01T00:00:00Z")
        insert_event(self.conn, "e1", "quote_accepted", "Q-1", "2026-08-20T00:00:00Z")  # after NOW
        self.assertIn("Q-1", reason_codes(self.conn))

    def test_helper_function_directly(self):
        insert_quote(self.conn, "Q-1", status="open", created_at="2026-08-01T00:00:00Z")
        self.assertFalse(policy.quote_accepted_via_events(self.conn, "Q-1", NOW))
        insert_event(self.conn, "e1", "quote_accepted", "Q-1", "2026-08-14T00:00:00Z")
        self.assertTrue(policy.quote_accepted_via_events(self.conn, "Q-1", NOW))


if __name__ == "__main__":
    unittest.main()

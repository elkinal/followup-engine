import unittest

from followup_engine.drafting import draft_message
from followup_engine.policy import Candidate


def make_candidate(**overrides):
    row = dict(
        quote_id="Q-1", customer_name="Ann Smith", customer_phone="+1",
        tech_name="Brad", amount=1000, tier=10, reason_code="stale_general",
        reason="r", channel="sms", sort_key=(),
    )
    row.update(overrides)
    return Candidate(**row)


class TestDrafting(unittest.TestCase):
    def test_uses_first_name_only(self):
        body = draft_message(make_candidate(customer_name="Ann Smith"))
        self.assertIn("Ann", body)
        self.assertNotIn("Smith", body)

    def test_blank_name_does_not_crash(self):
        body = draft_message(make_candidate(customer_name="   "))
        self.assertIn("there", body)

    def test_each_reason_code_has_a_distinct_template(self):
        bodies = {
            code: draft_message(make_candidate(reason_code=code))
            for code in ["reply_pending", "viewed_no_response", "stale_big", "stale_general"]
        }
        self.assertEqual(len(set(bodies.values())), 4)

    def test_unknown_reason_code_falls_back_to_default_template(self):
        body = draft_message(make_candidate(reason_code="something_new"))
        self.assertIn("Ann", body)
        self.assertIn("$1,000", body)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from followup_engine import ingest
from tests.helpers import make_db


QUOTE = {
    "id": "Q-1", "customer_name": "Ann", "customer_phone": "+19175551234",
    "tech_name": "Brad", "amount": 1000, "status": "open",
    "created_at": "2026-08-01T00:00:00Z", "last_contact_at": None,
}


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.quotes_path = self.tmp / "quotes.json"
        self.events_path = self.tmp / "events.jsonl"

    def _write(self, quotes, event_lines):
        self.quotes_path.write_text(json.dumps(quotes))
        self.events_path.write_text("\n".join(event_lines))

    def test_duplicate_event_ids_are_deduped(self):
        line = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [line, line, line])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_inserted, 1)
        self.assertEqual(report.events_duplicate, 2)
        count = self.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(count, 1)

    def test_reingest_is_idempotent(self):
        line = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [line])
        ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        report2 = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report2.events_inserted, 0)
        self.assertEqual(report2.events_duplicate, 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM quotes").fetchone()[0], 1)

    def test_quote_status_is_upserted_not_duplicated(self):
        self._write([QUOTE], [])
        report1 = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        updated = {**QUOTE, "status": "accepted"}
        self._write([updated], [])
        report2 = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        rows = self.conn.execute("SELECT status FROM quotes WHERE id = 'Q-1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "accepted")
        # Updating a quote across separate ingest runs is normal upsert
        # behavior, not a collision -- must not be flagged as a warning.
        self.assertEqual(report1.warnings, [])
        self.assertEqual(report2.warnings, [])

    def test_duplicate_quote_id_within_one_file_is_flagged_not_silent(self):
        clobbered = {**QUOTE, "amount": 1}
        winner = {**QUOTE, "amount": 2}
        self._write([clobbered, winner], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("Q-1", report.warnings[0])
        self.assertIn("more than once", report.warnings[0])
        # last-write-wins is still the applied behavior -- just no longer silent
        amount = self.conn.execute("SELECT amount FROM quotes WHERE id = 'Q-1'").fetchone()[0]
        self.assertEqual(amount, 2)

    def test_malformed_json_line_is_skipped_not_fatal(self):
        good = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [good, "{not valid json", ""])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_inserted, 1)
        self.assertEqual(len(report.events_skipped), 1)

    def test_event_for_unknown_quote_is_skipped_not_fatal(self):
        bad = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-999", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [bad])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_inserted, 0)
        self.assertEqual(len(report.events_skipped), 1)
        self.assertIn("unknown quote_id", report.events_skipped[0])

    def test_non_z_timestamps_are_normalized_so_string_comparison_still_works(self):
        # 2026-08-15T10:00:00+08:00 is the same instant as 2026-08-15T02:00:00Z.
        # Every timestamp comparison downstream (policy.py) is a raw SQL
        # string comparison, so storing this un-normalized would silently
        # corrupt chronological ordering even though the value is valid.
        line = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-15T10:00:00+08:00"})
        self._write([QUOTE], [line])
        ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        stored = self.conn.execute("SELECT timestamp FROM events").fetchone()[0]
        self.assertEqual(stored, "2026-08-15T02:00:00Z")

    def test_quote_created_at_is_also_normalized(self):
        offset_quote = {**QUOTE, "created_at": "2026-08-01T20:00:00-04:00"}  # == 2026-08-02T00:00:00Z
        self._write([offset_quote], [])
        ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        stored = self.conn.execute("SELECT created_at FROM quotes WHERE id='Q-1'").fetchone()[0]
        self.assertEqual(stored, "2026-08-02T00:00:00Z")

    def test_event_id_reused_for_different_content_is_flagged_not_silently_dropped(self):
        first = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        conflicting = json.dumps({"event_id": "e1", "type": "customer_replied", "quote_id": "Q-1", "timestamp": "2026-08-10T00:00:00Z"})
        self._write([QUOTE], [first, conflicting])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_inserted, 1)
        self.assertEqual(report.events_duplicate, 0)  # not a true duplicate -- must not be miscounted as one
        self.assertEqual(len(report.events_skipped), 1)
        self.assertIn("reused for different content", report.events_skipped[0])

    def test_byte_identical_redelivery_still_counts_as_a_plain_duplicate(self):
        line = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [line, line])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_duplicate, 1)
        self.assertEqual(report.events_skipped, [])

    def test_missing_quotes_file_raises_ingest_error_not_a_crash(self):
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_all(self.conn, self.tmp / "nope.json", self.events_path)

    def test_non_list_top_level_json_raises_ingest_error(self):
        self.quotes_path.write_text(json.dumps({"id": "Q-1"}))
        self.events_path.write_text("")
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_all(self.conn, self.quotes_path, self.events_path)

    def test_differently_formatted_phone_numbers_for_the_same_customer_are_normalized_to_match(self):
        # Same real person, two different string spellings -- the guardrail
        # that keys on customer_phone (cross-quote cooldown) only works if
        # these resolve to the same stored value.
        q1 = {**QUOTE, "id": "Q-1", "customer_phone": "+19175552003"}
        q2 = {**QUOTE, "id": "Q-2", "customer_phone": "(917) 555-2003"}
        self._write([q1, q2], [])
        ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        phones = {
            row["id"]: row["customer_phone"]
            for row in self.conn.execute("SELECT id, customer_phone FROM quotes")
        }
        self.assertEqual(phones["Q-1"], phones["Q-2"])

    def test_unparseable_phone_number_is_skipped_not_fatal(self):
        bad = {**QUOTE, "customer_phone": "abc"}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)
        self.assertEqual(report.quotes_upserted, 0)

    def test_quote_with_blank_id_is_skipped(self):
        bad = {**QUOTE, "id": "   "}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_blank_customer_name_is_skipped(self):
        bad = {**QUOTE, "customer_name": "   "}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_negative_amount_is_skipped_through_the_real_pipeline(self):
        bad = {**QUOTE, "amount": -100}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_boolean_amount_is_skipped(self):
        # JSON true/false parse to Python bool, which is a subclass of int --
        # would silently pass an isinstance(x, (int, float)) check and get
        # stored as a nonsense amount unless explicitly excluded.
        bad = {**QUOTE, "amount": True}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_non_numeric_amount_is_skipped(self):
        bad = {**QUOTE, "amount": "a lot"}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_event_with_null_timestamp_is_skipped_not_fatal(self):
        # Regression test for a real bug: parse_ts(None) used to raise
        # AttributeError (not TypeError/ValueError), which slipped past this
        # exact except clause and crashed the whole ingest. Confirmed fixed
        # manually earlier; this locks it in permanently.
        bad = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": None})
        self._write([QUOTE], [bad])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_inserted, 0)
        self.assertEqual(len(report.events_skipped), 1)
        self.assertIn("unparseable timestamp", report.events_skipped[0])

    def test_event_with_garbage_timestamp_string_is_skipped_not_fatal(self):
        bad = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "not-a-date"})
        self._write([QUOTE], [bad])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(len(report.events_skipped), 1)

    def test_missing_events_file_raises_ingest_error_not_a_crash(self):
        self._write([QUOTE], [])  # quotes.json exists and is valid
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_all(self.conn, self.quotes_path, self.tmp / "nope.jsonl")

    def test_quotes_file_with_invalid_json_syntax_raises_ingest_error(self):
        self.quotes_path.write_text("{not valid json")
        self.events_path.write_text("")
        with self.assertRaises(ingest.IngestError):
            ingest.ingest_all(self.conn, self.quotes_path, self.events_path)

    def test_quote_missing_a_required_field_entirely_is_skipped(self):
        incomplete = {k: v for k, v in QUOTE.items() if k != "tech_name"}
        self._write([incomplete], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)
        self.assertEqual(report.quotes_upserted, 0)

    def test_quote_with_blank_tech_name_is_skipped(self):
        bad = {**QUOTE, "tech_name": "   "}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_unknown_status_is_skipped_through_the_real_pipeline(self):
        bad = {**QUOTE, "status": "pending"}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_garbage_created_at_is_skipped(self):
        bad = {**QUOTE, "created_at": "not-a-date"}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_quote_with_garbage_last_contact_at_is_skipped(self):
        bad = {**QUOTE, "last_contact_at": "not-a-date"}
        self._write([bad], [])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.quotes_skipped, 1)

    def test_event_missing_a_required_field_entirely_is_skipped(self):
        incomplete = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1"})  # no timestamp
        self._write([QUOTE], [incomplete])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(len(report.events_skipped), 1)

    def test_event_with_blank_event_id_is_skipped(self):
        bad = json.dumps({"event_id": "", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [bad])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(len(report.events_skipped), 1)

    def test_event_with_blank_type_is_skipped(self):
        bad = json.dumps({"event_id": "e1", "type": "", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self._write([QUOTE], [bad])
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(len(report.events_skipped), 1)

    def test_blank_lines_in_events_file_are_skipped_silently(self):
        line = json.dumps({"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-02T00:00:00Z"})
        self.quotes_path.write_text(json.dumps([QUOTE]))
        self.events_path.write_text(f"\n\n{line}\n\n")  # real blank lines, not just a trailing newline
        report = ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        self.assertEqual(report.events_inserted, 1)
        self.assertEqual(report.events_skipped, [])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from followup_engine import ingest
from tests.helpers import make_db


QUOTE = {
    "id": "Q-1", "customer_name": "Ann", "customer_phone": "+1555",
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
        ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        updated = {**QUOTE, "status": "accepted"}
        self._write([updated], [])
        ingest.ingest_all(self.conn, self.quotes_path, self.events_path)
        rows = self.conn.execute("SELECT status FROM quotes WHERE id = 'Q-1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "accepted")

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


if __name__ == "__main__":
    unittest.main()

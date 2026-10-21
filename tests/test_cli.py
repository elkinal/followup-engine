import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from followup_engine import cli


def run(argv):
    """Run the CLI in-process and capture stdout/stderr + exit code."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
        except SystemExit as e:
            code = e.code or 0
    return code, out.getvalue(), err.getvalue()


class TestCliErrorHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "test.db")

    def test_missing_quotes_file_is_a_clean_error_not_a_traceback(self):
        code, out, err = run(["--db", self.db, "ingest", "--quotes", str(self.tmp / "nope.json")])
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        self.assertNotIn("Traceback", err)

    def test_bad_db_path_is_a_clean_error_not_a_traceback(self):
        bad_db = str(self.tmp / "no" / "such" / "dir" / "db.sqlite")
        code, out, err = run(["--db", bad_db, "ingest"])
        self.assertEqual(code, 1)
        self.assertIn("could not open database", err)
        self.assertNotIn("Traceback", err)

    def test_non_list_top_level_json_is_a_clean_error(self):
        quotes_path = self.tmp / "quotes.json"
        quotes_path.write_text(json.dumps({"not": "a list"}))
        events_path = self.tmp / "events.jsonl"
        events_path.write_text("")
        code, out, err = run(["--db", self.db, "ingest", "--quotes", str(quotes_path), "--events", str(events_path)])
        self.assertEqual(code, 1)
        self.assertIn("JSON array", err)

    def test_approving_a_nonexistent_message_is_a_clean_error(self):
        code, out, err = run(["--db", self.db, "approve", "no-such-id", "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("no such message", err)

    def test_sending_a_nonexistent_message_is_a_clean_error(self):
        code, out, err = run(["--db", self.db, "send", "no-such-id", "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 1)
        self.assertIn("no such message", err)


class TestCliEmptyState(unittest.TestCase):
    """The CLI's read commands (candidates/drafts/outbox), run against a
    freshly-ingested db with nothing else having happened yet -- these were
    never exercised by any test before, even though they're three of the
    seven CLI commands."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "test.db")
        quotes_path = self.tmp / "quotes.json"
        events_path = self.tmp / "events.jsonl"
        # Created "now" (per --now below), so nothing is stale enough yet to
        # be a candidate -- this is what exercises the "no candidates" path.
        quotes_path.write_text(json.dumps([{
            "id": "Q-NEW", "customer_name": "Ann", "customer_phone": "+19175551234",
            "tech_name": "Brad", "amount": 1000, "status": "open",
            "created_at": "2026-08-16T00:00:00Z", "last_contact_at": None,
        }]))
        events_path.write_text("")
        run(["--db", self.db, "ingest", "--quotes", str(quotes_path), "--events", str(events_path)])

    def test_candidates_with_nothing_due_yet(self):
        code, out, _ = run(["--db", self.db, "candidates", "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("No follow-up candidates", out)

    def test_drafts_with_none_created_yet(self):
        code, out, _ = run(["--db", self.db, "drafts"])
        self.assertEqual(code, 0)
        self.assertIn("No messages", out)

    def test_outbox_with_nothing_sent_yet(self):
        code, out, _ = run(["--db", self.db, "outbox"])
        self.assertEqual(code, 0)
        self.assertIn("Outbox is empty", out)

    def test_now_defaults_to_the_real_current_time_when_omitted(self):
        # Doesn't assert a specific value (that would be flaky by
        # definition) -- just proves the real-clock default path runs
        # without error, since every other test always passes --now
        # explicitly and never exercises this branch.
        code, out, _ = run(["--db", self.db, "candidates"])
        self.assertEqual(code, 0)


class TestCliHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "test.db")
        self.quotes_path = self.tmp / "quotes.json"
        self.events_path = self.tmp / "events.jsonl"
        self.quotes_path.write_text(json.dumps([{
            "id": "Q-1", "customer_name": "Ann", "customer_phone": "+19175551234",
            "tech_name": "Brad", "amount": 10000, "status": "open",
            "created_at": "2026-07-01T00:00:00Z", "last_contact_at": None,
        }]))
        self.events_path.write_text("")
        run(["--db", self.db, "ingest", "--quotes", str(self.quotes_path), "--events", str(self.events_path)])

    def test_full_flow_end_to_end_through_the_cli(self):
        code, out, _ = run(["--db", self.db, "draft", "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("Created 1 draft(s).", out)

        conn = sqlite3.connect(self.db)
        message_id = conn.execute("SELECT id FROM messages WHERE status = 'draft'").fetchone()[0]
        conn.close()

        code, _, _ = run(["--db", self.db, "approve", message_id, "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 0)

        code, out, _ = run(["--db", self.db, "send", message_id, "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("Sent", out)

        code, out, _ = run(["--db", self.db, "outbox"])
        self.assertIn("Q-1", out)

        # retry: must be a clean no-op, not an error
        code, out, _ = run(["--db", self.db, "send", message_id, "--now", "2026-08-16T00:01:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("already sent", out)

        # The quote we just contacted must drop out of the candidate list --
        # this is the actual end-to-end proof that "we already reached this
        # customer" is reflected back into the policy, not just that the
        # outbox has one row in it.
        code, out, _ = run(["--db", self.db, "candidates", "--now", "2026-08-16T00:01:00Z"])
        self.assertIn("No follow-up candidates", out)

    def test_candidates_shows_the_prioritized_list(self):
        code, out, _ = run(["--db", self.db, "candidates", "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("Q-1", out)
        self.assertIn("stale_big", out)

    def test_drafts_lists_and_filters_by_status(self):
        run(["--db", self.db, "draft", "--now", "2026-08-16T00:00:00Z"])

        code, out, _ = run(["--db", self.db, "drafts"])
        self.assertIn("Q-1", out)
        self.assertIn("[draft", out)

        code, out, _ = run(["--db", self.db, "drafts", "--status", "sent"])
        self.assertIn("No messages", out)  # nothing's been sent yet


class TestCliDraftSkipOutput(unittest.TestCase):
    """Exercises cmd_draft's 'Skipped N:' branch, which the main happy-path
    test never triggers since it only ever has one eligible quote."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "test.db")
        quotes_path = self.tmp / "quotes.json"
        events_path = self.tmp / "events.jsonl"
        # Two quotes, same customer phone, both independently stale enough
        # to be candidates -- the second must be skipped for cooldown.
        quotes_path.write_text(json.dumps([
            {"id": "Q-A", "customer_name": "Ann", "customer_phone": "+19175551234",
             "tech_name": "Brad", "amount": 10000, "status": "open",
             "created_at": "2026-07-01T00:00:00Z", "last_contact_at": None},
            {"id": "Q-B", "customer_name": "Ann", "customer_phone": "+19175551234",
             "tech_name": "Brad", "amount": 10000, "status": "open",
             "created_at": "2026-07-01T00:00:00Z", "last_contact_at": None},
        ]))
        events_path.write_text("")
        run(["--db", self.db, "ingest", "--quotes", str(quotes_path), "--events", str(events_path)])

    def test_cooldown_skip_is_reported_in_cli_output(self):
        code, out, _ = run(["--db", self.db, "draft", "--now", "2026-08-16T00:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("Created 1 draft(s).", out)
        self.assertIn("Skipped 1:", out)
        self.assertIn("cooldown", out)


if __name__ == "__main__":
    unittest.main()

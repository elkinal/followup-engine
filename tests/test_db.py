import tempfile
import unittest
from pathlib import Path

from followup_engine import db


class TestConnectionPragmas(unittest.TestCase):
    def test_busy_timeout_is_set_so_a_contended_writer_retries_instead_of_failing_immediately(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_wal_mode_is_requested_for_a_real_file_db(self):
        # WAL lets readers (candidates/drafts/outbox) proceed without
        # blocking on a concurrent writer -- :memory: databases silently
        # ignore the request and stay in 'memory' mode, so this needs an
        # actual file to verify the pragma took effect.
        path = Path(tempfile.mkdtemp()) / "test.db"
        conn = db.connect(str(path))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_foreign_keys_are_enforced(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()

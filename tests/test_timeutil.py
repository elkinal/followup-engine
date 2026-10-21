import unittest
from datetime import timezone

from followup_engine.timeutil import format_ts, parse_ts


class TestParseTs(unittest.TestCase):
    def test_z_suffix(self):
        dt = parse_ts("2026-08-15T00:00:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(dt.hour, 0)

    def test_explicit_offset_is_converted_to_utc(self):
        dt = parse_ts("2026-08-15T00:00:00-04:00")
        self.assertEqual(dt.hour, 4)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_naive_timestamp_is_assumed_utc(self):
        dt = parse_ts("2026-08-15T00:00:00")
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_roundtrip_through_format_ts(self):
        self.assertEqual(format_ts(parse_ts("2026-08-15T12:34:56Z")), "2026-08-15T12:34:56Z")

    def test_garbage_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_ts("not-a-date")

    def test_empty_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_ts("")

    def test_none_raises(self):
        with self.assertRaises((TypeError, ValueError)):
            parse_ts(None)


if __name__ == "__main__":
    unittest.main()

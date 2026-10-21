import unittest

from followup_engine.phone import normalize_phone


class TestNormalizePhone(unittest.TestCase):
    def test_already_e164_is_unchanged(self):
        self.assertEqual(normalize_phone("+19175552003"), "+19175552003")

    def test_formatted_us_number_normalizes_to_the_same_value_as_e164(self):
        self.assertEqual(normalize_phone("(917) 555-2003"), normalize_phone("+19175552003"))

    def test_us_number_with_dashes_matches_e164(self):
        self.assertEqual(normalize_phone("917-555-2003"), normalize_phone("+19175552003"))

    def test_leading_one_without_plus_matches_e164(self):
        self.assertEqual(normalize_phone("19175552003"), normalize_phone("+19175552003"))

    def test_too_short_to_be_a_phone_number_is_rejected(self):
        self.assertIsNone(normalize_phone("12345"))

    def test_non_string_is_rejected(self):
        self.assertIsNone(normalize_phone(None))
        self.assertIsNone(normalize_phone(19175552003))

    def test_empty_string_is_rejected(self):
        self.assertIsNone(normalize_phone(""))


if __name__ == "__main__":
    unittest.main()

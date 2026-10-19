import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from create_users import agency_username


class AgencyUsernameTests(unittest.TestCase):
    def test_keeps_short_site_usernames_readable(self):
        self.assertEqual(agency_username("DVLA", "Tano North", 1, 2), "agency.dvla.tano.north")

    def test_shortens_long_site_usernames_with_unique_ids(self):
        agency = "A Very Long User Agency Name " * 3
        district = "An Extremely Long District Name " * 3

        first = agency_username(agency, district, 29, 140)
        second = agency_username(agency, district, 30, 140)

        self.assertLessEqual(len(first), 60)
        self.assertLessEqual(len(second), 60)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith(".29.140"))
        self.assertTrue(second.endswith(".30.140"))


if __name__ == "__main__":
    unittest.main()

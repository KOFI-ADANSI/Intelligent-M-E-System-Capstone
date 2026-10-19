import csv
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import main


class AuthenticationTests(unittest.TestCase):
    def test_app_uses_only_v2_credentials_file(self):
        self.assertEqual(
            main.CREDENTIALS_FILE,
            Path(main.ROOT) / "outputs" / "credentials_v2.csv",
        )

    def test_credentials_are_checked_against_configured_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_file = Path(temp_dir) / "credentials_v2.csv"
            with credentials_file.open("w", newline="", encoding="utf-8") as credentials:
                writer = csv.DictWriter(credentials, fieldnames=["username", "password"])
                writer.writeheader()
                writer.writerow({"username": "test.user", "password": "v2-password"})

            with patch.object(main, "CREDENTIALS_FILE", credentials_file):
                self.assertTrue(main.credentials_match("test.user", "v2-password"))
                self.assertFalse(main.credentials_match("test.user", "other-password"))
                self.assertFalse(main.credentials_match("legacy.user", "legacy-password"))


if __name__ == "__main__":
    unittest.main()

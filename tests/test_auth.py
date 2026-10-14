import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cnc_ui"))

import auth  # noqa: E402


class AuthTests(unittest.TestCase):
    def test_default_password_can_be_changed_from_ui_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "auth_config.json"
            with patch.object(auth, "AUTH_CONFIG_PATH", config_path):
                self.assertTrue(auth.verify_password("2026", "2026"))

                auth.set_password("operator-choice")

                self.assertFalse(auth.verify_password("2026", "2026"))
                self.assertTrue(auth.verify_password("operator-choice", "2026"))
                self.assertEqual(0o600, stat.S_IMODE(config_path.stat().st_mode))

    def test_corrupt_password_file_does_not_restore_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "auth_config.json"
            config_path.write_text("not json")
            with patch.object(auth, "AUTH_CONFIG_PATH", config_path):
                self.assertFalse(auth.verify_password("2026", "2026"))

    def test_password_length_is_validated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "auth_config.json"
            with patch.object(auth, "AUTH_CONFIG_PATH", config_path):
                with self.assertRaises(ValueError):
                    auth.set_password("123")

    def test_recovery_code_is_provisioned_once_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recovery_path = Path(temp_dir) / "recovery_config.json"
            with patch.object(auth, "RECOVERY_CONFIG_PATH", recovery_path):
                code = auth.provision_recovery_code()
                self.assertIsNotNone(code)
                self.assertTrue(auth.verify_recovery_code(code))
                self.assertTrue(auth.verify_recovery_code(code.lower().replace("-", " ")))
                self.assertFalse(auth.verify_recovery_code("wrong-code"))
                self.assertIsNone(auth.provision_recovery_code())
                self.assertNotIn(code, recovery_path.read_text(encoding="utf-8"))
                self.assertEqual(0o600, stat.S_IMODE(recovery_path.stat().st_mode))

    def test_missing_or_corrupt_recovery_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recovery_path = Path(temp_dir) / "recovery_config.json"
            with patch.object(auth, "RECOVERY_CONFIG_PATH", recovery_path):
                self.assertFalse(auth.verify_recovery_code("anything"))
                recovery_path.write_text("not json", encoding="utf-8")
                self.assertFalse(auth.verify_recovery_code("anything"))


if __name__ == "__main__":
    unittest.main()

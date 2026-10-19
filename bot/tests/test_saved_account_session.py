import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("account_session_test", Path(__file__).resolve().parents[1] / "account_session.py")
account_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(account_session)


class SavedAccountTests(unittest.TestCase):
    def test_only_explicit_new_browser_login_overrides_variable(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"USER_TOKEN": " configured "}):
            path = Path(folder) / "login.json"
            with patch.object(account_session, "SESSION_PATH", path):
                self.assertEqual(account_session.configured_account_token(), "configured")
                for contents in ("damaged", "[]", json.dumps({"token": "old", "id": "123"}),
                                 json.dumps({"token": "", "id": "123", "login_method": "browser-v2"})):
                    path.write_text(contents)
                    self.assertEqual(account_session.configured_account_token(), "configured")
                path.write_text(json.dumps({"token": "saved", "id": "123", "username": "selected", "login_method": "browser-v2"}))
                self.assertEqual(account_session.configured_account_token(), "saved")
                with patch.dict(os.environ, {"USER_TOKEN": ""}):
                    self.assertEqual(account_session.configured_account_token(), "saved")

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[2] / "docker/youtube_session_store.py"
spec = importlib.util.spec_from_file_location("youtube_session_store_test", source)
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "cookies.json"
        self.contents = json.dumps({"youtube": ["SID=test-session"]})
        self.path.write_text(self.contents)
        self.records = {}
        collection = SimpleNamespace(
            find_one=lambda query: self.records.get(query["_id"]),
            replace_one=lambda query, value, upsert: self.records.update({query["_id"]: value}),
        )
        class Client:
            def __init__(client, *args, **kwargs):
                client.midvox = SimpleNamespace(owner_login_sessions=collection)
            def __enter__(client):
                return client
            def __exit__(client, *args):
                pass
        real_import = __import__
        def import_dependency(name, *args, **kwargs):
            if name == "pymongo":
                return SimpleNamespace(MongoClient=Client)
            return real_import(name, *args, **kwargs)
        self.module = patch("builtins.__import__", side_effect=import_dependency)
        self.module.start()
        self.addCleanup(self.module.stop)
        self.env = patch.dict(os.environ, {"MONGO_URI": "test", "NEXTAUTH_SECRET": "test-encryption-secret",
            "OWNER_IDS": "1410320300129652932", "DISCORD_CLIENT_ID": "old-bot", "COOKIE_PATH": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_session_survives_host_and_bot_change_and_stays_encrypted(self):
        self.assertTrue(store.transfer())
        record = self.records["youtube:owner:1410320300129652932"]
        self.assertNotIn("test-session", record["encrypted"])
        self.assertNotIn("expires_at", record)
        self.path.unlink()
        with patch.dict(os.environ, {"DISCORD_CLIENT_ID": "new-bot", "RAILWAY_PUBLIC_DOMAIN": "new-host.example"}):
            self.assertTrue(store.transfer(restore=True))
        self.assertEqual(self.path.read_text(), self.contents)

    def test_old_application_backup_is_migrated_without_deleting_it(self):
        with patch.dict(os.environ, {"OWNER_IDS": ""}):
            self.assertTrue(store.transfer())
        self.path.unlink()
        self.assertTrue(store.transfer(restore=True))
        self.assertIn("youtube:old-bot", self.records)
        self.assertIn("youtube:owner:1410320300129652932", self.records)
        self.assertEqual(self.path.read_text(), self.contents)

    def test_wrong_encryption_secret_does_not_overwrite_local_session(self):
        self.assertTrue(store.transfer())
        with patch.dict(os.environ, {"NEXTAUTH_SECRET": "different-secret"}):
            with self.assertRaises(Exception):
                store.transfer(restore=True)
        self.assertEqual(self.path.read_text(), self.contents)

    def test_discord_selected_account_is_encrypted_and_portable(self):
        contents = json.dumps({"token": "private-discord-session", "id": "123", "login_method": "browser-v2"})
        self.path.write_text(contents)
        with patch.object(store, "Path", return_value=self.path):
            self.assertTrue(store.transfer(kind="discord"))
            record = self.records["discord:owner:1410320300129652932"]
            self.assertNotIn("private-discord-session", record["encrypted"])
            self.path.unlink()
            with patch.dict(os.environ, {"DISCORD_CLIENT_ID": "new-application"}):
                self.assertTrue(store.transfer(restore=True, kind="discord"))
        self.assertEqual(self.path.read_text(), contents)

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import cobalt_cookies


class CobaltCookieTests(unittest.TestCase):
    def test_merge_preserves_existing_youtube_session(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cookies.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"youtube": ["SID=youtube"]}, output)

            cobalt_cookies.merge(path, "sessionid=abc; csrftoken=def")

            with open(path, encoding="utf-8") as source:
                store = json.load(source)
            self.assertEqual(store["youtube"], ["SID=youtube"])
            self.assertEqual(store["instagram"], ["sessionid=abc; csrftoken=def"])

    def test_merge_creates_a_missing_store(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cookies.json")
            cobalt_cookies.merge(path, "sessionid=abc")
            with open(path, encoding="utf-8") as source:
                self.assertEqual(json.load(source), {"instagram": ["sessionid=abc"]})

    def test_main_reads_instagram_header_from_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cookies.json")
            with patch.dict(os.environ, {
                "COOKIE_PATH": path,
                "INSTAGRAM_COOKIES": "sessionid=abc; csrftoken=def",
            }, clear=True):
                self.assertTrue(cobalt_cookies.main())
            with open(path, encoding="utf-8") as source:
                self.assertEqual(
                    json.load(source)["instagram"],
                    ["sessionid=abc; csrftoken=def"],
                )

    def test_main_without_session_leaves_the_store_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cookies.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"youtube": ["SID=youtube"]}, output)
            with patch.dict(os.environ, {"COOKIE_PATH": path}, clear=True):
                self.assertFalse(cobalt_cookies.main())
            with open(path, encoding="utf-8") as source:
                self.assertEqual(json.load(source), {"youtube": ["SID=youtube"]})

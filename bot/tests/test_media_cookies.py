import base64
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from media_cookies import (
    cookie_file_from_value,
    instagram_cookie_header,
    youtube_cookie_file,
)


class CookieConversionTests(unittest.TestCase):
    def test_raw_header_becomes_netscape(self):
        content = cookie_file_from_value(
            "SID=secret; PREF=language=en", ".youtube.com"
        )
        self.assertIn("# Netscape HTTP Cookie File", content)
        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret", content)
        self.assertIn("PREF\tlanguage=en", content)

    def test_json_export_becomes_netscape(self):
        exported = json.dumps([
            {"name": "sessionid", "value": "abc", "domain": ".instagram.com",
             "path": "/", "secure": True, "hostOnly": False},
        ])
        content = cookie_file_from_value(exported, ".instagram.com")
        self.assertIn(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc", content)

    def test_base64_netscape_is_kept(self):
        netscape = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        )
        encoded = base64.b64encode(netscape.encode()).decode()
        self.assertEqual(cookie_file_from_value(encoded, ".youtube.com"), netscape)

    def test_single_cookie_pair_becomes_netscape(self):
        content = cookie_file_from_value("sessionid=only", ".instagram.com")
        self.assertIn(
            ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tonly", content
        )

    def test_path_input_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cookies.txt")
            with open(path, "w", encoding="utf-8") as output:
                output.write("SID=from-file; PREF=x")
            content = cookie_file_from_value(path, ".youtube.com")
        self.assertIn("SID\tfrom-file", content)

    def test_youtube_store_is_converted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = os.path.join(folder, "youtube.json")
            with open(store, "w", encoding="utf-8") as output:
                json.dump({"youtube": ["SID=one; PREF=two"]}, output)
            with patch.dict(os.environ, {"COOKIE_PATH": store}, clear=False):
                path = youtube_cookie_file()
                self.assertTrue(path and os.path.isfile(path))
                with open(path, encoding="utf-8") as cookie_file:
                    content = cookie_file.read()
        self.assertIn("SID\tone", content)
        self.assertIn("PREF\ttwo", content)

    def test_instagram_raw_header_is_returned_for_embed_requests(self):
        with patch.dict(os.environ, {"INSTAGRAM_COOKIES": "sessionid=abc; csrftoken=def"}):
            self.assertEqual(instagram_cookie_header(), "sessionid=abc; csrftoken=def")

    def test_instagram_netscape_file_becomes_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "instagram.txt")
            with open(path, "w", encoding="utf-8") as output:
                output.write(
                    "# Netscape HTTP Cookie File\n"
                    ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
                )
            with patch.dict(os.environ, {"INSTAGRAM_COOKIE_FILE": path}, clear=True):
                self.assertEqual(instagram_cookie_header(), "sessionid=abc")

    def test_instagram_header_drops_cookies_for_other_domains(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "instagram.txt")
            with open(path, "w", encoding="utf-8") as output:
                output.write(
                    "# Netscape HTTP Cookie File\n"
                    ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
                )
            with patch.dict(os.environ, {"INSTAGRAM_COOKIE_FILE": path}, clear=True):
                self.assertEqual(instagram_cookie_header(), "sessionid=abc")

    def test_dashboard_instagram_session_is_used_without_env(self):
        with tempfile.TemporaryDirectory() as folder:
            store = os.path.join(folder, "cookies.json")
            with open(store, "w", encoding="utf-8") as output:
                json.dump({
                    "youtube": ["SID=youtube"],
                    "instagram": ["sessionid=dashboard; csrftoken=csrf"],
                }, output)
            with patch.dict(os.environ, {"COOKIE_PATH": store}, clear=True):
                self.assertEqual(
                    instagram_cookie_header(),
                    "sessionid=dashboard; csrftoken=csrf",
                )


if __name__ == "__main__":
    unittest.main()

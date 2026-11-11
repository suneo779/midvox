import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from cobalt_errors import extraction_error


class CobaltErrorTests(unittest.TestCase):
    def test_fetch_fail_explains_blocked_server_connection(self):
        error = extraction_error(["error.api.fetch.fail", "error.api.fetch.fail"])
        self.assertEqual(error.code, "error.api.fetch.fail")
        self.assertIn("Railway/WARP", error.user_message)
        self.assertIn("Cookie-free downloads cannot complete", error.user_message)

    def test_login_failure_survives_different_client_errors(self):
        error = extraction_error([
            "error.api.content.video.unavailable", "error.api.youtube.login",
        ])
        self.assertEqual(error.code, "error.api.youtube.login")
        self.assertIn("sign in", error.user_message)
        self.assertIn("/dashboard/youtube-login", error.user_message)
        self.assertIn("stay visible", error.user_message)

    def test_unavailable_requires_agreement_between_clients(self):
        self.assertIsNone(extraction_error(["error.api.content.video.unavailable", "unknown"]))
        self.assertEqual(
            extraction_error(["error.api.content.video.unavailable"]).code,
            "error.api.content.video.unavailable",
        )

    def test_unknown_and_empty_errors_keep_generic_handling(self):
        self.assertIsNone(extraction_error([]))
        self.assertIsNone(extraction_error(["error.api.youtube.api_error"]))

    def test_instagram_fetch_fail_does_not_blame_youtube(self):
        error = extraction_error(["error.api.fetch.fail"], source="instagram")
        self.assertIn("Instagram", error.user_message)
        self.assertNotIn("YouTube", error.user_message)
        self.assertIn("INSTAGRAM_COOKIES", error.user_message)

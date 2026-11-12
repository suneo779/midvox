import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import instagram_api


class InstagramApiTests(unittest.TestCase):
    def test_shortcode_maps_to_the_numeric_media_id(self):
        self.assertEqual(
            instagram_api.shortcode_to_media_id("DcUAiUBzAw0"),
            "3968799529587444788",
        )

    def test_invalid_shortcode_is_rejected(self):
        self.assertIsNone(instagram_api.shortcode_to_media_id("not/a/shortcode"))
        self.assertIsNone(instagram_api.shortcode_to_media_id(""))

    def test_video_urls_are_deduplicated_and_highest_first(self):
        payload = {
            "items": [{
                "video_versions": [
                    {"url": "https://cdn.example/low.mp4", "width": 640, "height": 360},
                    {"url": "https://cdn.example/high.mp4", "width": 1080, "height": 1920},
                    {"url": "https://cdn.example/high.mp4", "width": 1080, "height": 1920},
                ],
            }],
            "data": {"x": {"video_url": "https://cdn.example/also.mp4"}},
        }
        self.assertEqual(
            instagram_api.video_urls(payload),
            ["https://cdn.example/high.mp4", "https://cdn.example/low.mp4",
             "https://cdn.example/also.mp4"],
        )

    def test_image_payload_has_no_video(self):
        payload = {"items": [{"image_versions2": {"candidates": [
            {"url": "https://cdn.example/image.jpg", "width": 1080, "height": 1920},
        ]}}]}
        self.assertEqual(instagram_api.video_urls(payload), [])

    def test_api_requests_use_the_saved_session(self):
        calls = []

        def fake_json(url, headers, timeout=15):
            calls.append((url, headers))
            return None

        cookie = "sessionid=session; csrftoken=csrf"
        with patch.object(instagram_api, "_json", side_effect=fake_json):
            instagram_api.api_payloads("DcUAiUBzAw0", cookie)

        self.assertTrue(calls)
        for _, headers in calls:
            self.assertEqual(headers["Cookie"], cookie)
        desktop = [headers for url, headers in calls if "graphql" in url]
        self.assertTrue(desktop)
        self.assertEqual(desktop[0]["X-CSRFToken"], "csrf")
        self.assertEqual(desktop[0]["X-IG-App-ID"], instagram_api.APP_ID)
        mobile = [headers for url, headers in calls if "/api/v1/media/" in url]
        self.assertTrue(mobile)
        self.assertEqual(mobile[0]["X-CSRFToken"], "csrf")

    def test_refuses_media_from_foreign_hosts(self):
        with patch.object(instagram_api.urllib.request, "urlopen") as fetch:
            self.assertIsNone(instagram_api._download_media(
                "https://evil.example/reel.mp4", "sessionid=session", 100, 5,
            ))
        fetch.assert_not_called()

    def test_download_returns_the_first_working_source_for_compression(self):
        with patch.object(instagram_api, "api_payloads", return_value=[{
            "items": [{"video_versions": [
                {"url": "https://cdn.example/big.mp4", "width": 1080, "height": 1920},
            ]}],
        }]), patch.object(instagram_api, "_download_media", return_value="/tmp/reel.mp4") as media, \
             patch("instagram_api.os.path.getsize", return_value=200):
            result = instagram_api.download("DcUAiUBzAw0", 9_500_000)
        self.assertEqual(result, "/tmp/reel.mp4")
        self.assertEqual(media.call_args.args[0], "https://cdn.example/big.mp4")
        self.assertGreaterEqual(media.call_args.args[2], 9_500_000)


if __name__ == "__main__":
    unittest.main()

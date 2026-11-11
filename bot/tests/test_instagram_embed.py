import os
import tempfile
import unittest
from unittest.mock import patch

from instagram_embed import video_url_from_html


class InstagramEmbedTests(unittest.TestCase):
    def test_extracts_escaped_video_url(self):
        page = '{"video_url":"https:\\/\\/cdn.example\\/reel.mp4?x=1"}'
        self.assertEqual(
            video_url_from_html(page), "https://cdn.example/reel.mp4?x=1"
        )

    def test_extracts_video_tag(self):
        page = '<video controls src="https://cdn.example/video.mp4"></video>'
        self.assertEqual(video_url_from_html(page), "https://cdn.example/video.mp4")

    def test_returns_none_without_video(self):
        self.assertIsNone(video_url_from_html("<html>login required</html>"))

    def test_download_rejects_html_page(self):
        from instagram_embed import download

        class Response:
            def __init__(self, body):
                self.body = body
                self.headers = {}

            def read(self, size):
                body, self.body = self.body, b""
                return body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        page = Response(b"<html>embed</html>")
        media = Response(b"<html>not a video</html>" * 100)
        with patch("instagram_embed.urllib.request.urlopen", side_effect=[page, media]), \
             patch("instagram_embed.video_url_from_html", return_value="https://cdn.example/x.mp4"):
            self.assertIsNone(download("abc", 100000))


if __name__ == "__main__":
    unittest.main()

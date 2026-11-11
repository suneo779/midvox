import io
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from hosted_downloads import HostedDownloads


class Response(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}


class HostedDownloadTests(unittest.TestCase):
    def test_skips_oversized_file_without_get(self):
        with patch("hosted_downloads.urllib.request.urlopen", return_value=Response(headers={"Content-Length": "101"})) as fetch:
            self.assertIsNone(HostedDownloads._media("https://provider.example/video", 100, time.monotonic() + 10))
            self.assertEqual(fetch.call_count, 1)

    def test_empty_file_is_rejected_and_removed(self):
        with patch("hosted_downloads.urllib.request.urlopen", side_effect=[Response(), Response()]), patch("hosted_downloads.os.unlink") as cleanup:
            self.assertIsNone(HostedDownloads._media("https://provider.example/video", 100, time.monotonic() + 10))
            path = cleanup.call_args.args[0]
        os.unlink(path)

    def test_rejects_html_error_with_success_status(self):
        with patch("hosted_downloads.urllib.request.urlopen", side_effect=[Response(), Response(b"<html>error</html>")]):
            self.assertIsNone(HostedDownloads._media("https://provider.example/video", 100, time.monotonic() + 10))

    def test_writes_valid_mp4(self):
        content = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 30
        with patch("hosted_downloads.urllib.request.urlopen", side_effect=[Response(), Response(content)]):
            path = HostedDownloads._media("https://provider.example/video", 100, time.monotonic() + 10)
        try:
            with open(path, "rb") as output:
                self.assertEqual(output.read(), content)
        finally:
            os.unlink(path)

    def test_failed_provider_does_not_prevent_next_provider(self):
        downloader = HostedDownloads()
        providers = [(0, "https://first.example", "secret"), (1, "https://second.example", "")]
        body = b'{"status":"tunnel","url":"https://second.example/media"}'
        with patch.object(downloader, "providers", return_value=providers), patch("hosted_downloads.urllib.request.urlopen", side_effect=[TimeoutError(), Response(body)]), patch.object(downloader, "_media", return_value="video.mp4"):
            self.assertEqual(downloader.download("https://youtu.be/example", 100), "video.mp4")
        self.assertGreater(downloader.cooldowns["https://first.example"], time.monotonic())

    def test_bad_configuration_is_disabled(self):
        with patch.dict(os.environ, {"COBALT_HOSTED_PROVIDERS": '[{"url":"http://example.com"}]'}):
            self.assertEqual(HostedDownloads().providers(), [])

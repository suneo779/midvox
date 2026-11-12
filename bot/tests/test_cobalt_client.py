import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from cobalt_client import CobaltClient, OVERSIZED


class CobaltClientTests(unittest.TestCase):
    def client(self):
        return CobaltClient("https://cobalt.example")

    def test_tries_lower_quality_after_every_client_fails_at_max(self):
        client = self.client()
        calls = []

        def request(url, quality, youtube_client, deadline):
            calls.append((quality, youtube_client))
            if quality == "max":
                return {"status": "error", "error": {"code": "error.api.youtube.login"}}
            if quality == "1080" and youtube_client == "IOS":
                return {"status": "tunnel", "url": "https://cobalt.example/tunnel"}
            return {"status": "error", "error": {"code": "error.api.fetch.fail"}}

        with patch.object(client, "_request", side_effect=request), \
             patch.object(client, "_media", return_value="/tmp/media.mp4"):
            self.assertEqual(client.download("https://youtu.be/example", 100), "/tmp/media.mp4")

        self.assertIn(("max", "WEB_EMBEDDED"), calls)
        self.assertIn(("1080", "IOS"), calls)
        self.assertIsNone(client.last_error)

    def test_oversized_max_is_retried_at_lower_quality(self):
        client = self.client()
        qualities = []

        def request(url, quality, youtube_client, deadline):
            qualities.append(quality)
            return {"status": "tunnel", "url": "https://cobalt.example/tunnel"}

        with patch.object(client, "_request", side_effect=request), \
             patch.object(client, "_media", side_effect=[OVERSIZED, "/tmp/media.mp4"]):
            self.assertEqual(client.download("https://youtu.be/example", 100), "/tmp/media.mp4")

        self.assertEqual(qualities, ["max", "1080"])

    def test_oversized_source_is_returned_for_local_compression(self):
        client = self.client()
        handle, path = tempfile.mkstemp(suffix=".mp4")
        os.close(handle)
        with open(path, "wb") as output:
            output.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 500)
        calls = 0

        def media(url, limit, deadline, *, source="youtube"):
            nonlocal calls
            calls += 1
            return path if calls == 1 else None

        try:
            with patch.object(client, "_request", return_value={
                "status": "tunnel", "url": "https://cobalt.example/tunnel",
            }), patch.object(client, "_media", side_effect=media):
                self.assertEqual(client.download("https://youtu.be/example", 100), path)
            self.assertGreater(os.path.getsize(path), 100)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_all_failures_keep_the_specific_cobalt_error(self):
        client = self.client()
        response = {"status": "error", "error": {"code": "error.api.fetch.fail"}}
        with patch.object(client, "_request", return_value=response):
            self.assertIsNone(client.download("https://youtu.be/example", 100))
        self.assertEqual(client.last_error.code, "error.api.fetch.fail")

    def test_rate_limit_aborts_remaining_qualities(self):
        client = self.client()
        qualities = []

        def request(url, quality, youtube_client, deadline):
            qualities.append(quality)
            return {"status": "error", "error": {"code": "error.api.fetch.rate"}}

        with patch.object(client, "_request", side_effect=request), \
             patch("cobalt_client.time.sleep"):
            self.assertIsNone(client.download("https://youtu.be/example", 100))

        self.assertEqual(qualities, ["max"])
        self.assertEqual(client.last_error.code, "error.api.fetch.rate")

    def test_request_forces_proxy_and_passes_client(self):
        client = self.client()
        response = MagicMock()
        response.read.return_value = b'{"status":"tunnel","url":"https://cobalt.example/t"}'
        opener = MagicMock()
        opener.__enter__.return_value = response
        with patch("cobalt_client.open_cobalt_request", return_value=opener) as request:
            client._request("https://youtu.be/example", "720", "IOS", time.monotonic() + 10)
        payload = json.loads(request.call_args.args[0].data)
        self.assertTrue(payload["alwaysProxy"])
        self.assertEqual(payload["youtubeClient"], "IOS")
        self.assertEqual(payload["videoQuality"], "720")
        self.assertEqual(payload["localProcessing"], "disabled")


if __name__ == "__main__":
    unittest.main()

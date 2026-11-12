import os
import tempfile
import unittest
from unittest.mock import patch
from media_quality import download_fitting_video, fitting_formats, is_valid_video, upload_budget


class MediaQualityTests(unittest.TestCase):
    def test_skips_large_quality_and_separate_streams(self):
        info = {"formats": [
            {"url": "high", "height": 1080, "filesize": 25000000},
            {"url": "medium", "height": 720, "filesize": 5000000},
            {"url": "silent", "height": 1080, "acodec": "none"},
            {"url": "low", "height": 480, "filesize": 2000000},
        ]}
        self.assertEqual([f["url"] for f in fitting_formats(info, 9500000)], ["medium", "low"])

    def test_bitrate_estimate(self):
        info = {"duration": 60, "formats": [
            {"url": "high", "tbr": 2000}, {"url": "low", "tbr": 500},
        ]}
        self.assertEqual([f["url"] for f in fitting_formats(info, 9500000)], ["low"])

    def test_actual_oversize_retries_lower_and_cleans_file(self):
        with tempfile.TemporaryDirectory() as folder:
            class Downloader:
                attempted = []
                def extract_info(self, url, download):
                    return {"formats": [{"url": "high", "height": 1080}, {"url": "low", "height": 480}]}
                def process_ie_result(self, info, download):
                    fmt = info["formats"][0]
                    self.attempted.append(fmt["url"])
                    path = os.path.join(folder, fmt["url"] + ".mp4")
                    with open(path, "wb") as output:
                        output.write(b"x" * (200 if fmt["url"] == "high" else 50))
                    return {"path": path}
                def prepare_filename(self, info):
                    return info["path"]
            downloader = Downloader()
            path = download_fitting_video(downloader, "video", 100)
            self.assertEqual(os.path.getsize(path), 50)
            self.assertEqual(downloader.attempted, ["high", "low"])
            self.assertFalse(os.path.exists(os.path.join(folder, "high.mp4")))

    def test_instagram_single_oversized_source_is_compressed(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "source.mp4")
            compressed = os.path.join(folder, "compressed.mp4")
            class Downloader:
                params = {"max_filesize": 100}
                attempted = []
                def extract_info(self, url, download):
                    return {"formats": [{"url": "big", "height": 1080, "filesize": 500},
                                        {"url": "smaller", "height": 720, "filesize": 200}]}
                def process_ie_result(self, info, download):
                    self.attempted.append(info["formats"][0]["url"])
                    self.source_budget = self.params["max_filesize"]
                    with open(source, "wb") as output:
                        output.write(b"x" * 200)
                    return {"path": source}
                def prepare_filename(self, info):
                    return info["path"]
            downloader = Downloader()
            with open(compressed, "wb") as output:
                output.write(b"x" * 80)
            with patch("media_quality.compress_to_budget", return_value=compressed) as encode:
                result = download_fitting_video(downloader, "reel", 100, compress=True)
            self.assertEqual(result, compressed)
            self.assertEqual(downloader.attempted, ["smaller"])
            self.assertGreater(downloader.source_budget, 200)
            self.assertEqual(downloader.params["max_filesize"], 100)
            self.assertFalse(os.path.exists(source))
            encode.assert_called_once_with(source, 100)

    def test_oversized_source_without_compression_is_not_downloaded(self):
        class Downloader:
            def extract_info(self, url, download):
                return {"formats": [{"url": "big", "filesize": 200}]}
            def process_ie_result(self, info, download):
                raise AssertionError("Should not download")
        self.assertIsNone(download_fitting_video(Downloader(), "video", 100))

    def test_encoding_failure_removes_partial_output(self):
        import subprocess
        from media_quality import compress_to_budget
        with tempfile.TemporaryDirectory() as folder:
            partial = os.path.join(folder, "partial.mp4")
            fd = os.open(partial, os.O_CREAT | os.O_RDWR)
            probe = subprocess.CompletedProcess([], 0, stdout=b'{"format":{"duration":"30"}}')
            def run(command, **kwargs):
                if "-show_entries" in command:
                    return probe
                raise subprocess.TimeoutExpired("ffmpeg", 90)
            with patch("media_quality.shutil.which", return_value="binary"), \
                 patch("media_quality.tempfile.mkstemp", return_value=(fd, partial)), \
                 patch("media_quality.subprocess.run", side_effect=run):
                self.assertIsNone(compress_to_budget("source", 1_000_000))
            self.assertFalse(os.path.exists(partial))

    def test_server_limit_and_configured_cap(self):
        self.assertEqual(upload_budget(9500000, 8000000), 7840000)
        self.assertEqual(upload_budget(9500000, 50000000), 9500000)

    def test_source_cap_allows_blindly_oversized_renditions(self):
        from media_quality import source_cap
        with patch.dict(os.environ, {"MEDIA_SOURCE_MAX_BYTES": "200000000"}):
            self.assertEqual(source_cap(9500000), 200000000)
        with patch.dict(os.environ, {"MEDIA_SOURCE_MAX_BYTES": "invalid"}):
            self.assertEqual(source_cap(9500000), 104857600)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(source_cap(9500000), 104857600)
            self.assertEqual(source_cap(500000000), 500000000)

    def test_rejects_html_saved_as_video(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "error.mp4")
            with open(path, "wb") as output:
                output.write(b"<html>error</html>" * 100)
            self.assertFalse(is_valid_video(path))

    def test_accepts_mp4_and_webm_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            mp4 = os.path.join(folder, "ok.mp4")
            webm = os.path.join(folder, "ok.webm")
            with open(mp4, "wb") as output:
                output.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2000)
            with open(webm, "wb") as output:
                output.write(b"\x1aE\xdf\xa3" + b"\x00" * 2000)
            self.assertTrue(is_valid_video(mp4))
            self.assertTrue(is_valid_video(webm))


if __name__ == "__main__":
    unittest.main()

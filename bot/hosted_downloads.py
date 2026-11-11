"""Bounded hosted Cobalt downloads, with provider failover and media checks."""

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class HostedDownloads:
    def __init__(self):
        self.cooldowns = {}

    def providers(self):
        # Use only endpoints explicitly configured by the owner. Public
        # instance lists do not promise availability or automated API access.
        try:
            entries = json.loads(os.getenv("COBALT_HOSTED_PROVIDERS", "[]"))
            if not isinstance(entries, list):
                raise ValueError
            result = []
            for index, entry in enumerate(entries[:3]):
                if not isinstance(entry, dict):
                    continue
                endpoint = str(entry.get("url") or "").strip().rstrip("/")
                parsed = urllib.parse.urlsplit(endpoint)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                    continue
                key = entry.get("api_key", "")
                result.append((index, endpoint, key if isinstance(key, str) else ""))
            return result
        except (ValueError, TypeError, AttributeError):
            print("[InstaDL] invalid COBALT_HOSTED_PROVIDERS configuration")
            return []

    def download(self, url, limit, source="youtube"):
        deadline = time.monotonic() + 90
        for index, endpoint, key in self.providers():
            if self.cooldowns.get(endpoint, 0) > time.monotonic():
                continue
            try:
                for quality in ("max", "1080", "720", "480", "360", "240"):
                    if time.monotonic() >= deadline:
                        return None
                    headers = {"Accept": "application/json", "Content-Type": "application/json"}
                    if key:
                        headers["Authorization"] = "Api-Key " + key
                    payload = {
                        "url": url,
                        "videoQuality": quality,
                        # Keep the provider in the media path so the bot never
                        # follows a redirect signed for the provider's IP.
                        "alwaysProxy": True,
                        "localProcessing": "disabled",
                    }
                    if source == "youtube":
                        payload.update({
                            "youtubeVideoCodec": "h264",
                            "youtubeVideoContainer": "mp4",
                        })
                    request = urllib.request.Request(
                        endpoint, data=json.dumps(payload).encode(), headers=headers
                    )
                    with urllib.request.urlopen(request, timeout=15) as response:
                        data = json.loads(response.read(256 * 1024))
                    status = data.get("status") if isinstance(data, dict) else None
                    media_url = data.get("url") if isinstance(data, dict) else None
                    media_headers = {}
                    if key and media_url:
                        # Never send a provider's API key to another host
                        # (e.g. a googlevideo URL when alwaysProxy is ignored).
                        if urllib.parse.urlsplit(media_url).hostname == urllib.parse.urlsplit(endpoint).hostname:
                            media_headers["Authorization"] = "Api-Key " + key
                    if status in ("tunnel", "redirect") and media_url:
                        path = self._media(
                            media_url, limit, deadline,
                            source=source, headers=media_headers,
                        )
                        if path:
                            self.cooldowns.pop(endpoint, None)
                            print(f"[InstaDL] hosted provider {index + 1} downloaded {quality}")
                            return path
                        # None means oversized or invalid; try a lower quality
                        # without downloading an oversized file to completion.
                    elif status == "picker":
                        for item in data.get("picker") or []:
                            if not isinstance(item, dict) or item.get("type") != "video":
                                continue
                            item_url = item.get("url")
                            if not item_url:
                                continue
                            path = self._media(
                                item_url, limit, deadline,
                                source=source, headers=media_headers,
                            )
                            if path:
                                return path
            except Exception as error:
                # Do not print URLs, response bodies or credentials. Provider
                # exceptions can contain signed URLs or authentication data.
                print(f"[InstaDL] hosted provider {index + 1} failed: {type(error).__name__}")
                self.cooldowns[endpoint] = time.monotonic() + 300
        return None

    @staticmethod
    def _media(url, limit, deadline, *, source="youtube", headers=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            print("[InstaDL] hosted provider returned an invalid media URL")
            return None
        request_headers = {"User-Agent": "MidVox/1.0"}
        if source == "instagram":
            request_headers["Referer"] = "https://www.instagram.com/"
        if headers:
            request_headers.update(headers)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, method="HEAD", headers=request_headers),
                timeout=10,
            ) as response:
                # Estimated-Content-Length is advisory and can overestimate.
                length = response.headers.get("Content-Length", "")
                if length.isdigit() and int(length) > limit:
                    return None
        except (urllib.error.URLError, TimeoutError):
            pass
        path = None
        keep = False
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=request_headers), timeout=30
            ) as response:
                with tempfile.NamedTemporaryFile(prefix="midvox_hosted_", suffix=".mp4", delete=False) as output:
                    path = output.name
                    size = 0
                    first = True
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        if first:
                            # MP4/M4A starts with a file-type box and WebM
                            # starts with an EBML header; reject HTML, JSON
                            # error pages and arbitrary provider output.
                            if not (
                                len(chunk) >= 12
                                and (chunk[4:8] == b"ftyp" or chunk[:4] == b"\x1aE\xdf\xa3")
                            ):
                                print("[InstaDL] hosted provider returned non-video data")
                                return None
                            first = False
                        size += len(chunk)
                        if size > limit:
                            return None
                        output.write(chunk)
                    if size == 0:
                        print("[InstaDL] hosted provider returned an empty file")
                        return None
                    keep = True
                    return path
        except urllib.error.HTTPError as error:
            # A signed tunnel URL can expire between the API response and the
            # media request; try the next quality/provider instead of cooling
            # down an otherwise healthy provider.
            print(f"[InstaDL] hosted provider media HTTP {error.code}")
            return None
        finally:
            if path and not keep:
                os.unlink(path)

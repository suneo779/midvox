"""Cobalt client with bounded attempts and real quality/client fallback.

The previous downloader stopped as soon as every YouTube client failed at the
highest quality. A single challenged client at ``max`` therefore discarded
qualities that would have worked. This client treats each quality separately,
retries lower qualities, and validates the bytes returned by Cobalt before
handing a path to Discord.
"""

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from cobalt_errors import extraction_error
from cobalt_transport import open_cobalt_request

USER_AGENT = "MidVox/1.0 (+https://github.com/suneo779/midvox)"
VIDEO_QUALITIES = ("max", "1080", "720", "480", "360", "240")
YOUTUBE_CLIENTS = (
    "WEB_EMBEDDED",
    "IOS",
    "ANDROID_VR",
    "TV",
    "TV_SIMPLY",
    "TV_EMBEDDED",
    "WEB_CREATOR",
    "MWEB",
    "ANDROID",
    "VISIONOS",
    "YTMUSIC",
    "YTMUSIC_ANDROID",
    "YTSTUDIO_ANDROID",
    "YTKIDS",
)
OVERSIZED = object()


def _seconds(name, default):
    try:
        return max(1.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return float(default)


def _error_code(data):
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return error.get("code")
    if isinstance(error, str):
        return error
    return data.get("code") if isinstance(data, dict) else None


def _is_rate_limit(code):
    return isinstance(code, str) and ("rate" in code or "ratelimit" in code)


class CobaltClient:
    def __init__(self, base_url, api_key="", *, log="[InstaDL]"):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.log = log
        self.deadline_seconds = _seconds("COBALT_DOWNLOAD_TIMEOUT", 180)
        self.request_timeout = _seconds("COBALT_REQUEST_TIMEOUT", 25)
        self.last_error = None
        self.errors = []

    def download(self, url, limit, *, source="youtube"):
        """Return a validated temp path, or None when every route failed."""
        if not self.base_url:
            return None
        self.last_error = None
        self.errors = []
        deadline = time.monotonic() + self.deadline_seconds
        clients = list(YOUTUBE_CLIENTS)
        if source == "youtube" and self._has_youtube_session():
            clients.remove("WEB_CREATOR")
            clients.insert(0, "WEB_CREATOR")

        for quality in VIDEO_QUALITIES:
            if time.monotonic() >= deadline:
                break
            oversized = False
            rate_limited = False
            for client in (clients if source == "youtube" else (None,)):
                if time.monotonic() >= deadline:
                    break
                data = self._request(url, quality, client, deadline)
                if not isinstance(data, dict):
                    continue
                status = data.get("status")
                if status == "error":
                    code = _error_code(data)
                    if code:
                        self.errors.append(code)
                    print(f"{self.log} cobalt {quality} {client or source} error: {code or 'unknown'}")
                    if _is_rate_limit(code):
                        # The limiter is shared by every quality/client on this
                        # instance. Back off once instead of burning the whole
                        # deadline on guaranteed failures.
                        rate_limited = True
                        time.sleep(min(5, max(1, deadline - time.monotonic())))
                        break
                    continue

                media_url = data.get("url")
                if status in ("tunnel", "redirect") and media_url:
                    path = self._media(media_url, limit, deadline, source=source)
                    if path is OVERSIZED:
                        print(f"{self.log} cobalt {quality} exceeds Discord upload limit")
                        oversized = True
                        break
                    if path:
                        print(f"{self.log} cobalt {quality} {client or source} downloaded successfully")
                        return path
                    continue

                if status == "picker":
                    for item in data.get("picker") or []:
                        if not isinstance(item, dict) or item.get("type") != "video":
                            continue
                        media_url = item.get("url")
                        if not media_url:
                            continue
                        path = self._media(media_url, limit, deadline,
                                           source=source)
                        if path and path is not OVERSIZED:
                            return path
                    continue

                print(f"{self.log} cobalt {quality} {client or source} returned {status or 'empty'}")
            if rate_limited:
                break
            if oversized:
                continue

        self.last_error = extraction_error(self.errors, source=source)
        if self.last_error:
            print(f"{self.log} cobalt exhausted: {self.last_error.code}")
        else:
            print(f"{self.log} cobalt exhausted with no usable media")
        return None

    @staticmethod
    def _has_youtube_session():
        path = os.getenv("COOKIE_PATH", "/tmp/midvox-youtube-cookies.json")
        try:
            with open(path, encoding="utf-8") as cookie_file:
                return bool(json.load(cookie_file).get("youtube"))
        except (OSError, ValueError, AttributeError):
            return False

    def _request(self, url, quality, client, deadline):
        payload = {
            "url": url,
            "videoQuality": quality,
            # Keep Cobalt's own connection in the media path. This also avoids
            # redirect URLs that were signed for a different (WARP) egress IP.
            "alwaysProxy": True,
            "localProcessing": "disabled",
        }
        if client:
            payload.update({
                "youtubeClient": client,
                "youtubeVideoCodec": "h264",
                "youtubeVideoContainer": "mp4",
                "youtubeBetterAudio": False,
            })
        body = json.dumps(payload).encode()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = "Api-Key " + self.api_key

        last_error = None
        for endpoint in (self.base_url, self.base_url + "/api/json"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            request = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
            try:
                with open_cobalt_request(
                    request, self.base_url,
                    timeout=max(1.0, min(self.request_timeout, remaining)),
                    recovery_seconds=min(10.0, remaining),
                ) as response:
                    return json.loads(response.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as error:
                last_error = error
                parsed = self._http_error(error)
                if parsed is not None:
                    return parsed
                if error.code == 429:
                    return {
                        "status": "error",
                        "error": {"code": "error.api.rate_limit"},
                    }
                if error.code not in (404, 405):
                    return None
            except Exception as error:
                last_error = error
        if last_error:
            print(f"{self.log} cobalt request failed: {type(last_error).__name__}")
        return None

    def _http_error(self, error):
        try:
            raw = error.read().decode("utf-8", "replace")
        except Exception:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            print(f"{self.log} cobalt HTTP {error.code} returned a non-JSON response")
            return None
        if isinstance(parsed, dict) and "status" not in parsed:
            parsed = {"status": "error", **parsed}
        return parsed

    def _media(self, url, limit, deadline, *, source="youtube"):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None

        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if source == "instagram":
            headers["Referer"] = "https://www.instagram.com/"
        if self.api_key and parsed.hostname == urllib.parse.urlsplit(self.base_url).hostname:
            headers["Authorization"] = "Api-Key " + self.api_key

        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            path = None
            keep = False
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(
                    request, timeout=max(5.0, min(90.0, remaining))
                ) as response:
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > limit:
                        return OVERSIZED
                    handle, path = tempfile.mkstemp(
                        prefix="midvox_cobalt_", suffix=".mp4"
                    )
                    size = 0
                    with os.fdopen(handle, "wb") as output:
                        while True:
                            if time.monotonic() >= deadline:
                                raise TimeoutError
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > limit:
                                return OVERSIZED
                            output.write(chunk)
                if size < 1024:
                    raise ValueError("empty media")
                with open(path, "rb") as output:
                    head = output.read(12)
                # MP4/M4A and WebM are the only containers Cobalt should return
                # for a video. Reject HTML error pages with a 200 status.
                if head[4:8] != b"ftyp" and head[:4] != b"\x1aE\xdf\xa3":
                    raise ValueError("invalid media")
                keep = True
                return path
            except urllib.error.HTTPError as error:
                if error.code in (401, 403, 404, 410):
                    return None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                if not isinstance(error, ValueError):
                    print(f"{self.log} cobalt media download failed: {type(error).__name__}")
            finally:
                if path and not keep and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        return None


def download(base_url, url, limit, *, source="youtube", api_key=""):
    client = CobaltClient(base_url, api_key)
    path = client.download(url, limit, source=source)
    return path, client.last_error

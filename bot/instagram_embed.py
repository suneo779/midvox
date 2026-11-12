"""Last-resort Instagram Reel downloader using the public embed page.

Instagram frequently rate-limits datacenter IPs on its private/mobile APIs.
The public ``/embed/captioned/`` page usually still renders the video URL for
public Reels, so it is a useful final fallback after Cobalt and yt-dlp.
"""

import html
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

from media_quality import source_cap

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_VIDEO_PATTERNS = (
    re.compile(r'"video_url"\s*:\s*"((?:\\.|[^"\\])*)"'),
    re.compile(r'"contentUrl"\s*:\s*"((?:\\.|[^"\\])*)"'),
    re.compile(r'<video[^>]+src="([^"]+)"', re.IGNORECASE),
    re.compile(r'<meta[^>]+property="og:video"[^>]+content="([^"]+)"', re.IGNORECASE),
)


def video_url_from_html(page):
    """Extract the first playable URL from an Instagram embed page."""
    if not page:
        return None
    for pattern in _VIDEO_PATTERNS:
        for match in pattern.finditer(page):
            raw = match.group(1)
            try:
                value = json.loads(f'"{raw}"')
            except (TypeError, ValueError):
                value = html.unescape(raw).replace("\\/", "/")
            value = html.unescape(value).strip()
            if value.startswith(("http://", "https://")):
                return value
    return None


def download(shortcode, limit, *, cookie_header="", timeout=25):
    """Download a public Reel through its embed page.

    Returns a temp file path, or ``None``. The file may exceed ``limit`` so the
    caller can compress the only rendition Instagram exposes.
    """
    try:
        cap = source_cap(limit)
    except (TypeError, ValueError):
        cap = max(limit, 104857600)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "X-IG-App-ID": "936619743392459",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    video_url = None
    for page_url in (
        f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
        f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
    ):
        try:
            request = urllib.request.Request(page_url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                page = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
        except Exception as error:
            print(f"[InstaDL] Instagram embed page unavailable: {type(error).__name__}")
            continue
        video_url = video_url_from_html(page)
        if video_url:
            break

    if not video_url:
        return None

    parsed = urllib.parse.urlsplit(video_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if not (
        host == "instagram.com"
        or host.endswith(".instagram.com")
        or host.endswith(".cdninstagram.com")
        or host.endswith(".fbcdn.net")
    ):
        print("[InstaDL] refused a media URL outside Instagram's domains")
        return None

    media_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
    if cookie_header:
        media_headers["Cookie"] = cookie_header
    path = None
    keep = False
    try:
        request = urllib.request.Request(video_url, headers=media_headers)
        with urllib.request.urlopen(request, timeout=max(15, timeout)) as response:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > cap:
                return None
            handle, path = tempfile.mkstemp(prefix="midvox_instagram_", suffix=".mp4")
            with os.fdopen(handle, "wb") as output:
                size = 0
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > cap:
                        return None
                    output.write(chunk)
        if os.path.getsize(path) < 1024:
            return None
        with open(path, "rb") as output:
            head = output.read(12)
        if head[4:8] != b"ftyp" and head[:4] != b"\x1aE\xdf\xa3":
            return None
        keep = True
        return path
    except Exception as error:
        print(f"[InstaDL] Instagram embed download failed: {type(error).__name__}")
        return None
    finally:
        if path and not keep and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

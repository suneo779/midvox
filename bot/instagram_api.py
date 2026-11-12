"""Instagram's own web and mobile API for Reels.

Instagram blocks anonymous datacenter traffic long before it reaches the CDN.
When the owner supplies a session, the web GraphQL endpoint and the mobile
media-info endpoint both return playable ``video_versions`` for a shortcode.
This module asks those two endpoints directly, so Reels download even when
Cobalt's generic Instagram extractor or yt-dlp are rate-limited.

The returned file may be larger than the Discord upload budget; the downloader
compresses it afterwards instead of rejecting the only available rendition.
"""

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from media_quality import source_cap

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Instagram 275.0.0.27.98 Android (33/13; 280dpi; 720x1423; "
    "Xiaomi; Redmi 7; onclite; qcom; en_US; 458229237)"
)
APP_ID = "936619743392459"
GRAPHQL_DOC_ID = "8845758582119845"
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_NO_COOKIE_HINT = (
    "No Instagram session is configured; add INSTAGRAM_COOKIES or sign in "
    "through the dashboard's Instagram login."
)


def shortcode_to_media_id(shortcode):
    """Convert an Instagram shortcode into its numeric media primary key."""
    if not shortcode:
        return None
    value = 0
    try:
        for character in shortcode:
            value = value * 64 + _ALPHABET.index(character)
    except ValueError:
        return None
    return str(value)


def _cookie_value(header, name):
    for part in (header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return ""


def _desktop_headers(shortcode, cookie_header):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.instagram.com/reel/{shortcode}/",
        "X-IG-App-ID": APP_ID,
        "X-ASBD-ID": "129477",
        "X-Requested-With": "XMLHttpRequest",
        "X-IG-WWW-Claim": "0",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
        csrf = _cookie_value(cookie_header, "csrftoken")
        if csrf:
            headers["X-CSRFToken"] = csrf
    return headers


def _mobile_headers(cookie_header):
    headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US",
        "X-IG-App-ID": APP_ID,
        "X-FB-HTTP-Engine": "Liger",
        "X-FB-Client-IP": "True",
        "X-FB-Server-Cluster": "True",
        "X-IG-WWW-Claim": "0",
        "Content-Length": "0",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
        csrf = _cookie_value(cookie_header, "csrftoken")
        if csrf:
            headers["X-CSRFToken"] = csrf
    return headers


def _json(url, headers, timeout=15):
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024)
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        print(f"[InstaAPI] {urllib.parse.urlsplit(url).hostname} returned HTTP {error.code}")
        return None
    except Exception as error:
        print(f"[InstaAPI] API request failed: {type(error).__name__}")
        return None
    if "json" not in content_type.lower() and body.lstrip()[:1] not in (b"{", b"["):
        print("[InstaAPI] API returned a non-JSON page (login wall or checkpoint)")
        return None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _collect_versions(node, found):
    if isinstance(node, dict):
        versions = node.get("video_versions")
        if isinstance(versions, list):
            for version in versions:
                if not isinstance(version, dict):
                    continue
                url = version.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    found.append(
                        (int(version.get("height") or 0),
                         int(version.get("width") or 0), url)
                    )
        for key in ("video_url", "contentUrl", "playable_url"):
            url = node.get(key)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                found.append(
                    (int(node.get("height") or 0),
                     int(node.get("width") or 0), url)
                )
        for value in node.values():
            _collect_versions(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_versions(value, found)


def video_urls(payload):
    """Return distinct video URLs, highest resolution first."""
    found = []
    _collect_versions(payload, found)
    seen = set()
    ordered = []
    for height, width, url in sorted(found, key=lambda item: (item[0], item[1]), reverse=True):
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def api_payloads(shortcode, cookie_header, timeout=15):
    """Ask every Instagram API route that can return a Reel's video URL."""
    media_id = shortcode_to_media_id(shortcode)
    payloads = []

    if media_id:
        for base in ("https://i.instagram.com", "https://www.instagram.com"):
            payload = _json(
                f"{base}/api/v1/media/{media_id}/info/",
                _mobile_headers(cookie_header),
                timeout,
            )
            if payload:
                payloads.append(payload)

    variables = json.dumps({
        "shortcode": shortcode,
        "include_reel": True,
        "include_logged_out_extras": False,
        "server_timestamps": True,
    }, separators=(",", ":"))
    query = urllib.parse.urlencode({
        "doc_id": GRAPHQL_DOC_ID,
        "variables": variables,
    })
    payload = _json(
        f"https://www.instagram.com/graphql/query/?{query}",
        _desktop_headers(shortcode, cookie_header),
        timeout,
    )
    if payload:
        payloads.append(payload)

    # The older private shortcode endpoint still works from some egress IPs
    # and needs no numeric media id.
    payload = _json(
        f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis",
        _desktop_headers(shortcode, cookie_header),
        timeout,
    )
    if payload:
        payloads.append(payload)
    return payloads


def _download_media(url, cookie_header, cap, timeout):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if not (
        host == "instagram.com"
        or host.endswith(".instagram.com")
        or host.endswith(".cdninstagram.com")
        or host.endswith(".fbcdn.net")
    ):
        print("[InstaAPI] refused a media URL outside Instagram's domains")
        return None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    path = None
    keep = False
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=max(15, timeout)) as response:
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > cap:
                print("[InstaAPI] Reel is larger than the compression source cap")
                return None
            handle, path = tempfile.mkstemp(prefix="midvox_instagram_api_", suffix=".mp4")
            size = 0
            with os.fdopen(handle, "wb") as output:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > cap:
                        print("[InstaAPI] Reel exceeded the compression source cap")
                        return None
                    output.write(chunk)
        if size < 1024:
            return None
        with open(path, "rb") as output:
            head = output.read(12)
        if head[4:8] != b"ftyp" and head[:4] != b"\x1aE\xdf\xa3":
            return None
        keep = True
        return path
    except urllib.error.HTTPError as error:
        print(f"[InstaAPI] media CDN returned HTTP {error.code}")
        return None
    except Exception as error:
        print(f"[InstaAPI] media download failed: {type(error).__name__}")
        return None
    finally:
        if path and not keep and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def download(shortcode, limit, *, cookie_header="", timeout=20, deadline=None):
    """Return a Reel path from Instagram's API, even when it needs compression.

    ``limit`` is the Discord upload budget. The source is allowed to exceed it
    so the caller can encode it down; only the hard compression cap stops a
    download.
    """
    cap = source_cap(limit)
    if deadline is None:
        deadline = time.monotonic() + 120
    payloads = api_payloads(shortcode, cookie_header, timeout=timeout)
    if not payloads and not cookie_header:
        print(f"[InstaAPI] {_NO_COOKIE_HINT}")
    urls = []
    seen = set()
    for payload in payloads:
        for url in video_urls(payload):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    if not urls:
        print("[InstaAPI] no playable video version in the API response")
        return None
    for url in urls:
        if time.monotonic() >= deadline:
            break
        path = _download_media(
            url, cookie_header, cap,
            min(timeout, max(5, deadline - time.monotonic())),
        )
        if path:
            print(f"[InstaAPI] downloaded Reel through Instagram's API ({os.path.getsize(path)} bytes)")
            return path
    return None

"""Normalize browser cookies from Railway variables for yt-dlp.

People paste cookies in wildly different shapes: a raw ``Cookie`` header, a
Cookie-Editor JSON export, a base64-encoded Netscape file, or just a path on
disk. yt-dlp only accepts a Netscape ``cookies.txt`` file, so convert every
supported shape here once and keep the result in the temp directory.
"""

import base64
import json
import os
import tempfile

_HEADER = "# Netscape HTTP Cookie File"
_SAVED = {}


def _first_env(*names):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _cookie_domain(value, default):
    value = (value or default).strip()
    if not value:
        value = default
    value = value.lstrip(".")
    return "." + value if value else ""


def _json_to_netscape(content, default_domain):
    try:
        cookies = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(cookies, list):
        return None
    lines = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").replace("\t", "").replace("\n", "")
        value = str(cookie.get("value") or "").replace("\t", "").replace("\n", "")
        if not name or not value:
            continue
        host_only = bool(cookie.get("hostOnly"))
        domain = _cookie_domain(cookie.get("domain"), default_domain)
        if not domain:
            continue
        if host_only:
            domain = domain.lstrip(".")
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expiry = cookie.get("expirationDate")
        expires = str(int(expiry)) if expiry and not cookie.get("session") else "0"
        lines.append(
            f"{domain}\t{'FALSE' if host_only else 'TRUE'}\t{path}\t"
            f"{secure}\t{expires}\t{name}\t{value}"
        )
    return _HEADER + "\n" + "\n".join(lines) + "\n" if lines else None


def _header_to_netscape(content, default_domain):
    if "=" not in content or "\t" in content:
        return None
    domain = _cookie_domain(None, default_domain)
    if not domain:
        return None
    lines = []
    for part in content.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name or not value or name.startswith("$"):
            continue
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    return _HEADER + "\n" + "\n".join(lines) + "\n" if lines else None


def _looks_like_netscape(content):
    if not content:
        return False
    lines = [line for line in content.splitlines() if line.strip()]
    if not lines:
        return False
    if content.lstrip().startswith(_HEADER):
        return True
    return any(len(line.split("\t")) == 7 for line in lines[:10])


def _decode_base64(content):
    try:
        decoded = base64.b64decode(content, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if _looks_like_netscape(decoded) or decoded.lstrip().startswith(("[", "{")):
        return decoded
    return None


def cookie_file_from_value(raw, default_domain, *, allow_path=True):
    """Return Netscape cookie text for one of the supported input shapes."""
    raw = (raw or "").strip()
    if not raw:
        return None

    if allow_path and ("\n" not in raw and "\t" not in raw and "=" not in raw and ";" not in raw):
        if os.path.isfile(raw):
            try:
                with open(raw, encoding="utf-8", errors="replace") as cookie_file:
                    raw = cookie_file.read().strip()
            except OSError:
                return None

    decoded = _decode_base64(raw)
    if decoded:
        raw = decoded

    if raw.lstrip().startswith(("[", "{")):
        return _json_to_netscape(raw, default_domain)
    if _looks_like_netscape(raw):
        return raw if raw.startswith(_HEADER) else _HEADER + "\n" + raw
    return _header_to_netscape(raw, default_domain)


def _write_cookie_file(prefix, content):
    if not content:
        return None
    key = (prefix, content)
    cached = _SAVED.get(key)
    if cached and os.path.isfile(cached):
        return cached
    path = os.path.join(tempfile.gettempdir(), f"{prefix}-cookies.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _SAVED[key] = path
    return path


def youtube_session_header():
    """Read the YouTube login store written by the dashboard.

    The on-disk format is ``{"youtube": ["SID=...; PREF=..."]}``. The header is
    also useful as a hint for choosing Cobalt's first YouTube client.
    """
    path = os.getenv("COOKIE_PATH", "/tmp/midvox-youtube-cookies.json")
    try:
        with open(path, encoding="utf-8") as cookie_file:
            contents = json.load(cookie_file)
    except (OSError, ValueError):
        return ""
    if not isinstance(contents, dict):
        return ""
    entries = contents.get("youtube")
    if isinstance(entries, str):
        return entries.strip()
    if isinstance(entries, list):
        return "; ".join(str(item).strip() for item in entries if isinstance(item, str) and item.strip())
    return ""


def youtube_cookie_file():
    """Return a Netscape cookie file for yt-dlp, or None when unavailable."""
    raw = _first_env("YT_COOKIES", "YOUTUBE_COOKIES")
    content = cookie_file_from_value(raw, ".youtube.com") if raw else None
    if not content:
        header = youtube_session_header()
        content = cookie_file_from_value(header, ".youtube.com", allow_path=False) if header else None
    return _write_cookie_file("midvox-youtube", content)


def instagram_cookie_header():
    raw = _first_env(
        "INSTAGRAM_COOKIES", "IG_COOKIES",
        "INSTAGRAM_COOKIE_FILE", "IG_COOKIE_FILE",
    )
    if not raw:
        return ""
    content = cookie_file_from_value(raw, ".instagram.com")
    if not content:
        return ""
    pairs = []
    for line in content.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) == 7:
            pairs.append(f"{fields[5]}={fields[6]}")
    return "; ".join(pairs)


def instagram_cookie_file():
    raw = _first_env("INSTAGRAM_COOKIES", "IG_COOKIES", "INSTAGRAM_COOKIE_FILE", "IG_COOKIE_FILE")
    content = cookie_file_from_value(raw, ".instagram.com")
    return _write_cookie_file("midvox-instagram", content)


def instagram_credentials():
    username = _first_env("INSTAGRAM_USERNAME", "IG_USERNAME")
    password = _first_env("INSTAGRAM_PASSWORD", "IG_PASSWORD")
    return (username, password) if username and password else (None, None)

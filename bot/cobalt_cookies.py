"""Hand Instagram cookies to the local Cobalt sidecar before it starts.

Cobalt reads its cookie store once at startup. The YouTube login flow writes
the ``youtube`` entry; this helper adds or refreshes the ``instagram`` entry
without disturbing it, so Cobalt's Instagram extractor can authenticate
instead of being rate-limited as an anonymous visitor.
"""

import json
import os

from media_cookies import instagram_cookie_header


def merge(path, header):
    """Add Instagram cookies to Cobalt's JSON store without losing other keys."""
    try:
        with open(path, encoding="utf-8") as source:
            store = json.load(source)
    except (OSError, ValueError):
        store = {}
    if not isinstance(store, dict):
        store = {}
    store["instagram"] = [header]
    temporary = path + ".instagram"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(store, output)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main():
    header = instagram_cookie_header()
    if not header:
        print("No Instagram session configured; Cobalt will use public routes.")
        return False
    merge(os.getenv("COOKIE_PATH", "/tmp/midvox-youtube-cookies.json"), header)
    print("Instagram session loaded for Cobalt.")
    return True


if __name__ == "__main__":
    main()

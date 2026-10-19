import ast
import asyncio
from pathlib import Path
import unittest

source = Path(__file__).resolve().parents[2] / "docker/youtube_login_server.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
function = next(node for node in tree.body if getattr(node, "name", "") == "youtube_cookie_header")
restore_function = next(node for node in tree.body if getattr(node, "name", "") == "youtube_browser_cookies")
namespace = {}
exec(compile(ast.Module(body=[function, restore_function], type_ignores=[]), str(source), "exec"), namespace)
cookie_header = namespace["youtube_cookie_header"]

browser_namespace = {"asyncio": asyncio, "time": __import__("time"), "json": __import__("json")}
classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
exec(compile(ast.Module(body=classes, type_ignores=[]), str(source), "exec"), browser_namespace)


class BrowserHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_login_collects_only_its_own_api_authorization(self):
        browser = browser_namespace["LoginBrowser"]("discord")
        browser.capture_account_header({"method": "Network.requestWillBeSentExtraInfo", "params": {
            "requestId": "1", "headers": {"authorization": "selected-session"}}})
        self.assertIsNone(browser.account_token)
        browser.capture_account_header({"method": "Network.requestWillBeSent", "params": {
            "requestId": "1", "request": {"url": "https://discord.com/api/v10/users/@me", "headers": {}}}})
        self.assertEqual(browser.account_token, "selected-session")
        youtube = browser_namespace["LoginBrowser"]()
        self.assertIsNone(youtube.account_token)

    async def test_capture_uses_virtual_display_without_gpu_surface(self):
        from unittest.mock import AsyncMock
        browser = browser_namespace["LoginBrowser"]()
        browser.command = AsyncMock(return_value={"data": "image"})
        self.assertEqual(await browser.screenshot(), {"data": "image"})
        self.assertFalse(browser.command.call_args.args[1]["fromSurface"])

    async def test_protocol_crash_requires_reopen_without_capture_retry(self):
        class Socket:
            closed = False
            calls = 0

            async def send_json(self, data):
                self.calls += 1

            def __aiter__(self):
                return self

            async def __anext__(self):
                import json
                from types import SimpleNamespace
                return SimpleNamespace(data=json.dumps({"id": 1, "error": {"code": -32000}}))

        browser = browser_namespace["LoginBrowser"]()
        browser.socket = Socket()
        with self.assertRaises(browser_namespace["BrowserUnavailable"]):
            await browser.screenshot()
        self.assertEqual(browser.socket.calls, 1)

    async def test_closed_connection_requests_reopen(self):
        from types import SimpleNamespace
        browser = browser_namespace["LoginBrowser"]()
        browser.socket = SimpleNamespace(closed=True)
        with self.assertRaises(browser_namespace["BrowserUnavailable"]):
            await browser.command("Page.captureScreenshot")

    async def test_login_opens_account_instead_of_video_feed(self):
        self.assertEqual(browser_namespace["LoginBrowser"]().url, "https://www.youtube.com/account")


class YouTubeLoginTests(unittest.TestCase):
    def test_only_youtube_cookies_saved(self):
        cookies = [{"name": "SID", "value": "session", "domain": ".youtube.com"},
                   {"name": "PREF", "value": "preferences", "domain": "www.youtube.com"},
                   {"name": "OTHER", "value": "secret", "domain": ".google.com"},
                   {"name": "OTHER", "value": "secret", "domain": "evilyoutube.com"}]
        self.assertEqual(cookie_header(cookies), "SID=session; PREF=preferences")

    def test_anonymous_session_cannot_be_saved_as_login(self):
        with self.assertRaisesRegex(ValueError, "Finish signing in"):
            cookie_header([{"name": "PREF", "value": "x", "domain": ".youtube.com"}])

    def test_browser_restore_keeps_cookie_values_and_restricts_domain(self):
        cookies = namespace["youtube_browser_cookies"]({"youtube": ["SID=session==; PREF=value"]})
        self.assertEqual(cookies[0]["value"], "session==")
        self.assertTrue(all(c["domain"] == ".youtube.com" and c["secure"] for c in cookies))

    def test_missing_or_malformed_session_restores_no_cookies(self):
        for contents in ({}, {"youtube": []}, {"youtube": "invalid"}, {"youtube": [None]}):
            self.assertEqual(namespace["youtube_browser_cookies"](contents), [])

"""Exercise the actual deletion wrapper without importing Discord dependencies."""
import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

source = Path(__file__).resolve().parents[1] / "cogs/commands/insta_downloader.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
cog = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "InstaDownloader")
wrapper = next(node for node in cog.body if getattr(node, "name", "") == "_download_and_send")
namespace = {"asyncio": asyncio, "time": time}
exec(compile(ast.Module(body=[wrapper], type_ignores=[]), str(source), "exec"), namespace)


class FailureHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setup_bot(self, mongo=None):
        return SimpleNamespace(mongo=mongo, _failed_downloads=set(), _pending_downloads=set(),
                               _recent_posts={}, _duplicate_window=120,
                               _download_lock=asyncio.Lock(),
                               _send_status=AsyncMock(return_value=None),
                               _download_and_send_inner=AsyncMock(return_value=False))

    def message(self):
        return SimpleNamespace(id=123, guild=SimpleNamespace(id=456), channel=object(), delete=AsyncMock())

    async def test_media_jobs_run_one_at_a_time(self):
        bot = self.setup_bot()
        active = peak = 0

        async def download(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return True

        bot._download_and_send_inner.side_effect = download
        await asyncio.gather(*(
            namespace["_download_and_send"](bot, self.message(), f"short-{index}", {"delete_original": True})
            for index in range(3)
        ))
        self.assertEqual(peak, 1)

    async def test_first_failure_deletes_but_second_failure_preserves(self):
        bot = self.setup_bot()
        first, second = self.message(), self.message()
        for message in (first, second):
            await namespace["_download_and_send"](bot, message, "canonical-reel", {"delete_original": True})
        first.delete.assert_awaited_once()
        second.delete.assert_not_awaited()

    async def test_restart_reads_failure_from_mongo(self):
        collection = SimpleNamespace(find_one=AsyncMock(return_value={"failed": True}), update_one=AsyncMock())
        bot = self.setup_bot(SimpleNamespace(media_download_failures=collection))
        message = self.message()
        await namespace["_download_and_send"](bot, message, "canonical-short", {"delete_original": True}, "youtube")
        message.delete.assert_not_awaited()
        collection.update_one.assert_awaited_once()

    async def test_duplicate_link_while_downloading_is_ignored(self):
        bot = self.setup_bot()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def download(*args):
            calls.append(1)
            started.set()
            await release.wait()
            return True

        bot._download_and_send_inner.side_effect = download
        first = asyncio.create_task(namespace["_download_and_send"](
            bot, self.message(), "same-reel", {"delete_original": False},
        ))
        await started.wait()
        second = asyncio.create_task(namespace["_download_and_send"](
            bot, self.message(), "same-reel", {"delete_original": False},
        ))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(calls), 1)

    async def test_recent_success_is_not_reposted(self):
        bot = self.setup_bot()
        bot._download_and_send_inner.return_value = True
        message = self.message()
        for _ in range(2):
            await namespace["_download_and_send"](
                bot, message, "same-reel", {"delete_original": False},
            )
        bot._download_and_send_inner.assert_awaited_once()

    async def test_success_clears_failed_history_and_opt_out_preserves_link(self):
        collection = SimpleNamespace(find_one=AsyncMock(return_value=None), delete_one=AsyncMock())
        bot = self.setup_bot(SimpleNamespace(media_download_failures=collection))
        bot._download_and_send_inner.return_value = True
        message = self.message()
        await namespace["_download_and_send"](bot, message, "reel", {"delete_original": False})
        message.delete.assert_not_awaited()
        self.assertFalse(bot._failed_downloads)
        collection.delete_one.assert_awaited_once()

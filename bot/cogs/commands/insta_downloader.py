"""
Media Downloader — when an Instagram link or a YouTube Short is posted in
a configured channel, download the media and repost it to Discord so it
plays inline. Instagram embeds don't render in Discord, hence the bot.

Storage mirrors the AntiSpamPlus pattern: SQLite is always written (a
crash/restart-safe mirror), MongoDB is the durable store when reachable.
"""

import discord
from discord.ext import commands
import aiosqlite
import asyncio
import os
import time
import tempfile
from functools import partial
from cobalt_client import CobaltClient
from hosted_downloads import HostedDownloads
from instagram_embed import download as download_instagram_embed
from media_cookies import (
    instagram_cookie_file,
    instagram_cookie_header,
    instagram_credentials,
    youtube_cookie_file,
)
from media_quality import (
    compress_to_budget,
    download_fitting_video,
    is_valid_video,
    upload_budget,
)

from media_links import short_media_link

CONFIG_DEFAULTS = {
    "enabled": False,
    "delete_original": True,
}

# Discord's upload limit varies by plan/server. Keep a safety margin below the
# common 10 MB limit unless the deployment explicitly raises it.
UPLOAD_LIMIT_BYTES = int(os.getenv("DISCORD_UPLOAD_LIMIT", "9500000"))


class InstaDownloader(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._download_lock = asyncio.Lock()
        self._active_status_id = None
        self._hosted_downloads = HostedDownloads()
        self._failed_downloads = set()
        self._pending_downloads = set()
        self._handled_messages = set()
        self._recent_posts = {}
        self._duplicate_window = 120
        self._last_cobalt_error = None

    @property
    def mongo(self):
        return getattr(self.bot, "mongo", None)

    async def _ensure_mongo(self):
        """Lazy-connect Mongo on demand so reads never fall back to an
        empty SQLite mirror right after a redeploy."""
        if self.mongo:
            return True
        from utils.mongo import has_valid_mongo_uri
        mongo_uri = os.getenv("MONGO_URI")
        if not has_valid_mongo_uri(mongo_uri):
            return False
        try:
            from utils.mongo import MongoManager
            mongo = MongoManager()
            await mongo.connect(mongo_uri, server_selection_timeout=5000)
            self.bot.mongo = mongo
            print("\033[32m◈ MongoDB: Connected (lazy, InstaDL)\033[0m")
            return True
        except Exception as e:
            print(f"\033[33m◈ MongoDB: lazy connect failed (InstaDL) — {e}\033[0m")
            return False

    # ── SQLite mirror ──────────────────────────────────────────────────

    def _sqlite_conn(self):
        os.makedirs("db", exist_ok=True)
        return aiosqlite.connect("db/instadl.db")

    async def _ensure_tables(self, db):
        tables = [
            """CREATE TABLE IF NOT EXISTS config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                delete_original INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS channels (
                guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id)
            )""",
        ]
        for t in tables:
            await db.execute(t)
        await db.commit()

    async def _sqlite_get_config(self, guild_id):
        async with self._sqlite_conn() as db:
            await self._ensure_tables(db)
            cursor = await db.execute(
                "SELECT enabled, delete_original FROM config WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cursor.fetchone()
            if not row:
                await db.execute(
                    "INSERT INTO config (guild_id, delete_original) VALUES (?, 1)",
                    (guild_id,),
                )
                await db.commit()
                row = (0, 1)
            elif row[0] and not row[1]:
                # Older installs defaulted to keeping the link. Migrate an
                # enabled downloader to the current always-clean behavior.
                await db.execute(
                    "UPDATE config SET delete_original = 1 WHERE guild_id = ?",
                    (guild_id,),
                )
                await db.commit()
                row = (row[0], 1)
            cursor = await db.execute(
                "SELECT channel_id FROM channels WHERE guild_id = ?", (guild_id,)
            )
            channels = [r[0] for r in await cursor.fetchall()]
            return {
                "guild_id": guild_id,
                "enabled": bool(row[0]),
                "delete_original": bool(row[1]),
                "channels": [str(c) for c in channels],
            }

    # ── Mongo (durable) ────────────────────────────────────────────────

    def _default_doc(self, guild_id):
        return {
            "_id": str(guild_id),
            "guild_id": guild_id,
            **CONFIG_DEFAULTS,
            "channels": [],
        }

    async def _flush_to_mongo(self, guild_id, sqlite_cfg):
        """Push the SQLite mirror state into Mongo (lazy flush on read)."""
        if not self.mongo:
            return
        try:
            doc = self._default_doc(guild_id)
            doc.update(sqlite_cfg)
            doc["guild_id"] = int(guild_id)
            doc["_id"] = str(guild_id)
            doc.pop("_doc_id", None)
            await self.mongo.instadl_config.replace_one(
                {"_id": str(guild_id)}, doc, upsert=True
            )
        except Exception as e:
            print(f"[InstaDL] Mongo flush failed for {guild_id}: {e}")

    async def _mongo_get(self, guild_id):
        if not self.mongo:
            return None
        try:
            return await self.mongo.instadl_config.find_one({"_id": str(guild_id)})
        except Exception as e:
            print(f"[InstaDL] Mongo read failed: {e}")
            return None

    # ── Public API (used by bot/api routes) ────────────────────────────

    async def get_config(self, guild_id):
        await self._ensure_mongo()
        doc = await self._mongo_get(guild_id)
        if doc:
            if doc.get("enabled") and not doc.get("delete_original", False):
                try:
                    await self.mongo.instadl_config.update_one(
                        {"_id": str(guild_id)}, {"$set": {"delete_original": True}}
                    )
                    doc["delete_original"] = True
                except Exception as e:
                    print(f"[InstaDL] delete-original migration failed: {e}")
            return {
                "guild_id": guild_id,
                "enabled": bool(doc.get("enabled", False)),
                "delete_original": bool(doc.get("delete_original", False)),
                "channels": [str(c) for c in doc.get("channels", [])],
            }
        cfg = await self._sqlite_get_config(guild_id)
        # Lazy flush: if Mongo is reachable but the doc is missing, push
        # the mirror state so nothing saved during an outage is lost.
        if self.mongo and (cfg["enabled"] or cfg["channels"] or cfg["delete_original"]):
            await self._flush_to_mongo(guild_id, cfg)
        return cfg

    async def update_config(self, guild_id, data):
        async with self._sqlite_conn() as db:
            await self._ensure_tables(db)
            row = await (await db.execute(
                "SELECT enabled, delete_original FROM config WHERE guild_id = ?",
                (guild_id,),
            )).fetchone()
            enabled = row[0] if row else 0
            delete_original = row[1] if row else 0
            if "enabled" in data:
                enabled = 1 if data["enabled"] else 0
                if enabled and "delete_original" not in data:
                    delete_original = 1
            if "delete_original" in data:
                delete_original = 1 if data["delete_original"] else 0
            await db.execute(
                """INSERT OR REPLACE INTO config (guild_id, enabled, delete_original)
                   VALUES (?, ?, ?)""",
                (guild_id, enabled, delete_original),
            )
            await db.commit()

        await self._ensure_mongo()
        if self.mongo:
            doc = await self._mongo_get(guild_id)
            if doc:
                doc["enabled"] = bool(enabled)
                doc["delete_original"] = bool(delete_original)
                try:
                    await self.mongo.instadl_config.replace_one(
                        {"_id": str(guild_id)}, doc, upsert=True
                    )
                except Exception as e:
                    print(f"[InstaDL] Mongo update failed (mirror kept): {e}")
            else:
                await self._flush_to_mongo(guild_id, await self._sqlite_get_config(guild_id))
        return await self.get_config(guild_id)

    async def add_channel(self, guild_id, channel_id):
        try:
            async with self._sqlite_conn() as db:
                await self._ensure_tables(db)
                await db.execute(
                    "INSERT OR IGNORE INTO channels (guild_id, channel_id) VALUES (?, ?)",
                    (guild_id, channel_id),
                )
                await db.commit()
        except Exception as e:
            print(f"[InstaDL] SQLite mirror write failed: {e}")
        await self._ensure_mongo()
        if self.mongo:
            try:
                await self.mongo.instadl_config.update_one(
                    {"_id": str(guild_id)},
                    {"$addToSet": {"channels": str(channel_id)}},
                    upsert=True,
                )
            except Exception as e:
                print(f"[InstaDL] Mongo channel add failed (mirror kept): {e}")
        return await self.get_config(guild_id)

    async def remove_channel(self, guild_id, channel_id):
        try:
            async with self._sqlite_conn() as db:
                await self._ensure_tables(db)
                await db.execute(
                    "DELETE FROM channels WHERE guild_id = ? AND channel_id = ?",
                    (guild_id, channel_id),
                )
                await db.commit()
        except Exception as e:
            print(f"[InstaDL] SQLite mirror delete failed: {e}")
        await self._ensure_mongo()
        if self.mongo:
            try:
                await self.mongo.instadl_config.update_one(
                    {"_id": str(guild_id)},
                    {"$pull": {"channels": str(channel_id)}},
                )
            except Exception as e:
                print(f"[InstaDL] Mongo channel remove failed (mirror kept): {e}")
        return await self.get_config(guild_id)

    # ── Downloader ─────────────────────────────────────────────────────

    async def _auto_delete(self, msg, delay=60):
        """Delete a bot status message after `delay` seconds (media posts
        are never auto-deleted — only status/error messages use this)."""

        async def _delete_later():
            try:
                await asyncio.sleep(delay)
                await msg.delete()
            except Exception:
                pass

        try:
            asyncio.get_running_loop().create_task(_delete_later())
        except Exception:
            pass

    async def _send_status(self, channel, text, reference=None, auto_delete=60):
        try:
            msg = await channel.send(text, reference=reference)
        except Exception:
            try:
                msg = await channel.send(text)
            except Exception:
                return None
        if auto_delete:
            await self._auto_delete(msg, auto_delete)
        return msg

    @commands.Cog.listener()
    async def on_message(self, message):
        try:
            if message.author.bot or not message.guild:
                return
            if not isinstance(message.channel, discord.TextChannel):
                return

            config = await self.get_config(message.guild.id)
            if not config.get("enabled"):
                return
            if str(message.channel.id) not in config.get("channels", []):
                return

            content = message.content or ""
            media = short_media_link(content)
            if not media:
                return
            source, url, media_id = media

            # Discord can redeliver MESSAGE_CREATE after a gateway resume, and
            # a rolling deploy briefly runs two bot containers. Remember the
            # message ID so one link is not downloaded twice by this process.
            if message.id in self._handled_messages:
                return
            self._handled_messages.add(message.id)
            if len(self._handled_messages) > 4096:
                self._handled_messages.clear()

            print(f"[InstaDL] downloading {source} url {url} for guild {message.guild.id}")
            await self._download_and_send(message, url, config, source)
        except Exception as e:
            print(f"[InstaDL] on_message error: {e}")

    @commands.Cog.listener(name="on_message")
    async def _cleanup_bot_messages(self, message):
        """Auto-delete the bot's own status/error messages in configured
        channels. Media posts (videos/images) are never deleted."""
        try:
            if message.author.id != self.bot.user.id:
                return
            if not message.guild or not isinstance(message.channel, discord.TextChannel):
                return
            config = await self.get_config(message.guild.id)
            if not config.get("enabled"):
                return
            if str(message.channel.id) not in config.get("channels", []):
                return
            if message.attachments or (message.embeds and not message.content):
                return
            if self._active_status_id == message.id:
                return

            await asyncio.sleep(20)

            async def _delete_later():
                try:
                    await message.delete()
                except Exception:
                    pass

            asyncio.get_running_loop().create_task(_delete_later())
        except Exception as e:
            print(f"[InstaDL] cleanup error: {e}")

    def _cobalt_download(self, url, upload_limit=UPLOAD_LIMIT_BYTES, source="youtube"):
        """Download through the bundled Cobalt instance.

        Cobalt extracts and proxies the media, so the bot never has to follow
        a googlevideo/Instagram redirect signed for a different egress IP.
        """
        base_url = os.getenv(
            "COBALT_API_URL", "http://127.0.0.1:9000"
        ).strip().rstrip("/")
        client = CobaltClient(base_url, os.getenv("COBALT_API_KEY", ""))
        path = client.download(url, upload_limit, source=source)
        if not path:
            self._last_cobalt_error = client.last_error
        return path

    def _hosted_download(self, url, upload_limit, source):
        try:
            return self._hosted_downloads.download(
                url, upload_limit, source=source
            )
        except Exception as error:
            print(f"[InstaDL] hosted download failed: {type(error).__name__}")
            return None

    def _ytdlp_download(self, url, upload_limit, source):
        """Cookie-aware yt-dlp fallback for when Cobalt is challenged."""
        try:
            import yt_dlp
        except ImportError:
            return None, "Media Downloader is missing the `yt-dlp` dependency."

        base = {
            "outtmpl": os.path.join(
                tempfile.gettempdir(),
                f"midvox_{int(time.time() * 1000)}_%(id)s.%(ext)s",
            ),
            "format": "b",
            "format_sort": [
                "res", "fps", "vcodec:h264", "vcodec:vp9", "vcodec:av01",
            ],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "max_filesize": upload_limit,
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "concurrent_fragment_downloads": 4,
            "geo_bypass": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        }

        attempts = []
        seen = set()

        def add(options):
            key = tuple(sorted((name, str(value)) for name, value in options.items()))
            if key not in seen:
                seen.add(key)
                attempts.append(options)

        if source == "youtube":
            cookie_file = youtube_cookie_file()
            clients = dict(base)
            clients["extractor_args"] = {
                "youtube": {
                    "player_client": [
                        "ios", "android_vr", "tv", "web_embedded", "mweb",
                    ],
                },
            }
            if cookie_file:
                clients["cookiefile"] = cookie_file
            add(clients)
            if cookie_file:
                add({**base, "cookiefile": cookie_file})
            add(dict(base))
        else:
            cookie_file = instagram_cookie_file()
            username, password = instagram_credentials()
            if cookie_file or username:
                authenticated = dict(base)
                if cookie_file:
                    authenticated["cookiefile"] = cookie_file
                if username:
                    authenticated["username"] = username
                    authenticated["password"] = password
                add(authenticated)
            add(dict(base))
            if cookie_file:
                add({**base, "cookiefile": cookie_file})

        last_error = None
        for options in attempts:
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    path = download_fitting_video(
                        ydl, url, upload_limit, compress=True
                    )
                if path and os.path.isfile(path) and is_valid_video(path):
                    print(f"[InstaDL] yt-dlp downloaded {source} successfully")
                    return path, None
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            except Exception as error:
                last_error = error
                print(f"[InstaDL] yt-dlp attempt failed for {url}: {error}")

        if last_error:
            reason = str(last_error).strip()[:300]
            return None, reason or type(last_error).__name__
        return None, None

    async def _download_and_send(self, message, url, config, source="instagram"):
        """Delete the source link first, then download and repost the media."""
        failure_key = f"{message.guild.id}:{source}:{url}"
        posted_at = self._recent_posts.get(failure_key)
        if posted_at and time.monotonic() - posted_at < self._duplicate_window:
            print(f"[InstaDL] ignoring duplicate {source} link (already posted)")
            return
        if failure_key in self._pending_downloads:
            print(f"[InstaDL] ignoring duplicate {source} link (already downloading)")
            if config.get("delete_original"):
                try:
                    await message.delete()
                except Exception:
                    pass
            return
        # Claim the media before the first await so a second event cannot start
        # another download while this one is still in flight.
        self._pending_downloads.add(failure_key)

        status = await self._send_status(
            message.channel, "Downloading ⏳", reference=None, auto_delete=0
        )
        self._active_status_id = status.id if status else None
        animation = None
        if status:
            # Animate "Downloading . / .. / ..." until the download finishes.
            animation = asyncio.get_running_loop().create_task(
                self._animate_loading(status)
            )

        previously_failed = failure_key in self._failed_downloads
        if self.mongo and not previously_failed:
            try:
                previously_failed = bool(await self.mongo.media_download_failures.find_one({"_id": failure_key}))
            except Exception:
                previously_failed = True
        if config.get("delete_original") and not previously_failed:
            try:
                await message.delete()
            except Exception as e:
                print(f"[InstaDL] could not delete source message {message.id}: {e}")

        succeeded = False
        try:
            # Serialize expensive extraction/encoding to bound peak memory.
            async with self._download_lock:
                succeeded = bool(await self._download_and_send_inner(message, url, config, source))
        except Exception as error:
            print(f"[InstaDL] download crashed for {url}: {type(error).__name__}")
            try:
                await self._send_status(
                    message.channel,
                    "Couldn't download that link right now. Try again shortly.",
                    reference=message,
                )
            except Exception:
                pass
        finally:
            self._pending_downloads.discard(failure_key)
            if succeeded:
                self._failed_downloads.discard(failure_key)
                if len(self._recent_posts) >= 512:
                    cutoff = time.monotonic() - self._duplicate_window
                    self._recent_posts = {
                        key: stamp for key, stamp in self._recent_posts.items()
                        if stamp >= cutoff
                    }
                self._recent_posts[failure_key] = time.monotonic()
            else:
                if len(self._failed_downloads) >= 2048:
                    self._failed_downloads.pop()
                self._failed_downloads.add(failure_key)
            if self.mongo:
                try:
                    if succeeded:
                        await self.mongo.media_download_failures.delete_one({"_id": failure_key})
                    else:
                        await self.mongo.media_download_failures.update_one(
                            {"_id": failure_key}, {"$set": {"failed": True}}, upsert=True,
                        )
                except Exception:
                    print("[InstaDL] could not persist download failure history")
            self._active_status_id = None
            if animation:
                animation.cancel()
            if status:
                try:
                    await status.delete()
                except Exception:
                    pass

    async def _animate_loading(self, status, interval=0.6):
        """Spin 'Downloading' through . → .. → ... while a download runs."""
        dots = ["", ".", "..", "..."]
        i = 0
        try:
            while True:
                await status.edit(content=f"Downloading {dots[i]} ⏳")
                i = (i + 1) % len(dots)
                await asyncio.sleep(interval)
        except (discord.NotFound, discord.HTTPException):
            return
        except Exception:
            return

    async def _send_download_failure(self, message, source, ytdlp_error):
        cobalt_error = self._last_cobalt_error
        if cobalt_error and cobalt_error.user_message:
            text = cobalt_error.user_message
        elif ytdlp_error:
            lowered = str(ytdlp_error).lower()
            if source == "instagram" and any(term in lowered for term in (
                "login", "rate-limit", "rate limit", "empty media response",
                "redirected", "checkpoint", "cookies",
            )):
                if instagram_cookie_header():
                    text = (
                        "Instagram refused the download even with the saved "
                        "session. The session may have expired; refresh "
                        "`INSTAGRAM_COOKIES` in Railway Variables or try again "
                        "later."
                    )
                else:
                    text = (
                        "Instagram is blocking anonymous Reel downloads. Add "
                        "`INSTAGRAM_COOKIES` in Railway Variables to download "
                        "with a session, or try this link again later."
                    )
            elif any(term in lowered for term in (
                "login", "sign in", "confirm you", "bot check", "cookies",
            )):
                text = (
                    "The platform is asking the bot to sign in. Connect a "
                    "session in the dashboard and try the link again."
                )
            elif any(term in lowered for term in (
                "not available", "private", "removed", "unavailable",
            )):
                text = (
                    "Couldn't download that link: it may be private, removed, "
                    "or restricted for the bot's connection."
                )
            else:
                text = "Couldn't download that link right now. Try again shortly."
        else:
            text = "Couldn't download that link right now. Try again shortly."
        await self._send_status(message.channel, text, reference=message)

    async def _send_media(self, message, file_path, source, ext):
        await message.channel.send(
            file=discord.File(
                file_path, filename=f"{source}_{int(time.time())}{ext}"
            )
        )

    async def _download_and_send_inner(self, message, url, config, source="instagram"):
        """Download through every available route, then repost the media inline.

        Order matters: local Cobalt first, hosted Cobalt next, cookie-aware
        yt-dlp last. Instagram also gets a public embed fallback because its
        private API regularly rate-limits datacenter IPs.
        """
        loop = asyncio.get_running_loop()
        self._last_cobalt_error = None
        file_path = None
        ytdlp_error = None
        upload_limit = upload_budget(
            UPLOAD_LIMIT_BYTES, getattr(message.guild, "filesize_limit", None),
        )

        try:
            file_path = await loop.run_in_executor(
                None, self._cobalt_download, url, upload_limit, source,
            )
        except Exception as error:
            print(f"[InstaDL] local Cobalt route failed: {type(error).__name__}")

        if not file_path:
            file_path = await loop.run_in_executor(
                None, self._hosted_download, url, upload_limit, source,
            )

        if not file_path:
            try:
                file_path, ytdlp_error = await loop.run_in_executor(
                    None, self._ytdlp_download, url, upload_limit, source,
                )
            except Exception as error:
                ytdlp_error = str(error)
                print(f"[InstaDL] yt-dlp route failed: {type(error).__name__}")

        if not file_path and source == "instagram":
            shortcode = url.rstrip("/").rsplit("/", 1)[-1]
            try:
                file_path = await loop.run_in_executor(
                    None,
                    partial(
                        download_instagram_embed,
                        shortcode,
                        upload_limit,
                        cookie_header=instagram_cookie_header(),
                    ),
                )
            except Exception as error:
                print(f"[InstaDL] Instagram embed route failed: {type(error).__name__}")
            else:
                if file_path:
                    print("[InstaDL] Instagram embed downloaded successfully")

        if not file_path:
            print(f"[InstaDL] all download routes failed for {url}")
            await self._send_download_failure(message, source, ytdlp_error)
            return

        if not os.path.exists(file_path) or not is_valid_video(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
            print(f"[InstaDL] rejected invalid media returned for {url}")
            await self._send_status(
                message.channel,
                "The platform returned an invalid media file. Try again shortly.",
                reference=message,
            )
            return

        try:
            size = os.path.getsize(file_path)
            if size > upload_limit:
                compressed = await loop.run_in_executor(
                    None, compress_to_budget, file_path, upload_limit,
                )
                if not compressed:
                    await self._send_status(
                        message.channel,
                        "That media is too large for this server's upload limit.",
                        reference=message,
                    )
                    return
                try:
                    os.remove(file_path)
                except OSError:
                    pass
                file_path = compressed
                size = os.path.getsize(file_path)
                print(f"[InstaDL] compressed media to {size} bytes")

            ext = os.path.splitext(file_path)[1].lower()
            if ext not in (".mp4", ".webm", ".mov", ".m4v"):
                ext = ".mp4"

            try:
                await self._send_media(message, file_path, source, ext)
            except discord.HTTPException as error:
                print(f"[InstaDL] Discord upload failed: {error}")
                lowered = str(error).lower()
                too_large = (
                    getattr(error, "code", None) in (40005, 413)
                    or "too large" in lowered
                )
                if too_large:
                    try:
                        retry_limit = min(
                            upload_limit,
                            max(1_000_000, int(os.path.getsize(file_path) * 0.7)),
                        )
                    except OSError:
                        retry_limit = upload_limit
                    compressed = await loop.run_in_executor(
                        None, compress_to_budget, file_path, retry_limit,
                    )
                    if compressed:
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass
                        file_path = compressed
                        try:
                            await self._send_media(message, file_path, source, ext)
                            return True
                        except discord.HTTPException as retry_error:
                            print(f"[InstaDL] compressed upload failed: {retry_error}")
                await self._send_status(
                    message.channel,
                    "Discord rejected the upload. Try again shortly.",
                    reference=message,
                )
                return
            return True
        finally:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass


def setup(bot):
    bot.add_cog(InstaDownloader(bot))

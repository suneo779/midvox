import asyncio
import contextlib
import glob
import json
import os
import random
import signal
import string
from urllib.parse import urlsplit

import aiohttp
from desktop_health import DesktopHealth
from desktop_awake import DesktopAwake
from account_session import configured_account_token, saved_browser_account


TOKEN = configured_account_token()
CHANNEL_ID = os.environ.get("BUMP_CHANNEL_ID", "")
INTERVAL = int(os.getenv("BUMP_INTERVAL", "600"))
DISBOARD_APP_ID = "302050872383242240"
BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
API_BASE = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
SESSION_ID = ""
SESSION_READY = asyncio.Event()
DISCORD_DESKTOP = os.getenv("DISCORD_DESKTOP_BIN") or next(
    iter(
        sorted(
            glob.glob("/var/lib/midvox/discord-home/discord/app-*/Discord"),
            reverse=True,
        )
        + [
            path
            for path in ("/opt/Discord/Discord", "/opt/Discord/discord")
            if os.path.isfile(path)
        ]
    ),
    "/opt/Discord/discord",
)
DISCORD_CONFIG_HOME = os.getenv(
    "DISCORD_CONFIG_HOME", "/var/lib/midvox/discord-home"
)
USER_DESKTOP_READY_FILE = os.getenv(
    "USER_DESKTOP_READY_FILE", "/tmp/midvox-user-desktop-ready"
)
# Readiness marker for the companion Gateway used by slash interactions.
USER_GATEWAY_READY_FILE = os.getenv(
    "USER_GATEWAY_READY_FILE", "/tmp/midvox-user-gateway-ready"
)


def nonce() -> str:
    return "".join(random.choices(string.digits, k=18))


class InvalidCredential(RuntimeError):
    """Discord explicitly rejected authentication; operator action is required."""


async def validate_user_token(session: aiohttp.ClientSession) -> None:
    """Validate the token outside the Electron renderer.

    Renderer reloads can make an in-page fetch return an unrelated 400 even
    though the same token authenticated the desktop Gateway. A direct API
    check provides a stable startup signal instead.
    """
    async with session.get(
        f"{API_BASE}/users/@me",
        headers={"Authorization": TOKEN, "User-Agent": BROWSER_USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as response:
        if response.status == 401:
            raise InvalidCredential("Discord rejected the selected account login (HTTP 401). Sign in again in Admin Panel > Discord login, or update USER_TOKEN.")
        if response.status != 200:
            # A 403 can come from an edge challenge. Do not call it a revoked
            # credential or permanently disable the worker on that evidence.
            raise RuntimeError(f"Discord credential check HTTP {response.status}")
        if (await response.json()).get("bot"):
            raise InvalidCredential("USER_TOKEN belongs to a bot, not the account worker")


async def fetch_bump_command(session: aiohttp.ClientSession, headers: dict, guild_id: str):
    # Query DISBOARD directly first, then fall back to the complete guild index.
    # The channel index can contain unrelated user-installed applications.
    urls = [
        f"{API_BASE}/applications/{DISBOARD_APP_ID}/application-command-index",
        f"{API_BASE}/guilds/{guild_id}/application-command-index",
    ]
    for url in urls:
        async with session.get(url, headers=headers) as r:
            body = await r.text()
            source = "DISBOARD" if f"applications/{DISBOARD_APP_ID}" in url else "guild"
            print(f"[bump] {source} command index HTTP {r.status}", flush=True)
            if r.status != 200:
                continue
            try:
                data = json.loads(body)
            except Exception:
                print(f"[bump] {source} command index returned invalid JSON", flush=True)
                continue

            cmds = data.get("application_commands") or data.get("commands") or []
            if isinstance(cmds, dict):
                cmds = list(cmds.values())
            for c in cmds:
                if not isinstance(c, dict):
                    continue
                application_id = str(c.get("application_id") or DISBOARD_APP_ID)
                if (
                    application_id == DISBOARD_APP_ID
                    and c.get("name") == "bump"
                    and int(c.get("type") or 1) == 1
                ):
                    print(
                        f"[bump] selected DISBOARD /bump id={c['id']} "
                        f"application={application_id}",
                        flush=True,
                    )
                    return c
            print(f"[bump] {source} index had {len(cmds)} commands; DISBOARD /bump absent", flush=True)
    return None


async def send_bump(session: aiohttp.ClientSession, headers: dict) -> None:
    if not SESSION_ID:
        try:
            await asyncio.wait_for(SESSION_READY.wait(), timeout=30)
        except TimeoutError:
            print("[bump] gateway session not ready; delaying bump", flush=True)
            return

    async with session.get(f"{API_BASE}/channels/{CHANNEL_ID}", headers=headers) as r:
        if r.status != 200:
            if r.status in (403, 404):
                print(f"[bump] channel {CHANNEL_ID} is unavailable to this account (HTTP {r.status}); check membership, View Channel permission, and BUMP_CHANNEL_ID", flush=True)
            else:
                print(f"[bump] channel lookup HTTP {r.status}", flush=True)
            return
        guild_id = (await r.json()).get("guild_id")
        if not guild_id:
            print("[bump] no guild_id", flush=True)
            return

    cmd = await fetch_bump_command(session, headers, str(guild_id))
    if not cmd:
        print("[bump] /bump not found — Disboard not in this guild/channel or still down", flush=True)
        return

    payload = {
        "type": 2,
        "application_id": DISBOARD_APP_ID,
        "guild_id": str(guild_id),
        "channel_id": str(CHANNEL_ID),
        "session_id": SESSION_ID,
        "data": {
            "version": str(cmd.get("version") or cmd["id"]),
            "id": str(cmd["id"]),
            "name": "bump",
            "type": 1,
            "options": [],
            "application_command": {
                "id": str(cmd["id"]),
                "application_id": DISBOARD_APP_ID,
                "version": str(cmd.get("version") or cmd["id"]),
                "type": 1,
                "name": "bump",
                "description": cmd.get("description") or "Bump this server",
                "dm_permission": cmd.get("dm_permission", True),
                "integration_types": cmd.get("integration_types") or [0],
            },
            "attachments": [],
        },
        "nonce": nonce(),
    }

    async with session.post(f"{API_BASE}/interactions", headers=headers, json=payload) as r:
        body = await r.text()
        print(f"[bump] interaction {r.status} {body}", flush=True)
        if r.status == 429:
            try:
                retry = float(json.loads(body).get("retry_after", 10))
            except Exception:
                retry = 10
            await asyncio.sleep(retry)


async def heartbeat(ws, interval, sequence, acknowledged=None):
    """Keep the user session alive until the gateway connection closes."""
    await asyncio.sleep(interval)
    while not ws.closed:
        if acknowledged is not None:
            if not acknowledged[0]:
                print("[interaction-gateway] heartbeat acknowledgement missing; reconnecting", flush=True)
                await ws.close()
                return
            acknowledged[0] = False
        await ws.send_json({"op": 1, "d": sequence[0]})
        await asyncio.sleep(interval)


async def cdp_call(ws, request_id, method, params=None):
    """Send one Chrome DevTools command and wait for its response."""
    payload = {"id": request_id, "method": method}
    if params:
        payload["params"] = params
    await ws.send_json(payload)
    while True:
        message = await asyncio.wait_for(ws.receive(), timeout=20)
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError("Discord desktop DevTools connection closed")
        response = json.loads(message.data)
        if response.get("id") != request_id:
            continue
        if response.get("error"):
            raise RuntimeError(response["error"].get("message") or "DevTools error")
        result = response.get("result") or {}
        if result.get("exceptionDetails"):
            # Never echo evaluated source: a login expression may contain a secret.
            raise RuntimeError("Discord desktop JavaScript evaluation failed")
        return result


async def authenticate_desktop(session, target, debug_port):
    """Authenticate the native app without replacing it with the web client."""
    async with session.get(f"http://127.0.0.1:{debug_port}/json/list") as response:
        targets = await response.json()
    target = select_desktop_target(targets, target)
    if not target:
        # Log only origin/path; DevTools URLs may contain authentication query
        # parameters, which must never be copied into deployment logs.
        documents = []
        for item in targets:
            if item.get("type") == "page":
                parsed = urlsplit(str(item.get("url") or ""))
                documents.append(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
        raise RuntimeError(
            "Native Discord app target did not become available; documents="
            + repr(documents)
        )

    async with session.ws_connect(target["webSocketDebuggerUrl"]) as ws:
        parsed = urlsplit(target["url"])
        storage_id = {
            "securityOrigin": f"{parsed.scheme}://{parsed.netloc}",
            "isLocalStorage": True,
        }
        # Discord removes window.localStorage from the page. Write through CDP
        # rather than evaluating JavaScript against that missing global.
        await cdp_call(
            ws,
            2,
            "DOMStorage.setDOMStorageItem",
            {
                "storageId": storage_id,
                "key": "token",
                "value": json.dumps(TOKEN),
            },
        )
        stored = await cdp_call(
            ws, 3, "DOMStorage.getDOMStorageItems", {"storageId": storage_id}
        )
        if dict(stored.get("entries", [])).get("token") != json.dumps(TOKEN):
            raise RuntimeError("Discord desktop credential storage verification failed")
        # Reload lets the client consume the stored credential during startup.
        # Success here is only a handoff; Gateway READY is checked separately.
        await cdp_call(ws, 4, "Page.reload", {})
    return False


def select_desktop_target(targets, original):
    """Keep the known native window when its app document redirects to login."""
    pages = [
        item
        for item in targets
        if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
    ]
    for item in pages:
        same_window = (
            original.get("id") and item.get("id") == original["id"]
        ) or (
            item.get("webSocketDebuggerUrl") == original.get("webSocketDebuggerUrl")
        )
        parsed = urlsplit(str(item.get("url") or ""))
        if (
            same_window
            and parsed.scheme == "https"
            and parsed.netloc in {"discord.com", "discordapp.com"}
        ):
            return item
    return next(
        (
            item
            for item in pages
            if is_desktop_app_url(str(item.get("url") or ""))
        ),
        None,
    )


def is_desktop_app_url(url: str) -> bool:
    """Accept the official desktop app document, excluding login/web pages."""
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"discord.com", "discordapp.com"}
        and parsed.path.rstrip("/") == "/app"
    )


async def relay_desktop_output(stream, health=None):
    """Drain Discord output and retain useful startup diagnostics."""
    line_count = 0
    while True:
        line = await stream.readline()
        if not line:
            return
        line_count += 1
        text = line.decode("utf-8", errors="replace").strip()
        important = any(
            word in text.lower()
            for word in (
                "error",
                "fail",
                "install",
                "update",
                "module",
                "ready",
                "devtools",
                "version",
                "[spotify]",
                "[gatewaysocket]",
                "[fast connect]",
            )
        )
        if text and (line_count <= 80 or important):
            print(f"[discord-desktop] {text}", flush=True)
        transition = health.observe(text) if health else None
        if transition == "spotify_disconnected":
            print(
                "[desktop-health] Spotify socket disconnected; allowing 120s "
                "for Discord's own reconnect",
                flush=True,
            )
        elif transition == "spotify_connected":
            print("[desktop-health] native Spotify socket connected", flush=True)
        if transition == "disconnected":
            with contextlib.suppress(OSError):
                os.remove(USER_DESKTOP_READY_FILE)
            print(
                "[desktop-health] native Gateway disconnected; allowing 120s "
                "for Discord's own reconnect",
                flush=True,
            )
        if transition == "connected" or (
            health is None and "[GatewaySocket] [READY]" in text
        ):
            with contextlib.suppress(OSError):
                with open(
                    USER_DESKTOP_READY_FILE,
                    "w",
                    encoding="utf-8",
                ) as ready_file:
                    ready_file.write("ready")
            print(
                "[user-desktop] ONLINE as Linux Desktop; native Gateway established",
                flush=True,
            )


async def stop_desktop_process(process):
    """Stop the desktop bootstrapper and any children it launched."""
    if not process:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    # The launcher can already have exited while Electron children remain.
    # Wait for the entire group, not just the launcher, before relaunching.
    for _ in range(10):
        if not desktop_process_group_alive(process):
            break
        await asyncio.sleep(0.5)
    if desktop_process_group_alive(process):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        await asyncio.wait_for(process.wait(), timeout=5)


def desktop_process_group_alive(process) -> bool:
    """Return whether any process from the launched desktop session remains.

    Discord may close its DevTools listener or let its launcher process exit
    while the detached Electron client and Gateway remain healthy.  Every
    process inherits the new session/process group created at launch, making
    the group the durable lifecycle signal.
    """
    if not process:
        return False
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def discord_desktop_client():
    """Keep Discord's official desktop client open on a virtual display."""
    global SESSION_ID

    if not os.path.isfile(DISCORD_DESKTOP):
        raise RuntimeError("Discord desktop is missing from the container")

    async with aiohttp.ClientSession() as validation_session:
        await validate_user_token(validation_session)
    print("[discord-desktop] user token validated", flush=True)

    debug_port = 9222
    while True:
        with contextlib.suppress(OSError):
            os.remove(USER_DESKTOP_READY_FILE)
        display_process = None
        discord_process = None
        output_task = None
        desktop_health = DesktopHealth()
        try:
            display_process = await asyncio.create_subprocess_exec(
                "Xvfb",
                ":99",
                "-screen",
                "0",
                "1280x720x24",
                "-nolisten",
                "tcp",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(1)
            if display_process.returncode is not None:
                raise RuntimeError(
                    f"Xvfb exited with code {display_process.returncode}"
                )

            desktop_environment = os.environ.copy()
            desktop_environment["DISPLAY"] = ":99"
            desktop_environment["XDG_CONFIG_HOME"] = DISCORD_CONFIG_HOME
            desktop_environment["XDG_RUNTIME_DIR"] = "/tmp/midvox-runtime"
            # Railway has no physical GPU. Keep Mesa's CPU renderer available
            # for Electron instead of disabling every rendering backend.
            desktop_environment["LIBGL_ALWAYS_SOFTWARE"] = "1"
            os.makedirs(
                desktop_environment["XDG_RUNTIME_DIR"],
                mode=0o700,
                exist_ok=True,
            )
            print(
                "[discord-desktop] starting official client on virtual display :99",
                flush=True,
            )
            discord_process = await asyncio.create_subprocess_exec(
                "dbus-run-session",
                "--",
                DISCORD_DESKTOP,
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Disable hardware acceleration while preserving software
                # rasterization. Disabling both caused Electron's fatal
                # "GPU process isn't usable" crash before login.
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-extensions",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--mute-audio",
                "--renderer-process-limit=2",
                f"--remote-debugging-port={debug_port}",
                "--user-data-dir=/tmp/midvox-discord-desktop",
                env=desktop_environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            output_task = asyncio.create_task(
                relay_desktop_output(discord_process.stdout, desktop_health)
            )

            async with aiohttp.ClientSession() as session:
                target = None
                exited_at = None
                for attempt in range(600):
                    try:
                        async with session.get(
                            f"http://127.0.0.1:{debug_port}/json/list"
                        ) as response:
                            targets = await response.json()
                        pages = [
                            item
                            for item in targets
                            if item.get("type") == "page"
                            and item.get("webSocketDebuggerUrl")
                        ]
                        # The official client first exposes a file:// updater
                        # splash target. Injecting into that target tears down the
                        # bootstrap and kills the browser. Wait for Discord's
                        # stable main application target instead.
                        target = next(
                            (
                                item
                                for item in pages
                                if is_desktop_app_url(str(item.get("url") or ""))
                            ),
                            None,
                        )
                        if target:
                            break
                    except (aiohttp.ClientError, ValueError):
                        pass
                    if discord_process.returncode is not None:
                        exited_at = exited_at if exited_at is not None else attempt
                        if attempt - exited_at >= 60:
                            raise RuntimeError(
                                "Discord desktop bootstrapper exited with code "
                                f"{discord_process.returncode} and no client started"
                            )
                    if attempt in (60, 180, 360):
                        print(
                            "[discord-desktop] still waiting for the main app "
                            "window to become ready",
                            flush=True,
                        )
                    await asyncio.sleep(0.5)
                if not target:
                    raise RuntimeError("Discord desktop DevTools did not become ready")

                # Let the initial login renderer finish before touching it.
                # Reloading Electron while splash/main boot is still active causes
                # Chromium exitCode=11.
                await asyncio.sleep(8)
                if os.path.isfile(USER_DESKTOP_READY_FILE):
                    print(
                        "[discord-desktop] authenticated Gateway already ready; "
                        "authentication handoff skipped",
                        flush=True,
                    )
                else:
                    try:
                        token_reused = await authenticate_desktop(
                            session,
                            target,
                            debug_port,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (aiohttp.ClientError, RuntimeError, ValueError) as error:
                        detail = str(error)
                        if TOKEN:
                            detail = detail.replace(TOKEN, "[redacted]")
                        raise RuntimeError(
                            "Desktop authentication handoff failed: "
                            f"{type(error).__name__}: {detail}"
                        ) from error
                    if token_reused:
                        print(
                            "[discord-desktop] existing discord.com authentication "
                            "reused",
                            flush=True,
                        )
                    else:
                        print(
                            "[discord-desktop] native app authentication stored; "
                            "waiting for authenticated Gateway READY",
                            flush=True,
                        )

                for _ in range(90):
                    if os.path.isfile(USER_DESKTOP_READY_FILE):
                        break
                    if not desktop_process_group_alive(discord_process):
                        raise RuntimeError("Discord exited before authentication completed")
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError(
                        "Native Discord did not report Gateway READY within 90s; "
                        "process startup alone is not an authenticated desktop session"
                    )

                # Discord can detach from its launcher and can close the
                # optional DevTools listener while its Gateway stays ready.
                # Leave the authenticated official client open and let it own
                # status and linked-account activity. A local pointer pulse
                # prevents Xvfb inactivity from suppressing native updates.
                output_closed_reported = False
                awake = DesktopAwake()
                next_awake = 0
                awake_failed = False
                print(
                    "[desktop-health] checking desktop/display and native "
                    "connection state every 1s",
                    flush=True,
                )
                while desktop_process_group_alive(discord_process):
                    if display_process.returncode is not None:
                        raise RuntimeError(
                            "Virtual display exited; restarting the desktop session"
                        )
                    now = asyncio.get_running_loop().time()
                    if now >= next_awake:
                        succeeded = awake.pulse()
                        if not succeeded and not awake_failed:
                            print("[desktop-health] virtual desktop wake failed", flush=True)
                        elif succeeded and (next_awake == 0 or awake_failed):
                            print(
                                "[desktop-health] local desktop keep-awake active every 60s",
                                flush=True,
                            )
                        awake_failed = not succeeded
                        next_awake = now + awake.INTERVAL_SECONDS
                    if desktop_health.reconnect_stalled():
                        raise RuntimeError(
                            "Native Gateway reconnect stalled for 120s; "
                            "restarting the official desktop to recover"
                        )
                    if desktop_health.spotify_reconnect_stalled():
                        raise RuntimeError(
                            "Native Spotify socket did not connect within 120s; "
                            "restarting the official desktop to recover"
                        )
                    if output_task.done() and not output_closed_reported:
                        # Discord can legitimately detach from its launcher.
                        # Report lost diagnostics without repeatedly restarting
                        # an otherwise running client merely because stdout closed.
                        await output_task
                        output_closed_reported = True
                        print(
                            "[discord-desktop] output stream closed while process "
                            "group remains alive; session health is unconfirmed",
                            flush=True,
                        )
                    await asyncio.sleep(1)
                raise RuntimeError("Discord desktop process group exited")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[discord-desktop] error: {error}", flush=True)
        finally:
            with contextlib.suppress(OSError):
                os.remove(USER_DESKTOP_READY_FILE)
            await stop_desktop_process(discord_process)
            if output_task:
                output_task.cancel()
                await asyncio.gather(output_task, return_exceptions=True)
            if display_process and display_process.returncode is None:
                display_process.terminate()
                try:
                    await asyncio.wait_for(display_process.wait(), timeout=5)
                except TimeoutError:
                    display_process.kill()
                    await display_process.wait()

        print("[discord-desktop] restarting client in 5s", flush=True)
        await asyncio.sleep(5)


def gateway_identify_payload() -> dict:
    """Build a quiet companion session used only for slash interactions."""
    return {
        "op": 2,
        "d": {
            "token": TOKEN,
            "capabilities": 4093,
            "properties": {
                "os": "Linux",
                "browser": "Discord Client",
                "device": "",
                "system_locale": "en-US",
                "browser_user_agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
                "browser_version": "148.0.0.0",
                "os_version": "6",
                "referrer": "",
                "referring_domain": "",
            },
            "compress": False,
            "client_state": {
                "guild_versions": {},
                "highest_last_message_id": "0",
                "read_state_version": 0,
                "user_guild_settings_version": -1,
                "user_settings_version": -1,
                "private_channels_version": "0",
                "api_code_version": 0,
            },
        },
    }


async def connect_interaction_gateway():
    """Maintain a quiet Gateway session for the scheduled bump interaction."""
    global SESSION_ID

    while True:
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": BROWSER_USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.ws_connect(GATEWAY, heartbeat=None) as ws:
                    hello = await ws.receive_json()
                    if hello.get("op") != 10:
                        raise RuntimeError("Discord gateway did not send HELLO")

                    interval = hello["d"]["heartbeat_interval"] / 1000
                    sequence = [None]
                    acknowledged = [True]
                    await ws.send_json(gateway_identify_payload())

                    heartbeat_task = asyncio.create_task(heartbeat(ws, interval, sequence, acknowledged))
                    print("[interaction-gateway] identified; waiting for READY", flush=True)
                    try:
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(message.data)
                                if payload.get("s") is not None:
                                    sequence[0] = payload["s"]
                                if payload.get("op") == 0 and payload.get("t") == "READY":
                                    SESSION_ID = str(payload.get("d", {}).get("session_id") or "")
                                    if SESSION_ID:
                                        with contextlib.suppress(OSError):
                                            with open(
                                                USER_GATEWAY_READY_FILE,
                                                "w",
                                                encoding="utf-8",
                                            ) as ready_file:
                                                ready_file.write(SESSION_ID)
                                        SESSION_READY.set()
                                if payload.get("op") == 11:
                                    acknowledged[0] = True
                                if payload.get("op") == 1:
                                    await ws.send_json({"op": 1, "d": sequence[0]})
                                elif payload.get("op") in (7, 9):
                                    break
                            elif message.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                        if ws.close_code == 4004:
                            raise InvalidCredential("Discord Gateway rejected USER_TOKEN")
                    finally:
                        SESSION_ID = ""
                        SESSION_READY.clear()
                        with contextlib.suppress(OSError):
                            os.remove(USER_GATEWAY_READY_FILE)
                        heartbeat_task.cancel()
                        await asyncio.gather(
                            heartbeat_task,
                            return_exceptions=True,
                        )
        except InvalidCredential:
            raise
        except Exception as e:
            print(f"[interaction-gateway] error: {e}", flush=True)

        print("[interaction-gateway] reconnecting in 5s", flush=True)
        await asyncio.sleep(5)


async def loop():
    headers = {
        "Authorization": TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Super-Properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2aWNlIjoiIiwic3lzdGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyMC4wLjAuMCBTYWZhcmkvNTM3LjM2IiwiYnJvd3Nlcl92ZXJzaW9uIjoiMTIwLjAuMC4wIiwib3NfdmVyc2lvbiI6IjEwIiwicmVmZXJyZXIiOiIiLCJyZWZlcnJpbmdfZG9tYWluIjoiIiwicmVmZXJyZXJfY3VycmVudCI6IiIsInJlZmVycmluZ19kb21haW5fY3VycmVudCI6IiIsInJlbGVhc2VfY2hhbm5lbCI6InN0YWJsZSIsImNsaWVudF9idWlsZF9udW1iZXIiOjIyMzgxNiwiY2xpZW50X2V2ZW50X3NvdXJjZSI6bnVsbH0=",
    }
    print(
        f"[bump] start channel={CHANNEL_ID} random interval {INTERVAL}-{INTERVAL * 1.5:g}s",
        flush=True,
    )
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while True:
            try:
                await send_bump(session, headers)
            except Exception as e:
                print(f"[bump] error: {e}", flush=True)
            delay = random.uniform(INTERVAL, INTERVAL * 1.5)
            print(f"[bump] next attempt in {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)


async def supervise(name, coroutine_factory):
    """Restart one sidecar component without stopping the other components."""
    while True:
        try:
            await coroutine_factory()
        except (asyncio.CancelledError, InvalidCredential):
            raise
        except Exception as error:
            print(f"[{name}] crashed: {error}", flush=True)
        print(f"[{name}] restarting in 5s", flush=True)
        await asyncio.sleep(5)


if __name__ == "__main__":
    if not TOKEN:
        print("[user-session] Sign in through Admin Panel > Discord login or configure USER_TOKEN.", flush=True)
        raise SystemExit(78)

    async def main():
        with open("/tmp/midvox-bump-worker.pid", "w") as marker:
            marker.write(str(os.getpid()))
        # Validate once before starting expensive clients. Transient failures
        # receive bounded retries from the outer supervisor; 401 stops only
        # this optional worker, never the primary bot.
        async with aiohttp.ClientSession() as session:
            await validate_user_token(session)
        print("[user-session] credential accepted; starting independent components", flush=True)
        account = saved_browser_account()
        print(f"[user-session] account source: {'saved browser login ' + str(account['id']) if account else 'USER_TOKEN'}", flush=True)
        tasks = [supervise("interaction-gateway", connect_interaction_gateway)]
        if os.getenv("DISCORD_DESKTOP_ENABLED", "false").lower() == "true" and os.path.isfile(DISCORD_DESKTOP):
            tasks.append(supervise("discord-desktop", discord_desktop_client))
        else:
            print("[user-session] desktop disabled; using lightweight bump Gateway", flush=True)
        if CHANNEL_ID:
            tasks.append(supervise("bump", loop))
        else:
            print("[bump] disabled: BUMP_CHANNEL_ID is not configured", flush=True)
        stopped = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            event_loop.add_signal_handler(signum, stopped.set)
        running = asyncio.gather(*tasks)
        shutdown = asyncio.create_task(stopped.wait())
        try:
            done, _ = await asyncio.wait((running, shutdown), return_when=asyncio.FIRST_COMPLETED)
            if running in done:
                await running
        finally:
            running.cancel()
            shutdown.cancel()
            await asyncio.gather(running, shutdown, return_exceptions=True)

    try:
        asyncio.run(main())
    except InvalidCredential as error:
        print(f"[user-session] {error}", flush=True)
        raise SystemExit(78)

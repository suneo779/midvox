"""Choose the highest single-file video that fits a Discord upload."""

import os
import shutil
import subprocess
import tempfile
import json
import math


def upload_budget(configured, guild_limit=None):
    return min(configured, int(guild_limit * 0.98)) if guild_limit else configured


def source_cap(limit):
    """Largest source worth downloading for a later re-encode.

    Renditions above the Discord budget are kept and compressed, so the cap is
    intentionally larger than the upload limit. It only prevents a runaway
    download from exhausting the container.
    """
    try:
        configured = int(os.getenv("MEDIA_SOURCE_MAX_BYTES", "104857600"))
    except (TypeError, ValueError):
        configured = 104857600
    return max(limit, configured)


def is_valid_video(path):
    """Reject HTML/JSON error bodies saved with a .mp4 name."""
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as media:
            head = media.read(12)
    except OSError:
        return False
    return head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3"


def _codec_rank(fmt):
    codec = str(fmt.get("vcodec") or "").lower()
    # Prefer formats Discord clients play everywhere, then fall back to
    # increasingly efficient codecs only when H.264 is unavailable.
    if codec.startswith("avc") or codec.startswith("h264"):
        return 3
    if codec.startswith("vp9") or codec.startswith("vp09"):
        return 2
    if codec.startswith("av01") or codec.startswith("av1"):
        return 1
    return 0


def fitting_formats(info, limit):
    candidates = []
    for fmt in info.get("formats") or [info]:
        if not fmt.get("url") or fmt.get("vcodec") == "none" or fmt.get("acodec") == "none":
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if not size and fmt.get("tbr") and info.get("duration"):
            size = fmt["tbr"] * 1000 * info["duration"] / 8
        if size and size > limit:
            continue
        candidates.append(fmt)
    return sorted(candidates, key=lambda f: (
        f.get("height") or 0, f.get("width") or 0,
        f.get("fps") or 0, _codec_rank(f), f.get("tbr") or 0,
    ), reverse=True)


def download_fitting_video(ydl, url, limit, *, compress=False):
    info = ydl.extract_info(url, download=False)
    for fmt in fitting_formats(info, limit):
        selected = dict(info, formats=[fmt])
        path = None
        try:
            result = ydl.process_ie_result(selected, download=True)
            path = ydl.prepare_filename(result) if result else None
        except Exception as error:
            print(f"[InstaDL] yt-dlp format failed: {type(error).__name__}")
            continue
        if path and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if 0 < size <= limit:
                return path
            try:
                os.remove(path)
            except OSError:
                pass
    if compress:
        # Instagram can expose one oversized rendition with no lower qualities.
        # Download only the smallest complete source, then encode it to budget.
        sources = fitting_formats(info, float("inf"))
        source_limit = source_cap(limit)
        sources = [f for f in sources if not (f.get("filesize") or f.get("filesize_approx"))
                   or (f.get("filesize") or f.get("filesize_approx")) <= source_limit]
        if sources:
            fmt = min(sources, key=lambda f: (
                f.get("filesize") or f.get("filesize_approx") or float("inf"),
                f.get("height") or float("inf"), f.get("tbr") or float("inf"),
            ))
            previous = ydl.params.get("max_filesize")
            try:
                ydl.params["max_filesize"] = source_limit
                try:
                    result = ydl.process_ie_result(dict(info, formats=[fmt]), download=True)
                    path = ydl.prepare_filename(result) if result else None
                except Exception as error:
                    print(f"[InstaDL] yt-dlp compress source failed: {type(error).__name__}")
                    path = None
                if path and os.path.isfile(path):
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    try:
                        if 0 < size <= limit:
                            return path
                        if 0 < size <= source_limit:
                            return compress_to_budget(path, limit)
                    finally:
                        if os.path.exists(path) and not 0 < size <= limit:
                            try:
                                os.remove(path)
                            except OSError:
                                pass
            finally:
                if previous is None:
                    ydl.params.pop("max_filesize", None)
                else:
                    ydl.params["max_filesize"] = previous
    return None


def compress_to_budget(path, limit):
    """Encode one oversized Reel to a bounded H.264/AAC upload, off the event loop.

    Two-pass ABR targets the exact Discord budget. If the first attempt still
    lands above it, lower resolutions are retried before giving up, which keeps
    a single high-bitrate Instagram rendition from failing the whole download.
    """
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return None
    try:
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", path,
        ], capture_output=True, check=True, timeout=15)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("[InstaDL] could not read media duration for compression")
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None

    total_bitrate = int(limit * 0.94 * 8 / duration)
    if total_bitrate < 100_000:
        # Even a 360p/32k encode cannot fit this budget (very long video).
        return None

    audio_bitrate = min(96_000, max(32_000, int(total_bitrate * 0.20)))
    audio_bitrate = min(audio_bitrate, total_bitrate - 8_000 - 64_000)
    video_bitrate = total_bitrate - audio_bitrate - 8_000
    if audio_bitrate < 24_000 or video_bitrate < 64_000:
        return None

    for height in (720, 540, 480, 360):
        output = None
        try:
            handle, output = tempfile.mkstemp(prefix="instadl_discord_", suffix=".mp4")
            os.close(handle)
            with tempfile.TemporaryDirectory(prefix="instadl_encode_") as work:
                common = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", path, "-map", "0:v:0",
                    "-vf", f"scale=trunc(min({height}\\,iw)/2)*2:-2,fps=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate),
                    "-bufsize", str(video_bitrate * 2), "-threads", "2",
                    "-passlogfile", os.path.join(work, "pass"),
                ]
                subprocess.run(
                    common + ["-pass", "1", "-an", "-f", "null", os.devnull],
                    capture_output=True, check=True, timeout=180,
                )
                subprocess.run(
                    common + [
                        "-pass", "2", "-map", "0:a:0?", "-c:a", "aac",
                        "-b:a", str(audio_bitrate), "-movflags", "+faststart", output,
                    ],
                    capture_output=True, check=True, timeout=180,
                )
            if 0 < os.path.getsize(output) <= limit:
                result, output = output, None
                return result
            print(f"[InstaDL] {height}p encode still exceeded the upload budget")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"[InstaDL] {height}p compression attempt failed: {type(error).__name__}")
        finally:
            if output and os.path.exists(output):
                try:
                    os.remove(output)
                except OSError:
                    pass
    print("[InstaDL] compression could not produce a file within the upload budget")
    return None

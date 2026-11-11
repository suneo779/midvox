"""Preserve actionable extraction failures instead of suggesting blind retries."""


class CobaltDownloadError(Exception):
    def __init__(self, code, user_message):
        super().__init__(code)
        self.code = code
        self.user_message = user_message


def extraction_error(codes, source="youtube"):
    if source == "instagram":
        if codes and all(code == "error.api.fetch.fail" for code in codes):
            return CobaltDownloadError(
                "error.api.fetch.fail",
                "Instagram blocked Cobalt's server connection for this Reel. "
                "Add an Instagram session with INSTAGRAM_COOKIES or try again "
                "later; the bot will also retry through yt-dlp and Instagram's "
                "public embed page.",
            )
        if "error.api.fetch.rate" in codes:
            return CobaltDownloadError(
                "error.api.fetch.rate",
                "Instagram is rate-limiting the bot's connection right now. "
                "The download will be retried on the next repost.",
            )
        if codes and all(code == "error.api.content.video.unavailable" for code in codes):
            return CobaltDownloadError(
                "error.api.content.video.unavailable",
                "Instagram reports this Reel unavailable. It may be private, "
                "removed, or restricted for the bot's connection.",
            )
        return None
    if codes and all(code == "error.api.fetch.fail" for code in codes):
        return CobaltDownloadError(
            "error.api.fetch.fail",
            "YouTube blocked Cobalt's Railway/WARP connection for this Short. "
            "Cookie-free downloads cannot complete from the current server "
            "network; connect a YouTube session at /dashboard/youtube-login "
            "or enable COBALT_WARP=1.",
        )
    if "error.api.youtube.login" in codes:
        return CobaltDownloadError(
            "error.api.youtube.login",
            "YouTube is asking the bot to sign in. The owner can connect its "
            "YouTube session at /dashboard/youtube-login. "
            "If you repost this link after a failed download, it will stay visible.",
        )
    if codes and all(code == "error.api.content.video.unavailable" for code in codes):
        return CobaltDownloadError(
            "error.api.content.video.unavailable",
            "YouTube reports this video unavailable. It may be private, removed, "
            "or restricted for the bot's connection.",
        )
    if codes and all(code in ("error.api.youtube.no_matching_format",
                              "error.api.fetch.empty") for code in codes):
        return CobaltDownloadError(
            "error.api.youtube.no_matching_format",
            "YouTube returned no playable H.264/MP4 stream for this Short. "
            "The bot will retry through yt-dlp.",
        )
    if "error.api.fetch.rate" in codes:
        return CobaltDownloadError(
            "error.api.fetch.rate",
            "YouTube is rate-limiting the bot's connection right now. "
            "The download will be retried on the next repost.",
        )
    return None

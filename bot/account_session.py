"""Explicit browser login selection for the optional account worker."""
import json
import os
from pathlib import Path

SESSION_PATH = Path("/tmp/midvox-discord-login.json")


def saved_browser_account():
    try:
        account = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        if (account.get("login_method") == "browser-v2"
                and isinstance(account.get("token"), str) and account["token"].strip()
                and str(account.get("id", "")).isdigit()):
            return account
    except (OSError, ValueError, AttributeError):
        pass
    return None


def configured_account_token():
    account = saved_browser_account()
    return account["token"].strip() if account else os.environ.get("USER_TOKEN", "").strip()

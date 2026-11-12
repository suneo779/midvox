"""Encrypted backup of the owner's YouTube, Instagram and Discord logins."""
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("youtube", "instagram", "discord")


def session_ids(kind):
    legacy = kind + ":" + os.getenv("DISCORD_CLIENT_ID", "midvox")
    owners = re.findall(r"\d{17,20}", os.getenv("OWNER_IDS", ""))
    if owners:
        return [kind + ":owner:" + owners[0], legacy]
    return [legacy]


def _path(kind):
    if kind == "discord":
        return Path("/tmp/midvox-discord-login.json")
    # YouTube and Instagram share Cobalt's cookie store.
    return Path(os.getenv("COOKIE_PATH", "/tmp/midvox-youtube-cookies.json"))


def _key(kind):
    return "token" if kind == "discord" else kind


def transfer(restore=False, kind="youtube"):
    if kind not in KINDS:
        raise ValueError("Unsupported login kind")
    from cryptography.fernet import Fernet
    from pymongo import MongoClient
    uri, secret = os.getenv("MONGO_URI"), os.getenv("NEXTAUTH_SECRET")
    if not uri or not secret:
        return False
    cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
    path = _path(kind)
    with MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
                     socketTimeoutMS=5000) as client:
        collection = client.midvox.owner_login_sessions
        identities = session_ids(kind)
        identity = identities[0]
        if restore:
            record = None
            for key in identities:
                record = collection.find_one({"_id": key})
                if record:
                    break
            if not record:
                return False
            saved = json.loads(cipher.decrypt(record["encrypted"].encode()).decode())
            if not isinstance(saved, dict) or not saved.get(_key(kind)):
                return False
            # Restore only this service. YouTube and Instagram share one file,
            # so replacing it wholesale would erase the other session.
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    current = {}
            except (OSError, ValueError):
                current = {}
            if kind == "discord":
                contents = json.dumps(saved)
            else:
                current[kind] = saved[_key(kind)]
                contents = json.dumps(current)
            # Atomic replacement prevents Cobalt reading a half-written session.
            temporary = path.with_suffix(".pending")
            temporary.write_text(contents, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
            if record["_id"] != identity:
                collection.replace_one({"_id": identity}, {
                    "_id": identity, "encrypted": record["encrypted"],
                    "saved_at": record.get("saved_at", datetime.now(timezone.utc)),
                }, upsert=True)
        else:
            contents = path.read_text(encoding="utf-8")
            store = json.loads(contents)
            if not isinstance(store, dict) or not store.get(_key(kind)):
                return False
            if kind != "discord":
                # Keep each encrypted record limited to one service so a stale
                # YouTube backup can never restore over a newer Instagram login.
                contents = json.dumps({kind: store[_key(kind)]})
            collection.replace_one({"_id": identity}, {
                "_id": identity, "encrypted": cipher.encrypt(contents.encode()).decode(),
                "saved_at": datetime.now(timezone.utc),
            }, upsert=True)
    return True


if __name__ == "__main__":
    try:
        for kind in KINDS:
            print(f"{kind} login restored from encrypted backup." if transfer(restore=True, kind=kind)
                  else f"No saved {kind} login backup.")
    except Exception:
        print("WARNING: Owner login backup unavailable; continuing startup.")

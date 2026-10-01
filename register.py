#!/usr/bin/env python3
"""
Telegram-driven account registration.

Lets an account owner self-register by sending ONE message to the bot in
this exact format (any order of lines doesn't matter, but all four fields
must be present):

    username @theirhandle
    auth_token their_auth_token_value
    ct0 their_ct0_value
    code THE_SHARED_PASSCODE

This script polls Telegram's getUpdates for new messages since the last
one it processed (tracked in registration_state.json), and for every
message that matches the format AND has the correct passcode:
  - adds/updates that account in the encrypted account store (accounts.enc)
    — the account's telegram_chat_id is automatically set to whichever
    chat they messaged the bot from, so DMs route back to them with zero
    manual matching.
  - replies to them in Telegram confirming it worked (or saying what went
    wrong, without ever revealing the correct passcode).

Why an encrypted file instead of the TWITTER_ACCOUNTS secret: GitHub
Actions secrets are write-only (no API to read the current value back),
so there's no way to *merge* a new registration into the existing secret
automatically. accounts.enc is safe to commit even though this repo is
public — it's unreadable without ACCOUNTS_ENCRYPTION_KEY, which only
exists as a GitHub secret, never in the repo.

Expects environment variables:
  TELEGRAM_BOT_TOKEN        - from @BotFather
  ACCOUNTS_ENCRYPTION_KEY   - Fernet key used to encrypt/decrypt accounts.enc
  REGISTRATION_CODE         - the shared passcode account owners must include
  ACCOUNTS_FILE (optional)  - path to the encrypted account store (default: accounts.enc)
  REGISTRATION_STATE_FILE (optional) - path to the offset tracker
                                        (default: registration_state.json)
"""

import json
import os
import re
import sys
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ACCOUNTS_ENCRYPTION_KEY = os.environ.get("ACCOUNTS_ENCRYPTION_KEY")
REGISTRATION_CODE = os.environ.get("REGISTRATION_CODE")
ACCOUNTS_FILE = Path(os.environ.get("ACCOUNTS_FILE", "accounts.enc"))
REGISTRATION_STATE_FILE = Path(os.environ.get("REGISTRATION_STATE_FILE", "registration_state.json"))

# Each field pulled out independently (not one big sequential regex) so the
# message's line order/spacing doesn't matter.
# Accepts "key value", "key: value", or "key:value" -- a missing or extra
# space/colon shouldn't break registration.
FIELD_PATTERNS = {
    "handle": re.compile(r"\busername\s*:?\s*@?(\S+)", re.IGNORECASE),
    "auth_token": re.compile(r"\bauth_token\s*:?\s*(\S+)", re.IGNORECASE),
    "ct0": re.compile(r"\bct0\s*:?\s*(\S+)", re.IGNORECASE),
    "code": re.compile(r"\bcode\s*:?\s*(\S+)", re.IGNORECASE),
}


def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        return []
    if not ACCOUNTS_ENCRYPTION_KEY:
        print("ACCOUNTS_ENCRYPTION_KEY not set, cannot decrypt accounts.enc", file=sys.stderr)
        return []
    f = Fernet(ACCOUNTS_ENCRYPTION_KEY.encode())
    try:
        decrypted = f.decrypt(ACCOUNTS_FILE.read_bytes())
    except InvalidToken:
        print("accounts.enc could not be decrypted with ACCOUNTS_ENCRYPTION_KEY — wrong key, or file corrupted.", file=sys.stderr)
        return []
    return json.loads(decrypted)


def save_accounts(accounts: list[dict]) -> None:
    f = Fernet(ACCOUNTS_ENCRYPTION_KEY.encode())
    encrypted = f.encrypt(json.dumps(accounts).encode())
    ACCOUNTS_FILE.write_bytes(encrypted)


def load_registration_state() -> dict:
    if REGISTRATION_STATE_FILE.exists():
        return json.loads(REGISTRATION_STATE_FILE.read_text())
    return {"last_update_id": 0}


def save_registration_state(state: dict) -> None:
    REGISTRATION_STATE_FILE.write_text(json.dumps(state, indent=2))


def reply(chat_id: int, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    if resp.status_code != 200:
        print(f"Telegram reply failed ({resp.status_code}): {resp.text}", file=sys.stderr)


def parse_registration(text: str) -> dict | None:
    """Returns {handle, auth_token, ct0, code} if ALL four fields are found, else None."""
    result = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            return None
        result[field] = match.group(1).lstrip("@")
    return result


def get_updates(offset: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = httpx.get(url, params={"offset": offset, "timeout": 0}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print(f"getUpdates failed: {data}", file=sys.stderr)
        return []
    return data["result"]


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set, skipping registration check.", file=sys.stderr)
        return
    if not ACCOUNTS_ENCRYPTION_KEY or not REGISTRATION_CODE:
        print("ACCOUNTS_ENCRYPTION_KEY/REGISTRATION_CODE not set, skipping registration check.", file=sys.stderr)
        return

    reg_state = load_registration_state()
    updates = get_updates(reg_state["last_update_id"] + 1)
    if not updates:
        return

    accounts = load_accounts()
    accounts_by_handle = {a["handle"].lower(): a for a in accounts}
    changed = False

    for update in updates:
        reg_state["last_update_id"] = max(reg_state["last_update_id"], update["update_id"])

        message = update.get("message")
        if not message or "text" not in message:
            continue
        chat_id = message["chat"]["id"]
        text = message["text"]

        parsed = parse_registration(text)
        if not parsed:
            # Not a registration attempt (could be the old-style "just send
            # your handle" message, chit-chat, etc) — ignore silently.
            continue

        if parsed["code"] != REGISTRATION_CODE:
            reply(chat_id, "That passcode doesn't match. Please double-check it and resend your registration message.")
            continue

        handle = parsed["handle"]
        entry = {
            "label": handle,
            "handle": handle,
            "auth_token": parsed["auth_token"],
            "ct0": parsed["ct0"],
            "known_contacts": accounts_by_handle.get(handle.lower(), {}).get("known_contacts", []),
            "telegram_chat_id": str(chat_id),
        }
        is_update = handle.lower() in accounts_by_handle
        accounts_by_handle[handle.lower()] = entry
        changed = True

        verb = "updated" if is_update else "registered"
        reply(
            chat_id,
            f"✅ @{handle} {verb}. Your DMs will start relaying here within about 5 minutes.",
        )

    if changed:
        save_accounts(list(accounts_by_handle.values()))
        print(f"Accounts store updated — {len(accounts_by_handle)} account(s) total.")

    save_registration_state(reg_state)


if __name__ == "__main__":
    main()

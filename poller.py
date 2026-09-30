#!/usr/bin/env python3
"""
Telegram DM Relay — polls one or more X (Twitter) accounts for new direct
messages using twikit (unofficial/scraper-based, no API key) and forwards
new messages to a Telegram chat via the Bot API.

KNOWN LIMITATION (as of Sept 2026): twikit has no "list my DM inbox" call.
get_dm_history(user_id) only works for a conversation you already know the
other party's user_id for. A first-time DM from someone you've never
messaged/been messaged by before will NOT be detected by this script as
currently written. See README.md for details and options.

Run: python poller.py
Expects environment variables (see README.md):
  TELEGRAM_BOT_TOKEN       - token from @BotFather
  TELEGRAM_CHAT_ID         - default chat id to send alerts to
  TWITTER_ACCOUNTS         - JSON array, see accounts.example.json
  STATE_FILE (optional)    - path to the last-seen-message state file
                              (default: state.json in the repo root)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from twikit import Client

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_accounts() -> list[dict]:
    """
    TWITTER_ACCOUNTS is a JSON array of objects, one per monitored account:

    [
      {
        "label": "brandaccount",
        "auth_token": "....",
        "ct0": "....",
        "known_contacts": ["123456789", "987654321"],
        "telegram_chat_id": "optional override, else TELEGRAM_CHAT_ID is used"
      }
    ]

    known_contacts are X user IDs (not usernames) this account has an
    existing DM thread with — required because of the inbox-listing gap
    noted at the top of this file.
    """
    raw = os.environ.get("TWITTER_ACCOUNTS")
    if not raw:
        print("TWITTER_ACCOUNTS env var is empty or unset.", file=sys.stderr)
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"TWITTER_ACCOUNTS is not valid JSON: {e}", file=sys.stderr)
        return []


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def send_telegram_message(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set; cannot send message.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    if resp.status_code != 200:
        print(f"Telegram send failed ({resp.status_code}): {resp.text}", file=sys.stderr)


async def check_account(account: dict, state: dict) -> None:
    label = account.get("label", "unknown")
    chat_id = account.get("telegram_chat_id") or TELEGRAM_DEFAULT_CHAT_ID
    if not chat_id:
        print(f"[{label}] no Telegram chat id configured, skipping.", file=sys.stderr)
        return

    client = Client("en-US")

    try:
        # Cookie-based auth (preferred): no password stored, and lets the
        # account owner revoke access by logging out elsewhere.
        client.set_cookies({
            "auth_token": account["auth_token"],
            "ct0": account["ct0"],
        })
    except KeyError:
        print(f"[{label}] missing auth_token/ct0 in account config, skipping.", file=sys.stderr)
        return

    account_state = state.setdefault(label, {})

    for contact_id in account.get("known_contacts", []):
        try:
            messages = await client.get_dm_history(contact_id)
        except Exception as e:  # noqa: BLE001 - surface auth/session failures distinctly
            err_text = str(e).lower()
            if "login" in err_text or "auth" in err_text or "401" in err_text or "403" in err_text:
                send_telegram_message(
                    TELEGRAM_DEFAULT_CHAT_ID,
                    f"[{label}] session looks expired — needs a fresh auth_token/ct0 re-export.",
                )
            print(f"[{label}] error fetching DM history for {contact_id}: {e}", file=sys.stderr)
            continue

        last_seen_id = account_state.get(contact_id)
        new_messages = []
        for msg in messages:
            if last_seen_id is not None and str(msg.id) == str(last_seen_id):
                break
            new_messages.append(msg)

        if not new_messages:
            continue

        # messages come back newest-first; send oldest-first so the chat reads naturally
        for msg in reversed(new_messages):
            send_telegram_message(chat_id, f"[{label}] New DM:\n{msg.text}")

        account_state[contact_id] = str(new_messages[0].id)


async def main() -> None:
    accounts = load_accounts()
    if not accounts:
        print("No accounts configured. Nothing to do.", file=sys.stderr)
        return

    state = load_state()

    for account in accounts:
        await check_account(account, state)

    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())

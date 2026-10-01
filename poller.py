#!/usr/bin/env python3
"""
Telegram DM Relay — polls one or more X (Twitter) accounts for new direct
messages using twikit (unofficial/scraper-based, no API key) and forwards
new messages to a Telegram chat via the Bot API.

INBOX DISCOVERY: twikit's public API has no "list my inbox" method, but its
own source (twikit/client/v11.py) defines the real X inbox endpoint
(DM_INBOX = .../dm/inbox_initial_state.json) without ever wrapping it in a
method. This script calls that endpoint directly, unofficially, through
twikit's authenticated HTTP client — confirmed working against a live
account as of 2026-10. If it ever fails (X changes the response shape,
etc.), each account falls back automatically to the known-contacts method
(get_dm_history per known_contacts entry), which only catches new messages
in conversations you already have, not first-time senders.

MESSAGE LINKS: each forwarded message links the monitored account's own
@handle and the sender's @handle to their X profiles, and links to the DM
conversation itself (https://x.com/messages/<conversation_id>) so you can
jump straight into X. Sender handles are resolved from X's numeric user ID
via one extra API call per *new* sender, then cached in state.json so a
given sender is only looked up once, not on every run.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet, InvalidToken
from twikit import Client

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
ACCOUNTS_FILE = Path(os.environ.get("ACCOUNTS_FILE", "accounts.enc"))
ACCOUNTS_ENCRYPTION_KEY = os.environ.get("ACCOUNTS_ENCRYPTION_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# X's real inbox endpoint. Defined in twikit's own source (v11.py) as
# Endpoint.DM_INBOX but never wired up to a public method — called here
# directly, the same way twikit's own dm_conversation() calls DM_CONVERSATION.
DM_INBOX_URL = "https://x.com/i/api/1.1/dm/inbox_initial_state.json"


def load_accounts() -> list[dict]:
    """
    Loads the monitored-account list from accounts.enc, an encrypted file
    committed in this repo (safe even though the repo is public — it's
    unreadable without ACCOUNTS_ENCRYPTION_KEY, which only exists as a
    GitHub secret). The file holds a JSON array of objects, one per account:

    [
      {
        "label": "brandaccount",
        "handle": "brandaccount",
        "auth_token": "....",
        "ct0": "....",
        "known_contacts": ["123456789", "987654321"],
        "telegram_chat_id": "set automatically by register.py, or manually"
      }
    ]

    "handle" is this account's own X @handle (no @), used to link its name
    in Telegram messages — falls back to "label" if not set.

    known_contacts are X user IDs (not usernames) — used only as a fallback
    if inbox discovery fails for this account.

    register.py is what normally writes this file (via Telegram
    self-registration) — see its docstring. You can also create/edit it
    directly with a short script using cryptography.fernet.Fernet and
    ACCOUNTS_ENCRYPTION_KEY if you need to add an account by hand.
    """
    if not ACCOUNTS_FILE.exists():
        print(f"{ACCOUNTS_FILE} not found.", file=sys.stderr)
        return []
    if not ACCOUNTS_ENCRYPTION_KEY:
        print("ACCOUNTS_ENCRYPTION_KEY env var is empty or unset.", file=sys.stderr)
        return []
    f = Fernet(ACCOUNTS_ENCRYPTION_KEY.encode())
    try:
        decrypted = f.decrypt(ACCOUNTS_FILE.read_bytes())
    except InvalidToken:
        print(f"{ACCOUNTS_FILE} could not be decrypted — wrong key, or file corrupted.", file=sys.stderr)
        return []
    try:
        return json.loads(decrypted)
    except json.JSONDecodeError as e:
        print(f"Decrypted accounts data is not valid JSON: {e}", file=sys.stderr)
        return []


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def escape_html(text: str) -> str:
    """Escapes DM content for Telegram's HTML parse mode. Only for raw text
    we didn't construct ourselves — never escape the <a>/<b> tags we build."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def profile_link(handle: str | None, fallback_id: str | None) -> str:
    """HTML link to an X profile, or a plain non-linked id if we have no handle."""
    if handle:
        return f'<a href="https://x.com/{handle}">@{escape_html(handle)}</a>'
    return f"user {fallback_id}" if fallback_id else "unknown"


def conversation_link(conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    return f'<a href="https://x.com/messages/{conversation_id}">Open conversation →</a>'


# Fixed UTC+1 offset for West Africa Time (Lagos doesn't observe DST, so no
# need for a full tz database lookup on the CI runner).
_WAT = timezone(timedelta(hours=1))


def format_timestamp(time_ms: str | int | None) -> str | None:
    """Formats X's epoch-milliseconds message time as e.g. 'Oct 1, 2026, 7:23 PM WAT'."""
    if not time_ms:
        return None
    try:
        dt = datetime.fromtimestamp(int(time_ms) / 1000, tz=_WAT)
    except (ValueError, TypeError, OSError):
        return None
    return dt.strftime("%b %-d, %Y, %-I:%M %p WAT")


def send_telegram_message(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set; cannot send message.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = httpx.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"Telegram send failed ({resp.status_code}): {resp.text}", file=sys.stderr)


async def resolve_handle(client: Client, user_id: str | None, handle_cache: dict) -> str | None:
    """Resolves a numeric X user ID to an @handle, using/populating a cache
    (persisted in state.json) so each sender is only looked up once ever."""
    if not user_id:
        return None
    if user_id in handle_cache:
        return handle_cache[user_id] or None  # cached "" means "lookup failed before"
    try:
        user = await client.get_user_by_id(user_id)
        handle = user.screen_name
        handle_cache[user_id] = handle or ""
        return handle
    except Exception as e:  # noqa: BLE001 - don't let a lookup failure break the relay
        print(f"[handle lookup] failed for {user_id}: {e}", file=sys.stderr)
        handle_cache[user_id] = ""  # avoid retrying every run
        return None


def _extract_messages_from_entries(entries: list) -> list[dict]:
    """
    Normalizes the 'entries' list found in both inbox_initial_state.json and
    conversation_timeline responses into a flat list of
    {id, conversation_id, sender_id, recipient_id, text} dicts.
    Skips non-message entries (join/leave/reaction events etc).
    """
    out = []
    for item in entries:
        msg = item.get("message")
        if not msg:
            continue
        data = msg.get("message_data")
        if not data:
            continue
        out.append({
            "id": data.get("id") or msg.get("id"),
            "conversation_id": data.get("conversation_id"),
            "sender_id": data.get("sender_id"),
            "recipient_id": data.get("recipient_id"),
            "text": (data.get("text") or "").strip(),
            "time": data.get("time") or msg.get("time"),
        })
    return out


async def fetch_inbox(client: Client) -> list[dict] | None:
    """
    Calls X's inbox_initial_state endpoint directly (see module docstring).
    Returns a flat list of the most recent message per conversation across
    the WHOLE inbox — including conversations with people this account has
    never messaged before — or None if the call fails or the response
    doesn't look like what we expect, so the caller can fall back to the
    known-contacts method.
    """
    try:
        response, _ = await client.get(
            DM_INBOX_URL,
            headers=client._base_headers,
        )
    except Exception as e:  # noqa: BLE001 - any failure here just triggers fallback
        print(f"[inbox] request failed: {e}", file=sys.stderr)
        return None

    try:
        inbox = response.get("inbox_initial_state") or response.get("conversation_timeline")
        if not inbox or "entries" not in inbox:
            print(f"[inbox] unexpected response shape, keys: {list(response.keys())}", file=sys.stderr)
            return None
        return _extract_messages_from_entries(inbox["entries"])
    except (AttributeError, TypeError) as e:
        print(f"[inbox] failed to parse response: {e}", file=sys.stderr)
        return None


async def check_account_via_inbox(
    client: Client, label: str, account_handle: str, chat_id: str,
    account_state: dict, handle_cache: dict,
) -> bool:
    """Returns True if inbox discovery worked (even with zero new messages), False to signal fallback."""
    messages = await fetch_inbox(client)
    if messages is None:
        return False

    seen = account_state.setdefault("_inbox_seen_ids", [])
    seen_set = set(seen)

    new_messages = [m for m in messages if m["id"] and m["id"] not in seen_set]
    if new_messages:
        # entries are typically oldest-first per X's own timeline convention;
        # send in that order so the chat reads naturally.
        for m in new_messages:
            sender_handle = await resolve_handle(client, m["sender_id"], handle_cache)
            header = f"{profile_link(account_handle, label)} — New DM from {profile_link(sender_handle, m['sender_id'])}"
            timestamp = format_timestamp(m.get("time"))
            conv_link = conversation_link(m["conversation_id"])
            lines = [header]
            if timestamp:
                lines.append(f"📅 {timestamp}")
            lines += ["", escape_html(m["text"])]
            if conv_link:
                lines += ["", conv_link]
            send_telegram_message(chat_id, "\n".join(lines))

        seen.extend(m["id"] for m in new_messages)
        # keep this list from growing forever
        account_state["_inbox_seen_ids"] = seen[-500:]

    return True


async def check_account_via_known_contacts(
    client: Client, label: str, account_handle: str, chat_id: str,
    account: dict, account_state: dict, handle_cache: dict,
) -> None:
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
            sender_id = getattr(msg, "sender_id", None)
            sender_handle = await resolve_handle(client, sender_id, handle_cache)
            header = f"{profile_link(account_handle, label)} — New DM from {profile_link(sender_handle, sender_id)}"
            timestamp = format_timestamp(getattr(msg, "time", None))
            conv_link = conversation_link(getattr(msg, "conversation_id", None) or contact_id)
            lines = [header]
            if timestamp:
                lines.append(f"📅 {timestamp}")
            lines += ["", escape_html(msg.text)]
            if conv_link:
                lines += ["", conv_link]
            send_telegram_message(chat_id, "\n".join(lines))

        account_state[contact_id] = str(new_messages[0].id)


async def check_account(account: dict, state: dict) -> None:
    label = account.get("label", "unknown")
    account_handle = account.get("handle") or label
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
    handle_cache = state.setdefault("_user_handle_cache", {})

    inbox_worked = await check_account_via_inbox(
        client, label, account_handle, chat_id, account_state, handle_cache
    )
    if not inbox_worked:
        print(f"[{label}] inbox discovery unavailable, falling back to known_contacts.", file=sys.stderr)
        await check_account_via_known_contacts(
            client, label, account_handle, chat_id, account, account_state, handle_cache
        )


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

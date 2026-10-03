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
from twikit.errors import (
    AccountLocked,
    AccountSuspended,
    Forbidden,
    TooManyRequests,
    TwitterException,
    Unauthorized,
)

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
ACCOUNTS_FILE = Path(os.environ.get("ACCOUNTS_FILE", "accounts.enc"))
ACCOUNTS_ENCRYPTION_KEY = os.environ.get("ACCOUNTS_ENCRYPTION_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Residential/mobile proxy to route all X traffic through, instead of the
# GitHub Actions runner's own (datacenter, widely-flagged) IP. twikit's
# Client accepts this natively (see twikit/client/client.py). Format:
# "http://user:pass@host:port" -- works for rotating-endpoint providers
# like Webshare/IPRoyal/Bright Data. A per-account "proxy" field in
# accounts.enc overrides this default, in case accounts ever need
# different exit IPs.
PROXY_URL = os.environ.get("PROXY_URL") or None

# Optional pool of several proxy URLs (comma-separated), to spread accounts
# across more than one exit IP instead of hammering a single one. Each
# account is deterministically (not randomly) assigned one proxy from the
# pool by hashing its label -- same account always gets the same IP on
# every run. That matters: an X session that suddenly starts connecting
# from a different IP/country every few minutes looks like account
# takeover to X's own fraud detection, which is a bigger risk than one
# shared exit IP seeing a bit more traffic. PROXY_URLS takes priority over
# the single PROXY_URL above when both are set; a per-account "proxy"
# field in accounts.enc overrides both.
PROXY_URLS = [u.strip() for u in os.environ.get("PROXY_URLS", "").split(",") if u.strip()]


def pick_proxy(label: str) -> str | None:
    """Deterministically assign one proxy from PROXY_URLS to this account
    label (sticky across runs), falling back to PROXY_URL, then None."""
    if PROXY_URLS:
        # crc32 instead of hash(): hash() is salted per-process in Python
        # (PYTHONHASHSEED), so it would NOT be stable across runs/workers.
        import zlib
        idx = zlib.crc32(label.encode("utf-8")) % len(PROXY_URLS)
        return PROXY_URLS[idx]
    return PROXY_URL

# X's real inbox endpoint. Defined in twikit's own source (v11.py) as
# Endpoint.DM_INBOX but never wired up to a public method — called here
# directly, the same way twikit's own dm_conversation() calls DM_CONVERSATION.
DM_INBOX_URL = "https://x.com/i/api/1.1/dm/inbox_initial_state.json"

# Where to send operational alerts (token expiry, rate limiting, Cloudflare
# blocks, unexpected crashes) -- separate from the per-account relay chat,
# since these are "something needs your attention" messages for whoever
# runs this, not DMs. Hardcoded rather than a secret/env var because it's
# not sensitive (just a chat id) and doesn't vary per account.
OWNER_ALERT_CHAT_ID = "6056524121"

# Minimum time between two alerts for the *same* underlying problem, so a
# persistent block/rate-limit doesn't spam one message per run (every run
# interval) forever -- you'll still get the first one immediately.
ALERT_COOLDOWN = timedelta(minutes=30)

# How many times the SAME problem has to happen in a row on the SAME
# account before notify_owner actually sends anything (see its docstring).
MIN_CONSECUTIVE_FAILURES = 2


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


def save_accounts(accounts: list[dict]) -> None:
    """Writes accounts.enc back out (re-encrypted). Used to persist
    auth_token/ct0 if X rotates them mid-session -- see check_account's
    cookie-refresh step, which is what actually calls this."""
    if not ACCOUNTS_ENCRYPTION_KEY:
        print("ACCOUNTS_ENCRYPTION_KEY env var is empty or unset, cannot save accounts.enc", file=sys.stderr)
        return
    f = Fernet(ACCOUNTS_ENCRYPTION_KEY.encode())
    encrypted = f.encrypt(json.dumps(accounts).encode())
    ACCOUNTS_FILE.write_bytes(encrypted)


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


def classify_error(e: Exception) -> tuple[str, str]:
    """Maps an exception to (short_key, human-readable label) so callers can
    both de-dupe alerts (by short_key) and explain what's actually wrong."""
    if isinstance(e, AccountSuspended):
        return "suspended", "X reports this account as SUSPENDED"
    if isinstance(e, AccountLocked):
        return "locked", "X has LOCKED this account pending verification (captcha/phone)"
    if isinstance(e, Unauthorized):
        return "auth", "AUTH FAILED (401) -- the auth_token/ct0 cookie is likely expired or revoked and needs replacing"
    if isinstance(e, TooManyRequests):
        return "ratelimit", "RATE LIMITED (429) by X -- polling is happening too often for this account"
    if isinstance(e, Forbidden):
        return "blocked", "BLOCKED (403) -- almost certainly Cloudflare/anti-bot, not a real auth problem"
    if isinstance(e, TwitterException):
        return "twitter_error", f"X API error: {type(e).__name__}"
    return "error", f"Unexpected error: {type(e).__name__}"


def _current_run_url() -> str | None:
    """Link to this specific GitHub Actions run, if we're running in one
    (these env vars are set automatically by Actions on every job -- no
    workflow changes needed to get them)."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def record_account_success(account_state: dict) -> None:
    """Call this after any check against an account completes without
    error. Clears its consecutive-failure counters, so one bad run
    followed by a good one doesn't escalate into an alert -- see
    notify_owner's MIN_CONSECUTIVE_FAILURES."""
    if account_state.get("_consecutive_fails"):
        account_state["_consecutive_fails"] = {}
    account_state.pop("_owner_notified_cookie_expired", None)


# How many times (in a row) an "auth" (expired/revoked cookie) failure has
# to happen before we message the ACCOUNT OWNER directly (as opposed to
# MIN_CONSECUTIVE_FAILURES, which is when the admin/owner-alert chat gets
# pinged). Higher than MIN_CONSECUTIVE_FAILURES on purpose: a transient
# Cloudflare-flavored 401 can look identical to a genuinely dead cookie
# for a run or two, and account owners are non-technical end users, so
# this should only fire once it's clearly not a blip.
ACCOUNT_OWNER_AUTH_ALERT_THRESHOLD = 3

# Plain-language instructions sent straight to the account owner's own
# Telegram chat (not the admin alert chat) when their session cookie has
# genuinely expired. Point this at the real re-export instructions.
COOKIE_REUPLOAD_URL = "https://github.com/HaywhyD/telegram-dm-relay#2-get-session-cookies-for-each-x-account"


def notify_account_owner_cookie_expired(chat_id: str, label: str) -> None:
    """Messages the account owner's OWN relay chat (not the admin alert
    chat) once their session has failed auth ACCOUNT_OWNER_AUTH_ALERT_
    THRESHOLD times in a row, asking them to redo the cookie export.
    Separate from notify_owner's admin alert, which already fired earlier
    (lower threshold) -- this one is written for a non-technical end user,
    not a debugging note."""
    lines = [
        "⚠️ Your DM relay session has expired and needs to be refreshed.",
        "",
        f"Please redo the steps here to get a new session, then send the new values: {COOKIE_REUPLOAD_URL}",
        "",
        "Important: please don't log out of X, or log into X from another "
        "device/browser, until after you've redone this -- doing so "
        "immediately invalidates the session this is trying to reuse, "
        "which is exactly what causes this.",
    ]
    send_telegram_message(chat_id, "\n".join(lines))


def notify_owner(
    state: dict, account_state: dict, label: str, error: Exception,
    chat_id: str | None = None,
) -> None:
    """Sends a one-time (per problem, per 30 min) alert to OWNER_ALERT_CHAT_ID
    so token expiry / rate limiting / Cloudflare blocks / crashes don't go
    unnoticed between the times you happen to check the logs yourself.

    Requires MIN_CONSECUTIVE_FAILURES in a row for the *same* problem on
    the *same* account before it actually alerts -- a single 401/403 is
    often a transient hiccup (Cloudflare, a momentary X-side blip) that's
    gone by the next run a minute later, not a genuinely dead cookie, and
    alerting on every single one of those was crying wolf. record_account_
    success() resets the count, so it really does need to fail several
    times back-to-back, not just several times total."""
    key_suffix, human_label = classify_error(error)

    fails = account_state.setdefault("_consecutive_fails", {})
    fails[key_suffix] = fails.get(key_suffix, 0) + 1
    count = fails[key_suffix]

    if (
        key_suffix == "auth"
        and count == ACCOUNT_OWNER_AUTH_ALERT_THRESHOLD
        and chat_id
        and not account_state.get("_owner_notified_cookie_expired")
    ):
        notify_account_owner_cookie_expired(chat_id, label)
        # Once per onset, not once per run -- record_account_success()
        # clears this flag too, so a future genuine expiry re-alerts.
        account_state["_owner_notified_cookie_expired"] = True

    if count < MIN_CONSECUTIVE_FAILURES:
        print(
            f"[{label}] {key_suffix} failure {count}/{MIN_CONSECUTIVE_FAILURES} "
            f"-- not alerting yet, could be a transient hiccup",
            file=sys.stderr,
        )
        return

    key = f"{label}:{key_suffix}"
    alerts = state.setdefault("_error_alerts", {})
    now = datetime.now(timezone.utc)
    last_raw = alerts.get(key)
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            if now - last < ALERT_COOLDOWN:
                return  # already alerted recently for this exact problem
        except ValueError:
            pass
    alerts[key] = now.isoformat()

    detail = str(error).strip().replace("\n", " ")
    if len(detail) > 300:
        detail = detail[:300] + "…"

    lines = [
        f"\u26a0\ufe0f <b>{escape_html(label)}</b>: {escape_html(human_label)}",
        f"(failed {count}x in a row)",
        "",
        f"<code>{escape_html(detail)}</code>",
    ]
    run_url = _current_run_url()
    if run_url:
        lines += ["", f'<a href="{run_url}">View this workflow run</a>']
    send_telegram_message(OWNER_ALERT_CHAT_ID, "\n".join(lines))


async def resolve_handle(client: Client, user_id: str | None, handle_cache: dict) -> str | None:
    """Resolves a numeric X user ID to an @handle, using/populating a cache
    (persisted in state.json) so each sender is only looked up once ever.

    As of 2026-10, twikit's get_user_by_id() (which calls X's GraphQL
    UserByRestId query) is consistently blocked by Cloudflare in this CI
    environment, even though the plain inbox_initial_state.json fetch
    above is not -- X appears to apply tighter bot-detection to its
    GraphQL endpoints than to its older "legacy" v1.1 REST endpoints. So
    we try the GraphQL call first (it's the documented way and may start
    working again if X's protections change), and fall back to the
    legacy users/show.json endpoint, which uses the same request() path
    (and so the same X-Client-Transaction-Id handling) as the working
    inbox fetch.
    """
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
        print(f"[handle lookup] graphql failed for {user_id}: {e}", file=sys.stderr)

    try:
        response, _ = await client.get(
            f"https://x.com/i/api/1.1/users/show.json?user_id={user_id}",
            headers=client._base_headers,
        )
        handle = response.get("screen_name") if isinstance(response, dict) else None
        handle_cache[user_id] = handle or ""
        return handle
    except Exception as e:  # noqa: BLE001
        print(f"[handle lookup] legacy fallback failed for {user_id}: {e}", file=sys.stderr)
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


async def fetch_inbox(
    client: Client, handle_cache: dict, state: dict, label: str,
    account_handle: str, account_state: dict, chat_id: str | None = None,
) -> list[dict] | None:
    """
    Calls X's inbox_initial_state endpoint directly (see module docstring).
    Returns a flat list of the most recent message per conversation across
    the WHOLE inbox — including conversations with people this account has
    never messaged before — with the monitored account's OWN outgoing
    messages filtered out (see self_id below) — or None if the call fails
    or the response doesn't look like what we expect, so the caller can
    fall back to the known-contacts method.

    As a side effect, also populates handle_cache from this same response's
    "users" dict. X's inbox_initial_state payload already carries a full
    user object (including screen_name) for every participant, right
    alongside the messages -- so we get handles for free from the one
    request that's known to work, instead of making a separate per-sender
    lookup request. That separate lookup (via resolve_handle's own network
    calls) is consistently blocked by Cloudflare in this CI environment
    (both the GraphQL and legacy REST endpoints), seemingly because only
    this specific inbox endpoint is considered "safe" by whatever's
    triggering the block -- any other x.com request in the same run gets a
    403, even with a freshly-computed transaction ID.

    The same "users" dict also lets us find the monitored account's own
    numeric ID (self_id) for free, by matching account_handle against the
    screen names in there -- the viewer is always a participant of their
    own conversations, so their own user object is always present. We
    cache it in account_state once found, so a run where it's somehow
    missing still has last run's value to filter with.
    """
    try:
        response, _ = await client.get(
            DM_INBOX_URL,
            headers=client._base_headers,
        )
    except Exception as e:  # noqa: BLE001 - any failure here just triggers fallback
        print(f"[inbox] request failed: {e}", file=sys.stderr)
        notify_owner(state, account_state, label, e, chat_id=chat_id)
        return None

    # The request above succeeded (no 401/403/etc raised), so whatever the
    # cookies are right now, they're working -- clear any consecutive-
    # failure count from a previous run's hiccup.
    record_account_success(account_state)

    try:
        inbox = response.get("inbox_initial_state") or response.get("conversation_timeline")
        if not inbox or "entries" not in inbox:
            print(f"[inbox] unexpected response shape, keys: {list(response.keys())}", file=sys.stderr)
            return None

        users = inbox.get("users") or {}
        target_handle = (account_handle or "").lstrip("@").strip().lower()
        for uid, user_obj in users.items():
            screen_name = (user_obj or {}).get("screen_name")
            if not screen_name:
                continue
            handle_cache[uid] = screen_name
            if target_handle and screen_name.strip().lower() == target_handle:
                account_state["_self_id"] = uid
        if users:
            print(f"[inbox] got {len(users)} handle(s) for free from the inbox response", file=sys.stderr)

        messages = _extract_messages_from_entries(inbox["entries"])

        self_id = account_state.get("_self_id")
        if self_id:
            before = len(messages)
            messages = [m for m in messages if str(m.get("sender_id")) != str(self_id)]
            skipped = before - len(messages)
            if skipped:
                print(f"[{label}] filtered out {skipped} outgoing message(s) sent by this account itself", file=sys.stderr)
        else:
            print(f"[{label}] couldn't determine this account's own user id (handle '{account_handle}' not "
                  f"found among inbox participants) -- outgoing self-sent messages won't be filtered this run",
                  file=sys.stderr)

        return messages
    except (AttributeError, TypeError) as e:
        print(f"[inbox] failed to parse response: {e}", file=sys.stderr)
        return None


async def check_account_via_inbox(
    client: Client, label: str, account_handle: str, chat_id: str,
    account_state: dict, handle_cache: dict, state: dict,
) -> bool:
    """Returns True if inbox discovery worked (even with zero new messages), False to signal fallback."""
    messages = await fetch_inbox(client, handle_cache, state, label, account_handle, account_state, chat_id=chat_id)
    if messages is None:
        return False

    # The very first time we check a given account (self-registered just
    # now, or added by hand), there's no seen-ids baseline yet, so every
    # message currently sitting in the inbox would otherwise look "new" --
    # that's the flood of old DMs you'd get right after registering. On
    # this first run only, we record everything that's there right now as
    # already-seen WITHOUT forwarding any of it, so the account owner only
    # starts getting pinged for messages that arrive from this point on.
    is_first_run = "_inbox_seen_ids" not in account_state

    seen = account_state.setdefault("_inbox_seen_ids", [])
    seen_set = set(seen)

    new_messages = [m for m in messages if m["id"] and m["id"] not in seen_set]

    if is_first_run:
        seen.extend(m["id"] for m in new_messages if m["id"])
        account_state["_inbox_seen_ids"] = seen[-500:]
        print(
            f"[{label}] first run for this account -- priming with "
            f"{len(new_messages)} existing message(s), nothing forwarded",
            file=sys.stderr,
        )
        return True

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
    account: dict, account_state: dict, handle_cache: dict, state: dict,
) -> None:
    for contact_id in account.get("known_contacts", []):
        try:
            messages = await client.get_dm_history(contact_id)
        except Exception as e:  # noqa: BLE001 - surface auth/session failures distinctly
            print(f"[{label}] error fetching DM history for {contact_id}: {e}", file=sys.stderr)
            notify_owner(state, account_state, label, e, chat_id=chat_id)
            continue
        record_account_success(account_state)

        # Same first-run priming as the inbox-discovery path: the first time
        # we see this contact, record where their history currently stands
        # without forwarding any of it, so we don't flood old DMs.
        is_first_run = contact_id not in account_state
        if is_first_run:
            if messages:
                account_state[contact_id] = str(messages[0].id)
            print(
                f"[{label}] first run for contact {contact_id} -- priming, nothing forwarded",
                file=sys.stderr,
            )
            continue

        last_seen_id = account_state.get(contact_id)
        new_messages = []
        for msg in messages:
            if last_seen_id is not None and str(msg.id) == str(last_seen_id):
                break
            new_messages.append(msg)

        if not new_messages:
            continue

        # last_seen_id still needs to advance past our own outgoing messages
        # (otherwise we'd keep re-scanning them every run), but we only want
        # to *forward* the ones this account received, not sent.
        self_id = account_state.get("_self_id")

        # messages come back newest-first; send oldest-first so the chat reads naturally
        for msg in reversed(new_messages):
            sender_id = getattr(msg, "sender_id", None)
            if self_id and str(sender_id) == str(self_id):
                continue
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


async def check_account(account: dict, state: dict) -> bool:
    """Returns True if this account's auth_token/ct0 were refreshed in
    accounts.enc (so main() knows whether a save is needed) -- see the
    cookie-refresh step near the end of this function."""
    label = account.get("label", "unknown")
    account_handle = account.get("handle") or label
    chat_id = account.get("telegram_chat_id") or TELEGRAM_DEFAULT_CHAT_ID
    if not chat_id:
        print(f"[{label}] no Telegram chat id configured, skipping.", file=sys.stderr)
        return False

    proxy = account.get("proxy") or pick_proxy(label)
    client = Client("en-US", proxy=proxy) if proxy else Client("en-US")
    if proxy:
        print(f"[{label}] using proxy {proxy.rsplit('@', 1)[-1]}", file=sys.stderr)

    try:
        # Cookie-based auth (preferred): no password stored, and lets the
        # account owner revoke access by logging out elsewhere.
        client.set_cookies({
            "auth_token": account["auth_token"],
            "ct0": account["ct0"],
        })
    except KeyError:
        print(f"[{label}] missing auth_token/ct0 in account config, skipping.", file=sys.stderr)
        return False

    account_state = state.setdefault(label, {})
    handle_cache = state.setdefault("_user_handle_cache", {})

    try:
        inbox_worked = await check_account_via_inbox(
            client, label, account_handle, chat_id, account_state, handle_cache, state
        )
        if not inbox_worked:
            print(f"[{label}] inbox discovery unavailable, falling back to known_contacts.", file=sys.stderr)
            await check_account_via_known_contacts(
                client, label, account_handle, chat_id, account, account_state, handle_cache, state
            )
    except Exception as e:  # noqa: BLE001 - never let one account's crash take down the others
        print(f"[{label}] unhandled error: {e}", file=sys.stderr)
        notify_owner(state, account_state, label, e, chat_id=chat_id)
        return False

    # X rotates ct0 periodically. We only ever loaded the value that was
    # true at registration time, and never looked at it again -- if X has
    # since rotated it server-side, the account.get("ct0") we started this
    # run with is already stale, which is a plausible cause of the
    # intermittent 401 "Could not authenticate you" / 403 "matching csrf
    # cookie" hiccups: it can still work sometimes and not others depending
    # on how X currently reconciles it, rather than being a genuinely dead,
    # permanently-revoked cookie. Picking up whatever's in the client's
    # live cookie jar now (after a request that worked) and writing it back
    # to accounts.enc means the NEXT run starts from the freshest known-
    # good value instead of repeating this run's.
    live_cookies = client.get_cookies()
    changed = False
    for cookie_name in ("auth_token", "ct0"):
        live_value = live_cookies.get(cookie_name)
        if live_value and live_value != account.get(cookie_name):
            print(f"[{label}] {cookie_name} changed since last run -- updating accounts.enc", file=sys.stderr)
            account[cookie_name] = live_value
            changed = True

    return changed


async def main() -> None:
    accounts = load_accounts()
    if not accounts:
        print("No accounts configured. Nothing to do.", file=sys.stderr)
        return

    state = load_state()

    any_cookies_changed = False
    for account in accounts:
        changed = await check_account(account, state)
        any_cookies_changed = any_cookies_changed or changed

    save_state(state)
    if any_cookies_changed:
        save_accounts(accounts)


if __name__ == "__main__":
    asyncio.run(main())

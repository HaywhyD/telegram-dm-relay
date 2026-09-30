# Telegram DM Relay

Forwards new X (Twitter) direct messages to a Telegram chat, using
[twikit](https://github.com/d60/twikit) (unofficial, no API key/cost) and a
GitHub Actions cron job instead of X's paid webhook API.

## ⚠️ Known limitation — read this first

twikit has **no "list my inbox" call**. It can only fetch history for a
conversation you already know the other person's numeric user ID for
(`get_dm_history(user_id)`). A brand-new person DMing an account for the
first time will **not** be detected by this script. This is a limitation of
twikit itself (confirmed open issue: d60/twikit#117), not a bug here.

This script is built to track **new messages in conversations you already
have** — good for ongoing threads, not for catching cold/first-time DMs.
If you need to catch every DM including first-time senders, that requires
X's official pay-per-use API instead (see the "Handling multiple Twitter
accounts" doc from setup for the cost breakdown).

## Also read before using

twikit is against X's Terms of Service (it logs in and replays X's internal
web-app calls rather than using a sanctioned API). Automating DM access this
way carries a real risk of the account being rate-limited or suspended. Only
connect accounts whose owners understand and accept that risk.

## How it works

1. A GitHub Actions workflow runs on a schedule (`*/15 * * * *` by default).
2. For each configured account, it logs in with saved session cookies
   (`auth_token` + `ct0` — not a password) and checks each known contact's
   DM thread for new messages.
3. New messages get forwarded to Telegram via `sendMessage`.
4. The last-seen message ID per contact is saved to `state.json` and
   committed back to the repo, so the next run knows what's new.

## Setup

### 1. Create the Telegram bot

Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` →
follow the prompts → copy the bot token it gives you.

Then message your new bot once (any text) so it's allowed to message you
back, and get your chat ID by visiting:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `"chat":{"id": ...}` in the response.

### 2. Get session cookies for each X account

For each account you're monitoring, the account owner should:

1. Log into x.com normally in their browser.
2. Open DevTools → Application (Chrome) or Storage (Firefox) → Cookies →
   `https://x.com`.
3. Copy the values of two cookies: `auth_token` and `ct0`.

These expire (typically somewhere between ~30 days and a few months) and
need re-exporting when they do — the poller will message your default
Telegram chat when it detects a session has died.

### 3. Find each contact's numeric user ID

`known_contacts` needs X's internal numeric user ID, not a @handle. Easiest
way: look up the account at a site like tweeterid.com, or use twikit's
`get_user_by_screen_name()` once locally and note the `.id` it returns.

### 4. Add GitHub Secrets

In the repo: Settings → Secrets and variables → Actions → New repository
secret. Add:

| Secret | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | from step 1 |
| `TELEGRAM_CHAT_ID` | from step 1 |
| `TWITTER_ACCOUNTS` | a JSON array — see `accounts.example.json` for the shape |

`TWITTER_ACCOUNTS` holds every monitored account in one secret (easier to
manage than one secret per credential at scale). Copy
`accounts.example.json`, fill in real values, minify it to one line, and
paste the whole thing as the secret's value.

### 5. Enable Actions and let it run

The workflow runs automatically on schedule once secrets are set. You can
also trigger a run manually from the repo's Actions tab
("Poll DMs and relay to Telegram" → Run workflow) to test immediately.

## GitHub Actions minutes

This repo is **public**, so Actions minutes on standard runners are free
and unlimited — poll as often as you like (default: every 15 minutes). If
you ever make the repo private, the free plan caps you at 2,000
minutes/month, which works out to roughly a check every 2–3 hours at this
workflow's per-run cost — see the setup doc for the full math.

## Files

- `poller.py` — the script that does the checking and forwarding
- `.github/workflows/poll.yml` — the schedule that runs it
- `accounts.example.json` — shape of the `TWITTER_ACCOUNTS` secret
- `state.json` — last-seen message IDs (committed automatically by the workflow)
- `requirements.txt` — Python dependencies

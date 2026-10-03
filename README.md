# Telegram DM Relay

Forwards new X (Twitter) direct messages to a Telegram chat, using
[twikit](https://github.com/d60/twikit) (unofficial, no API key/cost) and a
GitHub Actions cron job instead of X's paid webhook API.

## Inbox discovery (catches first-time DMs too)

twikit's public API has no "list my inbox" method — it can only fetch
history for a conversation you already know the other person's numeric
user ID for (`get_dm_history(user_id)`), which would miss anyone DMing you
for the first time.

This script works around that: `poller.py` calls X's real inbox endpoint
(`dm/inbox_initial_state.json`) directly and unofficially, using a
constant (`Endpoint.DM_INBOX`) that exists in twikit's source but was
never wrapped in a public method. **Confirmed working as of 2026-10**
against a live account — it returns the whole inbox, including
conversations from people who have never messaged the account before.

If that call ever fails (X changes the response shape, rate-limits it,
etc.), each account automatically falls back to the known-contacts method
above (`known_contacts` in the account config) — which only catches new
messages in conversations you've already had, not first-time senders.
Check the Action's run logs if you want to confirm which path an account
is using.

⚠️ This, like all of twikit, relies on reverse-engineered internal
endpoints rather than a stable public API, so it can break whenever X
changes something. See the "Patch twikit for X's homepage migration"
step in `.github/workflows/poll.yml` for an example of exactly that kind
of breakage and fix.

## Also read before using

twikit is against X's Terms of Service (it logs in and replays X's internal
web-app calls rather than using a sanctioned API). Automating DM access this
way carries a real risk of the account being rate-limited or suspended. Only
connect accounts whose owners understand and accept that risk.

## How it works

1. A GitHub Actions workflow runs every 5 minutes (GitHub's real minimum
   for scheduled workflows — anything shorter is silently skipped).
2. It first checks Telegram for new self-registration messages (see
   "Adding accounts" below) and folds any into the account list.
3. For each configured account, it logs in with saved session cookies
   (`auth_token` + `ct0` — not a password) and checks the inbox for new
   messages (falling back to known-contacts polling if that fails).
4. New messages get forwarded to Telegram via `sendMessage`, with the
   account and sender's handles linked to their X profiles and a link
   into the DM conversation itself.
5. State (last-seen message IDs, the account list, the registration
   offset) is saved and committed back to the repo, so the next run picks
   up where this one left off.

## Adding accounts (self-registration via Telegram)

Account owners add themselves — no manual secret-editing needed. They
send ONE message to the bot, in any line order:

```
username @theirhandle
auth_token their_auth_token_cookie_value
ct0 their_ct0_cookie_value
code THE_SHARED_PASSCODE
```

`register.py` (run automatically each workflow cycle) parses this,
checks the passcode against the `REGISTRATION_CODE` secret, and — if it
matches — adds or updates that account, automatically setting its
`telegram_chat_id` to whichever chat they messaged from. It replies in
Telegram confirming success (or that the passcode was wrong, without
revealing the correct one). A wrong-format message is ignored silently —
nothing breaks from stray chat.

The account list itself lives in `accounts.enc`, encrypted with
`ACCOUNTS_ENCRYPTION_KEY` and committed to the repo. This is safe even
though the repo is public: without that key (which only exists as a
GitHub secret, never in the repo), the file is unreadable. This exists
because GitHub Actions secrets are write-only — there's no API to read
`TWITTER_ACCOUNTS` back and merge a new registration into it, so the
account list had to move somewhere mergeable.

**Keep `REGISTRATION_CODE` reasonably private** — anyone who has it (and
finds the bot) can register or overwrite an account entry. Share it only
with people you're actually onboarding, same as you'd share the bot's
name.

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
| `TELEGRAM_CHAT_ID` | your own default chat, used for session-expiry alerts |
| `ACCOUNTS_ENCRYPTION_KEY` | a Fernet key (`python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) — generate once, never changes |
| `REGISTRATION_CODE` | a passcode you make up and share with people you onboard |
| `PROXY_URL` | *(optional)* a single proxy URL, e.g. `http://user:pass@host:port` — see "Residential proxy" below |
| `PROXY_URLS` | *(optional)* several proxy URLs, comma-separated — each account sticks to one, spreading load across the pool. Takes priority over `PROXY_URL` when both are set |

Accounts themselves are **not** set as a secret — see "Adding accounts"
above. `accounts.example.json` still shows the shape of one account entry
for reference (e.g. if you ever want to seed `accounts.enc` by hand
instead of via Telegram registration).

### 5. Enable Actions and let it run

The workflow runs automatically on schedule once secrets are set. You can
also trigger a run manually from the repo's Actions tab
("Poll DMs and relay to Telegram" → Run workflow) to test immediately.

## GitHub Actions minutes

This repo is **public**, so Actions minutes on standard runners are free
and unlimited — poll as often as you like (default: every 5 minutes, GitHub's real floor). If
you ever make the repo private, the free plan caps you at 2,000
minutes/month, which works out to roughly a check every 2–3 hours at this
workflow's per-run cost — see the setup doc for the full math.

## Polling frequency: why there are 5 cron lines, not 1

GitHub Actions cron has no sub-minute resolution, and **a single cron line
can't go below every 5 minutes** — anything shorter on one line is
silently skipped, not an error. But a *second* line with a different
minute offset (e.g. `1-59/5 * * * *` instead of `0-59/5 * * * *`) is still
"every 5 minutes" on its own, it just lands on different minutes — so
stacking several offset lines gives you a faster *combined* cadence
without ever violating the 5-minute-per-line rule.

Minute offsets only go from 0 to 4 before they start repeating (offset 5 is
the same minute as offset 0), so **5 lines is the real ceiling** for this
trick — `poll.yml` already uses all 5, giving an effective ~1-minute
combined cadence. Adding a 6th, 7th, ... line (or duplicating the whole
workflow file) wouldn't buy anything: it would just double up with one of
the 5 existing offsets and fire two runs at the same minute for no reason.

Two things worth knowing about that ~1-minute number:

- It's the *scheduled* cadence, not a guarantee. GitHub explicitly
  deprioritizes scheduled (cron) runs under platform load, and that delay
  is a platform-wide thing — it happens to everyone's scheduled workflows,
  not just this repo's — so actual delivery can still lag behind what the
  cron lines say, especially during busy periods.
- Going faster isn't obviously *safer*, even if it were possible. The
  Cloudflare blocks this relay already runs into (see the "Inbox
  discovery" section and the code comments in `poller.py`) are X's
  anti-bot system reacting to *this account's* request pattern — hitting
  it more often is more likely to make that worse, not better. If you ever
  see the owner-alert chat (see below) light up with `blocked` or
  `ratelimit` messages more than occasionally, that's a sign to pull this
  back, not push it further.

The workflow also sets `concurrency: { group: poll-dms, cancel-in-progress:
false }` so if a run is ever still going when the next offset's minute
arrives, it queues instead of running in parallel — two runs touching
`state.json` and Telegram at the same time could otherwise forward the
same DM twice.

## Residential proxy

GitHub Actions runners share a small, well-known range of datacenter IPs
that X/Cloudflare already treats with suspicion, independent of whether
your cookies are valid. Routing requests through a residential proxy
gives each run a consumer-ISP IP instead, which is the main lever against
that specific `blocked` alert type (it won't help with `auth` cookie
expiry or 429 rate limits, which are unrelated).

`twikit`'s `Client` accepts a `proxy=` URL natively — no extra library
needed. This repo wires it up via the optional `PROXY_URL` secret above,
or a per-account `"proxy"` field in `accounts.enc` if you ever want
different accounts to exit through different IPs.

### Using Bright Data

If you're setting up Bright Data specifically: use the **Residential
Proxies** product — not Web Unlocker, Browser API, or SERP API. Those
three are different products (a scraping/rendering API with its own
response format, a hosted remote browser, and a search-results API,
respectively); none of them hand you a plain `host:port` you can drop
into another HTTP client the way `twikit` needs. Residential Proxies is
the plain rotating-IP proxy product, which is what `proxy=` expects.

An account-level **API key/token** (the kind shown on your dashboard
overview page) is *not* the same thing as proxy credentials and isn't
used here — don't put it in `PROXY_URL`. What you need instead:

1. In the Bright Data dashboard, create a zone under **Residential
   Proxies** (if you haven't already).
2. Open that zone's **Access parameters** / **Overview** tab. It shows a
   username (looks like `brd-customer-<your_customer_id>-zone-<zone_name>`),
   a password, and a host:port (typically `brd.superproxy.io:33335`).
3. Build the URL: `http://<username>:<password>@brd.superproxy.io:33335`.
4. Set that whole string as the `PROXY_URL` GitHub secret (step 4 above).

No code or workflow changes are needed beyond setting the secret — it's
already read by `poller.py` and passed through by the workflow.

**Security note:** since an API token was pasted into this chat, treat it
as exposed — it's worth rotating/regenerating in the Bright Data
dashboard, even though it isn't what gets used as `PROXY_URL`.

### Spreading accounts across several proxies

If you're monitoring more than a handful of accounts, don't route all of
them through one proxy IP -- that just moves the "too much traffic from
one IP" problem from GitHub's runners to your proxy. Set `PROXY_URLS`
(comma-separated) instead of `PROXY_URL`, e.g.:

```
http://user1:pass1@1.2.3.4:6754,http://user1:pass1@5.6.7.8:6014,http://user1:pass1@9.10.11.12:6641
```

Each account is deterministically assigned one proxy from the list by
hashing its label, and keeps using that same proxy on every run (not
randomized per run). That's intentional: an X session that suddenly
connects from a different IP/country every few minutes looks like
account takeover to X's own fraud detection -- a bigger risk than one
proxy IP seeing a bit more traffic. With N accounts spread over M
proxies, each proxy only ever has to carry ~N/M accounts' worth of
traffic.

A per-account `"proxy"` field in `accounts.enc` still overrides both
`PROXY_URL` and `PROXY_URLS` for that one account, if you ever need to
pin a specific account to a specific IP by hand.

## Error alerts

Token expiry, rate limiting, Cloudflare blocks, and any other unhandled
error get sent as a Telegram message to a fixed "owner alert" chat
(`OWNER_ALERT_CHAT_ID` in `poller.py`) — separate from the per-account
relay chats, since these are "something needs your attention" messages,
not DMs. Each distinct problem (per account) is throttled to at most one
alert every 30 minutes, so a persistent issue doesn't spam you once per
run — you'll still get the very first one immediately. Alert types:

- **`blocked`** — Cloudflare/anti-bot 403. Usually transient; frequent
  ones may mean the polling frequency needs to come down.
- **`auth`** — 401. The account's `auth_token`/`ct0` cookies have likely
  expired or been revoked and need re-exporting (see the onboarding doc).
- **`ratelimit`** — 429 from X.
- **`suspended`** / **`locked`** — X has suspended or locked the account.
- **`error`** — anything else unhandled, so a real bug doesn't fail
  silently.

## Files

- `poller.py` — the script that does the checking and forwarding
- `.github/workflows/poll.yml` — the schedule that runs it
- `accounts.example.json` — shape of one account entry (reference only)
- `accounts.enc` — the real, encrypted account list (committed automatically; unreadable without `ACCOUNTS_ENCRYPTION_KEY`)
- `register.py` — parses Telegram self-registration messages into `accounts.enc`
- `state.json` — last-seen message IDs (committed automatically by the workflow)
- `registration_state.json` — last Telegram update ID processed by `register.py` (committed automatically)
- `requirements.txt` — Python dependencies

# rank.uz watcher

Polls the .UZ registry every 30 minutes, pings Telegram the moment `rank.uz`
changes state, and sends one summary every day at 08:00 Tashkent time.

No server, no database, no dependencies — one stdlib Python file run by GitHub
Actions, with `state.json` in this repo as the memory.

## Why this domain, and when it drops

| | |
|---|---|
| Expired | 13.08.2026 |
| Pending renewal (7 working days, NS switched off) | ~14.08 – 24.08.2026 |
| **Redemption period (30 calendar days)** | ~24.08 – **23.09.2026** |
| Then | released → **usually to the UZEX auction**, not to open registration |

`23.09.2026` is an estimate: the registry publishes the rule (7 working days +
30 calendar days) but not the exact drop timestamp, and drops are often batched.
Treat it as ±3 days — which is exactly why this thing polls instead of guessing.

Released .UZ domains are normally listed on **https://domain.uzex.uz** rather
than becoming first-come-first-served. Recent starting prices: 484,000 UZS for
3-character names, 704,000 UZS for premium ones, auctions running ~5 days.
**Register and fund a UZEX account before 23.09**, or the alert arrives and you
can't act on it.

## What it watches

One authoritative source: `https://cctld.uz/whois/?domain=rank.uz&lang=eng`.
That page carries the registry's own status field, which covers every outcome:

| Registry status | Alert says |
|---|---|
| `Redemption period` (`W_RED`) | current state — no alert |
| `Active` (`ACTIV`) + new expiry | **owner renewed, it's gone** |
| `Auction` | **it's at auction — go bid** |
| `Domain ... is not found` | **it dropped — register it now** |

It also fingerprints the expiry date, registrar and nameservers, so a silent
back-office change still triggers an alert.

## Setup (~10 minutes)

**1. Telegram bot**

- Open [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
- Send your new bot any message (it can't message you until you do).
- Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
  `result[0].message.chat.id` — that's your chat id.

**2. Repo**

```bash
cd ~/Desktop/rankuz-monitor
git init -b main
git add . && git commit -m "rank.uz watcher"
gh repo create rankuz-monitor --private --source=. --push
```

**3. Secrets** — repo → Settings → Secrets and variables → Actions → New secret:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

**4. Turn it on** — Actions tab → enable workflows → run **watch rank.uz**
manually once. You should get a "Watching rank.uz" message showing the current
status. That message *is* the proof the parser works against the live site.

## Running it locally

```bash
python3 check.py --selftest   # parser tests, no network
python3 check.py --test       # send a test Telegram message
python3 check.py              # one real check
python3 check.py --daily      # one real check + force the summary
```

Without the two env vars set, messages are printed instead of sent — handy for
a dry run.

## Polling schedule and Actions limits

The repo is **public**, so Actions minutes are unmetered and the request rate is free.

Measured reality: GitHub delivered only **14% of scheduled runs** (83 of ~598) over
the first 13 days on the Free plan — a check every 3.65h instead of every 30 min,
worst gap 6.9h. Its docs call `schedule` best-effort and warn that queued jobs
"may be dropped." So the cron rate is a *request*, not a guarantee, and the way to
raise the delivered rate is to ask for more.

Current: `2-59/5 * * * *` (288 requests/day) for the drop window.
After the domain resolves, put it back to `13,43 * * * *`.

The 08:00 Tashkent summary is decided in `check.py`, not by a second cron, so a
dropped or delayed run still sends it exactly once a day.

## Things worth knowing

- **GitHub cron is approximate.** Scheduled runs can be delayed by several
  minutes under load. Fine here: UZEX auctions run for days, so 30-minute
  resolution loses you nothing.
- **Silence is verified, not assumed.** After 4 consecutive failed checks the
  bot sends a warning, and a message when it recovers — so "no news" always
  means "checked and unchanged".
- **A broken page is never read as a change.** If the response doesn't contain
  either a status table or the "is not found" notice, it's recorded as a
  failure, not a drop. No false "it's free!" alerts from a 502.
- **GitHub disables schedules in repos with 60 days of no activity.** Irrelevant
  for a ~6-week watch, but don't leave it running for a year and trust it.

# Voice Finance Tracker Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Fully offline/local personal finance tracker over Telegram. Send a voice
message (or just type) describing income or an expense; the bot transcribes
it (Moonshine), categorizes it (a local Ollama model), and logs it to
SQLite. No OpenAI API, no cloud database, no 24/7 server — it only needs to
be running while your machine is on, and picks up exactly where it left off.


## Table of contents

- [How it works](#how-it-works)
- [What it does](#what-it-does)
- [Setup](#setup)
- [Usage](#usage)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Audit notes](#audit-notes)
- [Contributing](#contributing)
- [License](#license)

## How it works

- **Speech-to-text:** [Moonshine](https://github.com/moonshine-ai/moonshine)
  (ONNX build), runs on CPU, ~57MB for the `base` model.
- **Categorization/reasoning:** a local [Ollama](https://ollama.com) model —
  `gemma3:4b` by default, sized for your hardware.
- **Queue:** Telegram's own update history. The bot's long-poll offset is
  stored in SQLite and only advanced after each message is fully handled, so
  a crash or shutdown mid-batch resumes exactly where it left off — nothing
  is skipped or double-processed on the happy path.
- **Storage:** one SQLite file for both the transaction ledger and bot state.

## What it does

- Detects income vs. expense automatically from what you say — no manual
  tagging.
- Invents categories freely from context (no fixed list).
- Every saved transaction gets a unique ID.
- `history` — shows your last 20 transactions (ID, amount, category,
  description).
- `/export` — exports the full transaction history as a CSV file.
- `delete transaction <id>` (also accepts `/delete 23`, `delete #23`,
  `remove entry 23`) — soft-deletes a transaction by ID.
- `balance` — all-time income/expense/net, with a savings rate.
- `weekly` / `monthly` — spending breakdown with a category pie chart.
- If a voice message can't be understood, the bot retries locally up to
  `MAX_PARSE_ATTEMPTS` times, then replies to that specific message asking
  you to resend it. **Reply to that prompt** (not a fresh message) to fix
  it — the rest of your queued messages keep processing in the meantime.
- On startup: a "back online" ping, then a "N saved / N flagged" summary
  once it finishes draining the queue.
- On the last calendar day of each month: an automatic weekly (last 7 days)
  and monthly pie-chart summary, sent as images with a caption.

## Setup

1. **Prerequisites**
   - Python 3.10+
   - `ffmpeg` on PATH (`sudo apt install ffmpeg`, `brew install ffmpeg`, or
     the Windows build from ffmpeg.org)
   - [Ollama](https://ollama.com/download) installed, then:
     ```sh
     ollama pull gemma3:4b
     ```

2. **Clone and install Python dependencies**
   ```sh
   git clone https://github.com/OWNER/finance-tracker-bot.git
   cd finance-tracker-bot
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   (The Moonshine install pulls from GitHub; it needs internet access once,
   not at every run.)

3. **Configure**
   ```sh
   cp .env.example .env
   ```
   Fill in:
   - `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `OWNER_CHAT_ID` — your numeric Telegram user ID (get it from
     [@userinfobot](https://t.me/userinfobot)). The bot ignores every chat
     that isn't this one, so a stranger who finds your bot can't log fake
     transactions.

   `.env` is git-ignored — never commit it.

4. **Run**
   ```sh
   python bot.py
   ```
   Leave it running while your machine is on. Closing the laptop / killing
   the process is safe — restart it whenever, and it resumes from the last
   update it fully processed.

5. **Run it automatically whenever you log in (optional)**
   - **Linux (systemd user service):** create
     `~/.config/systemd/user/financebot.service` running
     `python /path/to/bot.py`, then `systemctl --user enable --now financebot`.
   - **macOS:** a `launchd` plist in `~/Library/LaunchAgents/` pointed at
     `bot.py`.
   - **Windows:** a Task Scheduler task triggered "at log on".

## Usage

Once the bot is running, message it on Telegram (from the `OWNER_CHAT_ID`
account) — by voice note or by typing:

```
i spent 10k on lunch
bought coffee 15k
salary 3 million
taxi 20,000
```

Or use the menu buttons / commands: `/weekly`, `/monthly`, `/balance`,
`/export`, `/history`, `/delete <id>`, `/help`.

## Project layout

| File | Purpose |
|---|---|
| `bot.py` | Long-poll loop, command handling, transaction flow, periodic summaries |
| `db.py` | SQLite schema and all queries |
| `llm.py` | Ollama prompt + response validation/retry |
| `stt.py` | ffmpeg conversion + Moonshine transcription |
| `charts.py` | Pie chart + summary text rendering |
| `config.py` | Env var loading |
| `test_bot.py` | Unit tests (mocked Ollama/Moonshine/Telegram) |

## Testing

```sh
python3 test_bot.py
```

All 41 tests mock Ollama, Moonshine, and the Telegram API, and run against a
disposable `test_finance_bot.db` SQLite file — no external services,
credentials, or network access required. CI runs this on every push and
pull request (see `.github/workflows/ci.yml`).

## Troubleshooting

- **`RuntimeError: Missing required environment variable 'BOT_TOKEN'`** —
  you haven't created `.env` yet; see [Setup](#setup) step 3.
- **Bot doesn't respond** — confirm `OWNER_CHAT_ID` matches the Telegram
  account you're messaging from; the bot silently ignores every other chat.
- **`ffmpeg failed converting voice note`** — `ffmpeg` isn't on PATH; install
  it and restart the bot.
- **Ollama call failed / categorization always fails** — make sure
  `ollama serve` is running and `ollama pull gemma3:4b` (or your configured
  `OLLAMA_MODEL`) has completed.

## Audit notes

`test_bot.py` covers the DB layer, LLM response validation (valid/malformed/
unclear/retry-exhaustion), the delete-command regex, chart generation, and
the full `handle_update` control flow (owner-only filtering, history/balance/
delete commands, save path, unclear→pending-resend→resolved-by-reply path,
and that a reply to a pending resend is treated as transaction text even if
it reads like a command).

One real bug was caught and fixed during development: `insert_transaction`'s
duplicate-message guard originally swallowed *every* `IntegrityError`,
including a bad amount or type, silently returning `None` instead of
surfacing the problem. It now only treats it as "already saved" when the
error is actually the unique-message-id constraint; anything else re-raises.

**Known, accepted limitation:** if the process crashes in the narrow window
after inserting a transaction (or creating a resend prompt) but before the
offset is persisted, restarting will re-fetch that one update. A duplicate
transaction is prevented by the unique constraint on the Telegram message
ID (verified in tests), but a duplicate "please resend" prompt could in
theory be sent twice in that exact window. Harmless, just a repeated
message — true exactly-once delivery across crashes would need distributed
transactions, which isn't worth the complexity for a single-user bot.

**Interpretation call:** the weekly and monthly summaries both fire together
on the last day of the month, rather than the weekly one running separately
every Sunday. If you'd rather have the weekly one land every Sunday, that's
a small change to `maybe_send_periodic_summaries` in `bot.py`.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for setup
and PR guidelines.

## License

[MIT](LICENSE)

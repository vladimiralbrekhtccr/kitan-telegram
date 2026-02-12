# kitan-telegram

Telegram bridge for Paper Parser + live tmux/Codex workflow.

## What It Does

- Receives Telegram messages with long polling.
- Relays plain text into tmux pane (default `main:0.0`) as:
  - `[telegram] <message>`
- Sends quick Telegram ack (`Got it.`).
- Exposes local outbound reply API for this server:
  - `POST http://127.0.0.1:8788/reply`
- Exposes local metrics endpoint:
  - `GET http://127.0.0.1:8788/metrics`
- Supports Paper Parser commands (`/search`, `/paper`, `/stats`), Codex commands (`/ask`, `/toai`), tmux commands, and Piper TTS.

## Docs

- Detailed operator workflow:
  - `TELEGRAM_INTERACTION_GUIDE.md`
- Workspace-level cross-chat rules:
  - `/home/foggen/AGENTS.md`

## Setup

1. Configure env:

```bash
cd /home/foggen/kitan-telegram
cp .env.example .env
```

2. Fill required values in `.env`:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_CHAT_IDS` (recommended)
- `TELEGRAM_TMUX_TARGET_PANE` (if using relay)
- `TELEGRAM_LOCAL_REPLY_API_TOKEN` (for secure local `/reply`)

3. Install deps:

```bash
cd /home/foggen/kitan-telegram
uv sync
```

4. Run manually:

```bash
cd /home/foggen/kitan-telegram
uv run python bot.py
```

## Run As Service

```bash
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitan-telegram-bot.service
sudo systemctl status kitan-telegram-bot.service --no-pager
```

Logs:

```bash
sudo journalctl -u kitan-telegram-bot.service -n 80 --no-pager
```

## Local Reply API (Outbound Telegram)

Use this from the server to send explicit replies to user:

```bash
curl -sS -m 3 -X POST http://127.0.0.1:8788/reply \
  -H "Authorization: Bearer ${TELEGRAM_LOCAL_REPLY_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":1607870670,"text":"Done. Task completed."}'
```

Notes:
- Async queue: API returns quickly (`queued: true`).
- Dedupe: same `chat_id + text` is suppressed for `TELEGRAM_LOCAL_REPLY_API_DEDUPE_SEC`.
- IPv4 transport is enabled by default to reduce delivery lag (`TELEGRAM_FORCE_IPV4=true`).

Metrics quick check:

```bash
curl -sS http://127.0.0.1:8788/metrics
```

## Commands

- `/start`, `/help`, `/id`, `/ping`
- `/ops`
- `/aistatus`, `/ask <text>`, `/toai <text>`
- `/tmuxstatus`, `/totmux <text>`
- `/search <query>`, `/paper <id>`, `/stats`
- `/piperlangs`, `/piper <lang> <text>`

## Repository Hygiene

- `.env` is ignored (never commit secrets).
- Runtime assets are ignored by default:
  - `models/`
  - `vendor/`
- Keep deploy/service files and source code in git.

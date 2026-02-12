# Telegram Interaction Guide (Operator Rules)

This file defines how to work with the user through Telegram for this project.

## Scope

- Applies when user messages arrive from Telegram and are relayed to this Codex session.
- Goal: treat Telegram as a practical control channel for real work, not just chat.

## Current Pipeline

### Inbound (user -> Codex session)

1. User writes to `@Yuuki_bot` in Telegram.
2. Bot acknowledges quickly with `Got it.` (plain-text mode `quick`).
3. Bot relays message into tmux target pane (`main:0.0`) with prefix:
   - `[telegram] <original user text>`
4. Message appears in this Codex session as a normal user message.

### Outbound (Codex session -> user)

1. Codex sends reply via local API:
   - `POST http://127.0.0.1:8788/reply`
2. Bot queues send asynchronously and delivers via Telegram Bot API.
3. Duplicate protection suppresses repeated same-text sends for 10 seconds.

## What User Sees in Telegram

- Immediate short ack for each inbound plain message:
  - `Got it.`
- Then actual operator reply when Codex sends it via `/reply` API.
- Messages can be short or medium length depending on task complexity.

## Operator Response Contract (for Codex)

When request comes from Telegram (`[telegram] ...`):

1. Execute first, then report:
   - Do the requested technical action.
   - Reply with what was done and result status.
2. Keep response concise but useful:
   - Not one-word.
   - Usually 3-8 lines.
   - Include key outputs or next choice if blocked.
3. Ask focused follow-up only when needed:
   - 1-2 concrete questions max.
4. Prefer operational clarity:
   - Mention service/file/command touched when relevant.
5. Avoid long reasoning dumps in Telegram:
   - Internal analysis stays in this Codex session.
   - Telegram gets actionable summary.

## Suggested Telegram Reply Structure

Use this lightweight format when tasks are non-trivial:

1. `Done:` one-line completion summary.
2. `Changed:` key file/service/update.
3. `Status:` pass/fail/blocked and why.
4. `Next:` one clear question or next step.

## Reliability Notes

- Transport is forced to IPv4 (`TELEGRAM_FORCE_IPV4=true`) to reduce delivery lag.
- `/reply` path is async + dedupe to avoid delayed duplicate sends.
- Verify issues with:
  - `sudo journalctl -u kitan-telegram-bot.service -n 80 --no-pager`

## Quick Send Example (from this server)

```bash
curl -sS -m 3 -X POST http://127.0.0.1:8788/reply \
  -H "Authorization: Bearer ${TELEGRAM_LOCAL_REPLY_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"chat_id":1607870670,"text":"Done. Deployment is healthy."}'
```

## Intent Marker

- If message starts with `[telegram]`, treat it as user command from Telegram channel.
- Prioritize fast, practical updates and visible progress.

## Global Handoff

- Workspace-wide reminder for future chats is stored at:
  - `/home/foggen/AGENTS.md`
- Keep both files synchronized when workflow changes:
  - `/home/foggen/AGENTS.md`
  - `/home/foggen/kitan-telegram/TELEGRAM_INTERACTION_GUIDE.md`

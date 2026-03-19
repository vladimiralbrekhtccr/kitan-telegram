# Telegram Interaction Guide (Operator Rules)

This file defines how to work with the user through Telegram for this project.

## Persona Source

- Assistant identity/profile source:
  - `/home/foggen/SOUL.md`
- Keep Telegram replies aligned with that profile.

## Long-Term Memory Files

- Stable operating identity:
  - `/home/foggen/IDENTITY.md`
- Chronological task memory:
  - `/home/foggen/WORK_LOG.md`
- For non-trivial completed work, append a short entry to `WORK_LOG.md` so
  future sessions can recover context after days.

## Scope

- Applies when user messages arrive from Telegram and are relayed to this Codex session.
- Goal: treat Telegram as a practical control channel for real work, not just chat.
- Telegram is the primary/default communication channel for this workspace.

## Primary Channel Contract

For any message marked `[telegram]`:

1. Execute the requested task.
2. Always send a user-facing response back via `/reply`.
3. For long-running tasks, send a first status reply within 30 seconds.
4. While task is running, send progress replies every 5-10 minutes.
5. Ensure the response includes at least result/status (not a silent/no-op finish).
6. Never leave completion state only in tmux or local terminal output.

## Current Pipeline

### Inbound (user -> Codex session)

1. User writes to `@Yuuki_bot` in Telegram.
2. Bot acknowledges quickly with `Got it.` (plain-text mode `quick`).
3. Bot relays message into tmux target pane (`main:0.0`) with prefix:
   - `[telegram] <original user text>`
4. Message appears in this Codex session as a normal user message.

Image intake path:

1. User sends a photo/image document to `@Yuuki_bot`.
2. Bot saves image on VPS under `TELEGRAM_IMAGE_SAVE_DIR/YYYYMMDD/...`.
3. Bot forwards structured block to tmux:
   - `IMAGE_SAVED`
   - `path=...`
   - `caption=...`
   - `chat_id=...`
   - `message_id=...`

File intake path:

1. User sends a non-image document (for example `.md`, `.txt`, `.pdf`) to `@Yuuki_bot`.
2. Bot saves file on VPS under `TELEGRAM_FILE_SAVE_DIR/YYYYMMDD/...`.
3. Bot forwards structured block to tmux:
   - `FILE_SAVED`
   - `path=...`
   - `file_name=...`
   - `mime_type=...`
   - `size_bytes=...`
   - `caption=...`
   - `chat_id=...`
   - `message_id=...`

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

1. Always send outbound Telegram reply:
   - Every inbound `[telegram]` message must get a matching `/reply` response.
   - Do not leave response only inside tmux/Codex UI.
2. If execution is long, send an immediate progress reply first:
   - First outbound update within 30 seconds.
   - Then periodic updates every 5-10 minutes until done.
3. Execute first, then report:
   - Do the requested technical action.
   - Reply with what was done and result status.
4. Keep response concise but useful:
   - Not one-word.
   - Usually 3-8 lines.
   - Include key outputs or next choice if blocked.
5. Ask focused follow-up only when needed:
   - 1-2 concrete questions max.
6. Prefer operational clarity:
   - Mention service/file/command touched when relevant.
7. Avoid long reasoning dumps in Telegram:
   - Internal analysis stays in this Codex session.
   - Telegram gets actionable summary.
8. Prefer `/statusbrief` for quick health checks, `/statuscode` / `/statuscodejson` for integrations, and `/status` when detailed troubleshooting is needed.

## Autonomous Continuation Policy

- Current mode: `enabled` (per user preference).
- After a completed `[telegram]` task:
  1. If no blocker/question exists, continue with the next highest-value pending task in the same project scope.
  2. Send a concise Telegram update before and after each autonomous step.
  3. Keep steps small, verifiable, and reversible.
  4. Pause immediately when user sends stop/pause/redirection.
  5. Never perform destructive actions without explicit approval.

### Optional Auto-Nudge Loop

- `scripts/self_improve_nudge.sh` can periodically push a synthetic `[telegram]`
  message into tmux target pane.
- Use with timer `kitan-telegram-self-improve.timer` (default every 10 minutes).
- Keep message concise and action-oriented (`execute -> verify -> reply`).
- Optional dynamic planner for system-level autonomy:
  - `YUUKI_SELF_IMPROVE_AUTO_PLAN=true` enables generated next-step messages.
  - planner script default: `/home/foggen/kitan-telegram/scripts/self_improve_plan.py`
  - planner snapshot default: `runtime/self_improve_plan_latest.json`
  - timeout knob: `YUUKI_SELF_IMPROVE_PLAN_TIMEOUT_SEC`
- Script applies min-interval guard (`YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC`) and
  uses submit key logic compatible with active Codex tasks (`Tab` vs `Enter`).
- Self-improve can be health-gated:
  - `YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY=true` blocks synthetic nudges when health state is not `ok`.
- Optional Telegram cycle notify:
  - `YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM=true` sends a concise cycle result via local `/reply`.
  - `YUUKI_SELF_IMPROVE_NOTIFY_CHAT_ID` can pin target chat; fallback chain:
    `TELEGRAM_HEALTH_ALERT_CHAT_ID` -> first entry in `TELEGRAM_ALLOWED_CHAT_IDS`.
  - `YUUKI_SELF_IMPROVE_NOTIFY_ON_SKIP=true` includes `skipped` runs; default suppresses skip noise.
- Status visibility for this loop is available via `/status` using
  `YUUKI_SELF_IMPROVE_STATE_PATH`.

## Suggested Telegram Reply Structure

Use this lightweight format when tasks are non-trivial:

1. `Done:` one-line completion summary.
2. `Changed:` key file/service/update.
3. `Status:` pass/fail/blocked and why.
4. `Next:` one clear question or next step.

## Readable Telegram Formatting

When output contains operational data (cluster checks, logs, infra stats), use
this clearer layout:

1. One short heading line.
2. 3-6 bullets with labels:
   - `Context:` host/target.
   - `Checks:` commands executed.
   - `Result:` key numbers/status only.
   - `Action:` next concrete step.
3. Keep each bullet to one line where possible.
4. Avoid giant raw dumps unless explicitly asked.

## Reliability Notes

- Transport is forced to IPv4 (`TELEGRAM_FORCE_IPV4=true`) to reduce delivery lag.
- `/reply` path is async + dedupe to avoid delayed duplicate sends.
- `/reply` queue is bounded; overflow behavior is controlled by:
  - `TELEGRAM_LOCAL_REPLY_API_QUEUE_MAX`
  - `TELEGRAM_LOCAL_REPLY_API_QUEUE_DROP_OLDEST`
- Flapping counter persistence path:
  - `TELEGRAM_LOCAL_REPLY_API_METRICS_STATE_PATH`
  - persisted fields include:
    - `alert_transitions_total`
    - `queue_dropped_total`
    - `queue_full_rejected_total`
- Plain-text relay has optional dedupe window:
  - `TELEGRAM_TMUX_PLAIN_TEXT_DEDUPE_ENABLED`
  - `TELEGRAM_TMUX_PLAIN_TEXT_DEDUPE_SEC`
- Status/alert log scans are tail-limited for stable performance:
  - `TELEGRAM_STATUS_EVENT_SCAN_MAX_BYTES`
  - `TELEGRAM_ALERT_EVENT_SCAN_MAX_BYTES`
- Check `event_scan_complete` in `/status` when validating event-counter accuracy.
- `/status` also reports `event_scan_retry_used` when it had to expand scan window automatically.
- `/status` also reports `event_scan_retry_count` for retry depth visibility.
- Reliability self-test snapshot is exposed in `/status` from:
  - `YUUKI_SELF_TEST_STATUS_PATH`
  - `YUUKI_SELF_TEST_QUICK_STATUS_PATH`
- Reliability self-test execution profile:
  - `YUUKI_SELF_TEST_PROFILE=full|quick`
  - `YUUKI_SELF_TEST_ONLY=name1,name2` for explicit check subsets.
- Quick profile includes `monitoring_config_smoke` to catch alerting/routing config drift.
- Self-test includes `/reply` auth-mode coverage:
  - Bearer token accepted
  - `x-api-key` accepted
  - unauthorized requests rejected
  - open mode behavior when token is unset
- Quick transport-focused helper:
  - `scripts/self_test_transport_quick.sh`
- Optional periodic quick timer:
  - `deploy/kitan-telegram-self-test-quick.service`
  - `deploy/kitan-telegram-self-test-quick.timer`
  - writes snapshot to `runtime/self_test_quick_latest.json`.
- Health check also validates self-test freshness/failure:
  - `TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC`
  - `TELEGRAM_HEALTH_REQUIRE_QUICK_SELF_TEST`
  - `TELEGRAM_HEALTH_QUICK_SELF_TEST_MAX_AGE_SEC`
  - `TELEGRAM_HEALTH_SELF_TEST_FAIL_STREAK_THRESHOLD`
  - `TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_ENABLED`
  - `TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_TIMEOUT_SEC`
  - `TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_SCRIPT`
  - `TELEGRAM_HEALTH_CHECK_REPLY_AUTH` (live probe of `/reply` auth path; expected probe code `400`)
- Health payload includes quick-lane state in `self_test_quick`.
- Health payload includes reply-api auth probe under `reply_api`:
  - `auth_probe_enabled`
  - `auth_probe_ok`
  - `auth_probe_http`
- Health check attempts one self-test remediation before finalizing `self_test:*` failures.
- `health_status.json` now exposes remediation telemetry:
  - `self_test.autorepair_enabled`
  - `self_test.autorepair_attempted`
  - `self_test.autorepair_result`
- Health-check script supports test env override:
  - `TELEGRAM_HEALTH_ENV_FILE=/path/to/test.env`
- Status layer also marks stale health snapshots using:
  - `TELEGRAM_STATUS_HEALTH_STALE_SEC`
- Health check alert severity:
  - `self_test:stale` => `warning`
  - all other fail reasons => `critical`
- Health check queue-pressure fail signal:
  - `event:queue_pressure=<count>>=<threshold>`
  - controlled by `TELEGRAM_ALERT_QUEUE_PRESSURE_THRESHOLD`.
- Health check also emits repeated-check fail reasons:
  - `self_test:repeated_check:<check_name>` when the same failed self-test check
    repeats for `TELEGRAM_HEALTH_SELF_TEST_FAIL_STREAK_THRESHOLD` consecutive runs.
- Severity-aware cooldown knobs:
  - `TELEGRAM_HEALTH_ALERT_COOLDOWN_WARNING_SEC`
  - `TELEGRAM_HEALTH_ALERT_COOLDOWN_CRITICAL_SEC`
- Recovery signal:
  - `HEALTH_RECOVERY` is sent once when state transitions from alert to healthy.
- `/statusbrief` includes `health severity` + `health fail count` + `health stale/age` for quick operator checks.
- `/statusbrief` includes `health top=<...>` for fast first-pass cause visibility.
- `/statusbrief` includes `health alert_transitions=<...>` for quick flapping visibility.
- `/statusbrief` includes `health cd_left_s=<...>` for alert-cooldown visibility.
- `/statusbrief` includes `health last_age_s=<...>` for last-alert recency visibility.
- `/statusbrief` includes `health sig_short=<...>` for compact alert-signature visibility.
- `/statusbrief` includes `health sig8=<...>` for stable short hash correlation.
- `/statusbrief` includes `health quick_lane=<ok|stale|missing|failed|disabled|unknown>` for quick-lane visibility.
- `/statusbrief` includes `health reply_auth=<ok|fail|disabled|unknown>:<http_code>` for live `/reply` auth probe visibility.
  - idle normalization: when `in_alert=false` and no prior alert epoch exists, `last_age_s=0`.
- `/statusbrief` reply line includes `pressure=<ok|warn|critical>` and `pc=<count>/<threshold>`:
  - `ok` when no recent queue-pressure events exist
  - `warn` when there are recent events but count is below threshold
  - `critical` when count reaches `TELEGRAM_STATUS_QUEUE_PRESSURE_CRITICAL_THRESHOLD`.
  - on `critical`, compact hint code is set to:
    - `queue_pressure_critical_check_backpressure`
    - this allows machine routing without parsing human text.
- `/statusbrief` also includes self-test freshness/coverage fields:
  - `self_test checks`
  - `self_test stale`
  - `self_test age_s`
  - `self_test failed` names
- `/statusbrief` adds `self_test autorepair=<result>` only when health-check remediation was attempted.
- `/statusbrief` also surfaces health-check repeated-check signal:
  - `self_test repeat_hc` (repeated failed checks from health snapshot)
  - `self_test repeat_thr` (configured streak threshold)
- `/status` also includes:
  - `health_effective_state` (`ok|stale|degraded|critical|unknown`)
  - `health_operator_hint` (short next action)
  - in degraded/critical states, `health_operator_hint` becomes autorepair-aware when self-test remediation was attempted
  - `health_operator_hint_code` (stable token suitable for machine-driven branching)
- `/statuscode` returns one compact machine-readable line:
  - `state`, `hint_code`, `severity`, `fails`, `quick_lane`, `reply_auth`, `sig8`, `in_alert`, `stale`, `ts`
- `/statuscodejson` returns the same minimal keys as strict JSON plus:
  - `schema` and `version` for compatibility-safe parsing.
  - current version: `3`.
- Local API mirrors these compact status outputs:
  - `GET /statuscode`
  - `GET /statuscodejson`
- Local API also exposes Prometheus-friendly text snapshot:
  - `GET /metrics.prom`
  - includes derived routing gauge `yuuki_alert_should_page` (`1` yes, `0` no, `-1` unknown)
  - includes numeric quick-lane gauge `yuuki_health_quick_lane_state`
    (`ok=0, disabled=1, stale=2, missing=3, failed=4, unknown=-1`)
  - includes numeric reply-auth probe gauge `yuuki_reply_auth_probe_state`
    (`ok=0, disabled=1, fail=2, unknown=-1`)
  - includes flapping counter `yuuki_alert_transitions_total` (increments on known `in_alert` transitions)
  - `yuuki_statuscode_info` label set now also includes `quick_lane` and `reply_auth`.
  - example alert rules:
    - `deploy/monitoring/kitan-telegram-alerts.yml`
  - includes queue pressure counters:
    - `yuuki_reply_queue_dropped_total`
    - `yuuki_reply_queue_full_rejected_total`
- `/statusbrief` health line also includes `hint_code=<...>` for fast triage/routing.
- Watcher status now also includes health-gate snapshot:
  - `watcher_health_state`
  - `watcher_health_stale`
  - `watcher_require_healthy`
- Optional watcher (`scripts/yuuki_watcher.sh`) records idle/autonomy state in
  `YUUKI_WATCHER_STATE_PATH` and is exposed via `/status`.
- Watcher can be health-gated:
  - `YUUKI_WATCHER_REQUIRE_HEALTHY=true` blocks nudges when health state is not `ok`.
- If watcher timer is enabled, keep cooldown conservative to avoid noisy pings:
  - `YUUKI_WATCHER_IDLE_SEC`
  - `YUUKI_WATCHER_COOLDOWN_SEC`
- Verify issues with:
  - `sudo journalctl -u kitan-telegram-bot.service -n 80 --no-pager`

## Regression Log: Multiline Format

Date: `2026-02-13`

1. Symptom:
   Telegram `/help` rendered literal `\n` text instead of actual line breaks.
2. Root cause:
   Escaped-literal newline injection in outbound formatting/chunking path.
3. Prevention rules:
   Use `"\n"` for actual multiline joins in user-facing output.
   Never use `"\\n".join(...)` or string patterns like `f"...\\n..."` in outbound text construction.
4. Mandatory smoke check after text-path edits:
   - `cd /home/foggen/kitan-telegram && ./scripts/smoke_telegram_multiline.sh`
5. Mandatory smoke check after watcher/self-improve/health-gate edits:
   - `cd /home/foggen/kitan-telegram && ./scripts/smoke_autonomy_health_gates.sh`
   - if planner logic changed, also run:
     - `cd /home/foggen/kitan-telegram && ./scripts/smoke_self_improve_planner.sh`
6. Mandatory smoke check after `.gitignore`, runtime/log path, or local-state persistence edits:
   - `cd /home/foggen/kitan-telegram && ./scripts/smoke_repo_hygiene.sh`
7. Mandatory smoke check after `/reply` auth or local reply transport edits:
   - `cd /home/foggen/kitan-telegram && ./scripts/smoke_reply_auth.sh`
8. Mandatory smoke check after monitoring/alerting YAML edits:
   - `cd /home/foggen/kitan-telegram && ./scripts/smoke_monitoring_config.sh`
   - supports optional strict mode:
     - `YUUKI_MONITORING_REQUIRE_PROMTOOL=true`
     - `YUUKI_MONITORING_REQUIRE_AMTOOL=true`
9. Recommended one-command smoke before autonomous status report:
   - `cd /home/foggen/kitan-telegram && ./scripts/smoke_reliability_core.sh`
   - includes reply auth, health-gates, repo hygiene, multiline, monitoring config, and quick transport self-test.

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

## Gemini tmux Workflow (Second Chat)

Goal: run Gemini as a separate tmux chat (not direct API call) and allow controlled info exchange.

Scripts:

- Start/drive Gemini chat in tmux:
  - `/home/foggen/kitan-telegram/scripts/gemini_tmux_bridge.sh`
- Ask + mailbox + optional forward to another pane:
  - `/home/foggen/kitan-telegram/scripts/gemini_tmux_exchange.sh`

Default behavior:

1. Gemini runs in `tmux` window `main:gemini.0`.
2. Startup command uses no permission prompts:
   - `gemini --approval-mode yolo`
3. `ask` sends prompt to Gemini pane and waits for tagged answer.
4. `forward` can push Gemini reply into another tmux pane.

Examples:

```bash
/home/foggen/kitan-telegram/scripts/gemini_tmux_bridge.sh start
/home/foggen/kitan-telegram/scripts/gemini_tmux_exchange.sh ask "Give 3 headline hooks in RU."
/home/foggen/kitan-telegram/scripts/gemini_tmux_exchange.sh forward "Summarize in 4 bullets." "main:0.0"
```

Mailbox files:

- `/tmp/tmux-gemini-bridge/gemini_last_prompt.txt`
- `/tmp/tmux-gemini-bridge/gemini_last_reply.txt`

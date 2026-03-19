# kitan-telegram

Minimal Telegram bridge for:
- tmux relay
- Piper TTS
- image intake (save + relay path)
- file intake (save + relay path)
- local `/reply` API

## What It Does

- Receives Telegram messages with long polling.
- Relays plain text into tmux pane as:
  - `[telegram] <message>`
- Optional debounce merges rapid plain-text bursts into one relay payload (`TELEGRAM_TMUX_DEBOUNCE_*`).
- Optional dedupe suppresses identical plain-text repeats in a short window (`TELEGRAM_TMUX_PLAIN_TEXT_DEDUPE_*`).
- Supports `/totmux`, `/tmuxstatus`, `/status`, `/archivecfg`.
- Supports `/statusbrief` for compact operational snapshot.
- Supports `/statuscode` for minimal machine-readable health signal.
- Supports `/statuscodejson` for minimal machine-readable health signal as JSON.
  - includes `schema` and `version` for compatibility-safe consumers
- Supports Piper TTS:
  - `/piperlangs`
  - `/piper <lang> <text>`
- Supports image intake:
  - upload `photo` or `image/* document`
  - bot saves file to VPS and sends `IMAGE_SAVED` block
  - when tmux relay is available, the same `IMAGE_SAVED` block is forwarded to tmux
- Supports file intake:
  - upload non-image `document` (e.g. `.md`, `.txt`, `.pdf`)
  - bot saves file to VPS and sends `FILE_SAVED` block
  - when tmux relay is available, the same `FILE_SAVED` block is forwarded to tmux
- Exposes local outbound reply API:
  - `POST http://127.0.0.1:8788/reply`
  - queue is bounded (`TELEGRAM_LOCAL_REPLY_API_QUEUE_MAX`) with configurable overflow policy
    (`TELEGRAM_LOCAL_REPLY_API_QUEUE_DROP_OLDEST`)
- Exposes local health/metrics:
  - `GET http://127.0.0.1:8788/health`
  - `GET http://127.0.0.1:8788/metrics`
  - `GET http://127.0.0.1:8788/metrics.prom` (Prometheus text format)
- `GET http://127.0.0.1:8788/statuscode` (minimal machine status JSON)
- `GET http://127.0.0.1:8788/statuscodejson` (same + `schema`/`version`)
  - includes `quick_lane` (`ok|stale|missing|failed|disabled|unknown`)
  - includes derived gauge: `yuuki_alert_should_page` (`1` yes, `0` no, `-1` unknown)
  - includes flapping counter: `yuuki_alert_transitions_total` (increments on `in_alert` bool transitions)
- `/status` in Telegram also includes latest health snapshot from
  `TELEGRAM_HEALTH_STATUS_PATH` (written by `scripts/health_check.sh`).
- `/status` includes recent event counters over `TELEGRAM_STATUS_EVENT_LOOKBACK_MIN`.
- `/statusbrief` health line includes `alert_transitions=<n>` (from local flapping counter).
- `/statusbrief` reply line includes `pressure=<ok|warn|critical>` and `pc=<count>/<threshold>`
  based on recent queue overflow/drop events in lookback.
  - when pressure is `critical`, compact status hint code is overridden to
    `queue_pressure_critical_check_backpressure` for machine routing.
- `/status` event scan is tail-limited by `TELEGRAM_STATUS_EVENT_SCAN_MAX_BYTES`
  to keep status latency stable as logs grow.
- `/status` shows `event_scan_complete` so you can see when counters are exact vs tail-estimated.
- `/status` can auto-retry event scan with larger tail window when initial scan is incomplete (`event_scan_retry_used=true`).
- `/status` includes `event_scan_retry_count` (how many expansion retries were needed).
- `/status` includes `event_plain_text_deduped` counter for duplicate suppression visibility.
- `/status` includes watcher state from `YUUKI_WATCHER_STATE_PATH`.
- `/status` includes self-improve nudge state from `YUUKI_SELF_IMPROVE_STATE_PATH`.
- `/status` includes self-improve health-gate state (`self_improve_health_state`, `self_improve_require_healthy`).
- `/status` includes reliability self-test state from `YUUKI_SELF_TEST_STATUS_PATH`.
- `/status` marks health snapshot age/staleness using `TELEGRAM_STATUS_HEALTH_STALE_SEC`.
- JSONL log contains high-signal structured events (`event_type`, `event_fields`)
  for queue/send/retry/fail and intake flows.

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
- `TELEGRAM_LOCAL_REPLY_API_METRICS_STATE_PATH` (persistent state for flapping counter across restarts)
- `TELEGRAM_STATUS_QUEUE_PRESSURE_CRITICAL_THRESHOLD` (critical threshold for queue pressure in `/statusbrief`)
- `TELEGRAM_IMAGE_SAVE_DIR` (if you want non-default image storage path)
- `TELEGRAM_FILE_SAVE_DIR` (if you want non-default file storage path)
- `TELEGRAM_HEALTH_STATUS_PATH` (health snapshot file path for `/status`)

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

### Optional: Keep Codex Pane Alive

If you relay Telegram text into a specific tmux pane (for example `main:0.0`), you can keep that Codex pane auto-recovering with a watchdog service:

```bash
sudo cp /home/foggen/kitan-telegram/deploy/codex-tmux-guard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-tmux-guard.service
sudo systemctl status codex-tmux-guard.service --no-pager
```

This service restarts Codex in the target pane with `codex resume --last --dangerously-bypass-approvals-and-sandbox` when the process exits.
If you need strict thread continuity, set `CODEX_RESUME_SESSION_ID` in the service to pin a specific Codex session id instead of `--last`.

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
- IPv4 transport is enabled by default (`TELEGRAM_FORCE_IPV4=true`).
- Backpressure:
  - if queue is full and `...DROP_OLDEST=true`, oldest pending reply is dropped to accept newest.
  - if queue is full and `...DROP_OLDEST=false`, API responds `429 queue_full`.
- Flapping state persistence:
  - `yuuki_alert_transitions_total` is restored/saved via `TELEGRAM_LOCAL_REPLY_API_METRICS_STATE_PATH`.
  - queue pressure counters are also persisted:
    - `queue_dropped_total`
    - `queue_full_rejected_total`

Metrics quick check:

```bash
curl -sS http://127.0.0.1:8788/metrics
```

Prometheus scrape check:

```bash
curl -sS http://127.0.0.1:8788/metrics.prom
```

Example alert rules file:
- `deploy/monitoring/kitan-telegram-alerts.yml`
Alertmanager routing example:
- `deploy/monitoring/alertmanager-kitan-example.yml`
- includes:
  - critical health/page alert (`yuuki_alert_should_page`)
  - quick-lane stale/missing/failed alerts (`yuuki_health_quick_lane_state`)
  - `/reply` auth-probe fail alert (`yuuki_reply_auth_probe_state`)
  - queue drop/reject alerts
  - reply failure burst and failure-ratio alerts.

Queue pressure counters exported in Prometheus:
- `yuuki_reply_queue_dropped_total`
- `yuuki_reply_queue_full_rejected_total`

Quick-lane gauge exported in Prometheus:
- `yuuki_health_quick_lane_state`:
  - `ok=0`
  - `disabled=1`
  - `stale=2`
  - `missing=3`
  - `failed=4`
  - `unknown=-1`

Reply auth-probe gauge exported in Prometheus:
- `yuuki_reply_auth_probe_state`:
  - `ok=0`
  - `disabled=1`
  - `fail=2`
  - `unknown=-1`

## Commands

- `/start`, `/help`, `/id`, `/ping`
- `/tmuxstatus`, `/status`, `/statusbrief`, `/statusjson`, `/archivecfg`, `/totmux <text>`
- `/statuscode` (compact machine status line)
- `/statuscodejson` (same compact status as strict JSON + `schema`/`version`)
  - statuscode fields: `state`, `hint_code`, `severity`, `fails`, `quick_lane`, `reply_auth`, `sig8`, `in_alert`, `stale`, `ts`
  - current statuscode version: `3`
- `/piperlangs`, `/piper <lang> <text>`
- `photo` / `image document` upload: saves image and relays path block
- `document` upload (non-image): saves file and relays path block

## Optional Health Check Script

Script:
- `/home/foggen/kitan-telegram/scripts/health_check.sh`
- Supports env-file override for isolated tests:
  - `TELEGRAM_HEALTH_ENV_FILE=/path/to/test.env`

Suggested cron (every minute):

```bash
* * * * * /home/foggen/kitan-telegram/scripts/health_check.sh
```

Or use systemd timer:

```bash
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-health-check.service /etc/systemd/system/
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-health-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitan-telegram-health-check.timer
sudo systemctl status kitan-telegram-health-check.timer --no-pager
```

Optional alert thresholds (read by `health_check.sh`):
- `TELEGRAM_ALERT_EVENT_LOOKBACK_MIN`
- `TELEGRAM_ALERT_EVENT_SCAN_MAX_BYTES` (tail scan limit for JSONL parsing)
- `TELEGRAM_ALERT_REPLY_FAILED_THRESHOLD`
- `TELEGRAM_ALERT_RELAY_ERROR_THRESHOLD`
- `TELEGRAM_ALERT_QUEUE_PRESSURE_THRESHOLD` (critical queue-pressure threshold for `HEALTH_ALERT` fail signal)
- `TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC` (fail if reliability self-test snapshot is stale)
- `TELEGRAM_HEALTH_REQUIRE_QUICK_SELF_TEST` (require quick self-test lane in health-check)
- `TELEGRAM_HEALTH_QUICK_SELF_TEST_MAX_AGE_SEC` (quick lane stale threshold)
- `TELEGRAM_HEALTH_SELF_TEST_FAIL_STREAK_THRESHOLD` (repeat threshold for same failed self-test check name)
- `TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_ENABLED` (attempt one self-test refresh before failing)
- `TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_TIMEOUT_SEC` (timeout for the remediation run)
- `TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_SCRIPT` (script used for remediation, default self-test runner)
- `TELEGRAM_HEALTH_CHECK_REPLY_AUTH` (default: `true`; probes `/reply` auth path and expects HTTP `400` for empty JSON probe)
- `TELEGRAM_HEALTH_ALERT_COOLDOWN_SEC` (suppress repeated identical alerts)
- `TELEGRAM_HEALTH_ALERT_COOLDOWN_WARNING_SEC` (default: 1800)
- `TELEGRAM_HEALTH_ALERT_COOLDOWN_CRITICAL_SEC` (default: 900)
- `TELEGRAM_HEALTH_ALERT_STATE_PATH` (internal cooldown state file)
- `TELEGRAM_STATUS_HEALTH_STALE_SEC` (status-level stale detector threshold)

Alert behavior:
- `health_status.json` now includes `alert.severity` (`none|warning|critical`).
- `self_test:stale` is treated as `warning`; other failures are `critical`.
- repeated failures of the same self-test check add `self_test:repeated_check:<name>` into fails and are treated as `critical`.
- `health_check.sh` now attempts one self-test remediation before classifying `self_test:*` failures.
- `health_status.json` now records self-test remediation telemetry:
  - `self_test.autorepair_enabled`
  - `self_test.autorepair_attempted`
  - `self_test.autorepair_result`
- `health_status.json` includes quick-lane snapshot under `self_test_quick`
  (`required/path/max_age/exists/ok/stale/age/ts/failed_checks`).
- `health_status.json` includes reply-api probe snapshot under `reply_api`:
  - `auth_probe_enabled`
  - `auth_probe_ok`
  - `auth_probe_http`
- On transition from alert to healthy state, script emits `HEALTH_RECOVERY` once.
- `/statusbrief` now includes `health severity`, `health fail count`, and `health stale/age` for faster triage.
- `/statusbrief` now includes `health top=<...>` (top fail reasons from health snapshot).
- `/statusbrief` now includes `health cd_left_s=<...>` (seconds left until alert cooldown ends).
- `/statusbrief` now includes `health last_age_s=<...>` (age in seconds of last alert event).
- `/statusbrief` now includes `health sig_short=<...>` (compact alert fingerprint for cooldown correlation).
- `/statusbrief` now includes `health sig8=<...>` (stable 8-char hash of alert fingerprint).
- `/statusbrief` now includes `health quick_lane=<ok|stale|missing|failed|disabled|unknown>` for quick self-test lane visibility.
- `/statusbrief` now includes `health reply_auth=<ok|fail|disabled|unknown>:<http_code>` from live `/reply` auth probe.
- `/statusbrief` now includes `reply pressure=<ok|warn|critical>` (from recent `reply_queue_full` / `reply_queue_drop_oldest` events).
  - escalates to `critical` when pressure count reaches `TELEGRAM_STATUS_QUEUE_PRESSURE_CRITICAL_THRESHOLD`.
  - `health_check.sh` also emits `event:queue_pressure=...` when pressure count reaches `TELEGRAM_ALERT_QUEUE_PRESSURE_THRESHOLD`.
  - when alert state is idle (`in_alert=false`) and there is no prior alert epoch, `last_age_s=0`.
- `/statusbrief` now includes self-test freshness/coverage (`checks`, `stale`, `age_s`, `failed`) for reliability triage.
- `/statusbrief` now also includes health-check repeated-check signal:
  - `repeat_hc=<names>`
  - `repeat_thr=<threshold>`
- `/statusbrief` now adds `autorepair=<result>` on `self_test` line only when remediation was attempted.
- `/status` includes `health_effective_state` + `health_operator_hint` for next-action guidance.
- `health_operator_hint` now includes autorepair-aware guidance in degraded/critical states when remediation was attempted.
- `/status` now also includes `health_operator_hint_code` (stable enum-like token for machine routing).
- `/statusbrief` health line now includes `hint_code=<...>`.
- `/status` includes watcher health-gate fields (`watcher_health_state`, `watcher_health_stale`, `watcher_require_healthy`).

## Optional Autonomous Watcher

Script:
- `/home/foggen/kitan-telegram/scripts/yuuki_watcher.sh`

Purpose:
- tracks inbound-activity idle time from JSONL events
- writes watcher snapshot to `YUUKI_WATCHER_STATE_PATH`
- optionally sends a concise `/reply` status ping after long idle intervals
- can require healthy runtime state before nudging (`YUUKI_WATCHER_REQUIRE_HEALTHY=true`)

Install timer:

```bash
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-watcher.service /etc/systemd/system/
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-watcher.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitan-telegram-watcher.timer
sudo systemctl status kitan-telegram-watcher.timer --no-pager
```

Env knobs:
- `YUUKI_WATCHER_ENABLED`
- `YUUKI_WATCHER_DRY_RUN` (compute + state update, no outbound ping)
- `YUUKI_WATCHER_REQUIRE_HEALTHY` (block nudges unless health state is `ok`)
- `YUUKI_WATCHER_IDLE_SEC`
- `YUUKI_WATCHER_COOLDOWN_SEC`
- `YUUKI_WATCHER_STATE_PATH`
- `YUUKI_WATCHER_TASK_STATE_PATH`

## Optional: Self-Improve Nudge Every 10 Minutes

Script:
- `/home/foggen/kitan-telegram/scripts/self_improve_nudge.sh`

Purpose:
- injects a synthetic `[telegram] ...` command into the tmux target pane
- useful for autonomous continuation when user is idle
- uses same submit behavior as relay (`Tab` when task is running, else `Enter`)
- has min-interval guard to prevent repeated flood on manual/service restarts
- can require healthy runtime state before nudging (`YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY=true`)
- can send a short Telegram status update after each nudge cycle (via local `/reply`)

Install timer:

```bash
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-self-improve.service /etc/systemd/system/
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-self-improve.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitan-telegram-self-improve.timer
sudo systemctl status kitan-telegram-self-improve.timer --no-pager
```

Env knobs:
- `YUUKI_SELF_IMPROVE_ENABLED`
- `YUUKI_SELF_IMPROVE_TARGET_PANE` (fallback: `TELEGRAM_TMUX_TARGET_PANE`)
- `YUUKI_SELF_IMPROVE_PREFIX` (default: `[telegram]`)
- `YUUKI_SELF_IMPROVE_MESSAGE`
- `YUUKI_SELF_IMPROVE_STATE_PATH`
- `YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY` (block nudges unless health state is `ok`)
- `YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC`
- `YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM` (send short cycle status to Telegram)
- `YUUKI_SELF_IMPROVE_NOTIFY_CHAT_ID` (optional explicit target chat; fallback: `TELEGRAM_HEALTH_ALERT_CHAT_ID` or first allowed chat)
- `YUUKI_SELF_IMPROVE_NOTIFY_ON_SKIP` (include `skipped` cycles in Telegram notify)
- `YUUKI_SELF_IMPROVE_AUTO_PLAN` (enable dynamic next-step planner; default: `false`)
- `YUUKI_SELF_IMPROVE_PLAN_SCRIPT` (default: `/home/foggen/kitan-telegram/scripts/self_improve_plan.py`)
- `YUUKI_SELF_IMPROVE_PLAN_OUTPUT_PATH` (default: `/home/foggen/kitan-telegram/runtime/self_improve_plan_latest.json`)
- `YUUKI_SELF_IMPROVE_PLAN_TIMEOUT_SEC` (planner subprocess timeout; default: `8`)

## Optional: Reliability Self-Test

Script:
- `/home/foggen/kitan-telegram/scripts/self_test_reliability.sh`

Purpose:
- continuously validates internal reliability primitives (dedupe, queue backpressure, event-scan fallback)
- validates `/reply` auth modes (Bearer, `x-api-key`, unauthorized reject, and no-token mode)
- validates monitoring config integrity via `monitoring_config_smoke` (rules + alertmanager routing)
- writes machine-readable result to `YUUKI_SELF_TEST_STATUS_PATH`
- health check consumes this snapshot and alerts on `missing/failed/stale`.
- supports execution profiles:
  - `full` (default): complete test matrix
  - `quick`: transport-critical subset for faster cadence (includes monitoring config smoke)

Install timer:

```bash
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-self-test.service /etc/systemd/system/
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-self-test.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitan-telegram-self-test.timer
sudo systemctl status kitan-telegram-self-test.timer --no-pager
```

Env knobs:
- `YUUKI_SELF_TEST_STATUS_PATH`
- `YUUKI_SELF_TEST_QUICK_STATUS_PATH`
- `YUUKI_SELF_TEST_PROFILE` (`full` or `quick`)
- `YUUKI_SELF_TEST_ONLY` (optional CSV list of explicit check names)

Quick transport profile helper:
- `/home/foggen/kitan-telegram/scripts/self_test_transport_quick.sh`

Quick-profile timer (optional):

```bash
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-self-test-quick.service /etc/systemd/system/
sudo cp /home/foggen/kitan-telegram/deploy/kitan-telegram-self-test-quick.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kitan-telegram-self-test-quick.timer
sudo systemctl status kitan-telegram-self-test-quick.timer --no-pager
```

Quick timer writes snapshot to:
- `/home/foggen/kitan-telegram/runtime/self_test_quick_latest.json`

## Core Reliability Smoke Suite

Run one command to validate the critical reliability path end-to-end:

```bash
cd /home/foggen/kitan-telegram
./scripts/smoke_reliability_core.sh
```

This runs:
- `smoke_reply_auth.sh`
- `smoke_autonomy_health_gates.sh`
- `smoke_self_improve_planner.sh`
- `smoke_repo_hygiene.sh`
- `smoke_telegram_multiline.sh`
- `smoke_monitoring_config.sh`
- `self_test_transport_quick.sh`

It must print `PASS: reliability core smoke suite`.

## Regression Guard: Multiline Formatting

Recorded bug date: `2026-02-13`.
Symptom: Telegram showed literal `\n` in `/help` instead of line breaks.

Run this smoke check after editing any text/chunking/send path:

```bash
cd /home/foggen/kitan-telegram
./scripts/smoke_telegram_multiline.sh
```

It must print `PASS: Telegram multiline formatting checks`.

Run this smoke check after editing watcher/self-improve/health-gate logic:

```bash
cd /home/foggen/kitan-telegram
./scripts/smoke_autonomy_health_gates.sh
```

It must print `PASS: autonomy health-gate smoke checks`.

Run this smoke check after editing `.gitignore`, deploy/runtime paths, or local state persistence:

```bash
cd /home/foggen/kitan-telegram
./scripts/smoke_repo_hygiene.sh
```

It must print `PASS: repo hygiene smoke checks`.

Run this smoke check after editing local `/reply` auth/transport behavior:

```bash
cd /home/foggen/kitan-telegram
./scripts/smoke_reply_auth.sh
```

It must print `PASS: reply auth smoke checks`.

Run this smoke check after editing monitoring/alerting YAML:

```bash
cd /home/foggen/kitan-telegram
./scripts/smoke_monitoring_config.sh
```

It must print `PASS: monitoring config smoke checks`.
If `promtool`/`amtool` are missing it prints `SKIP` lines by default.
Set strict mode to fail when tools are absent:
- `YUUKI_MONITORING_REQUIRE_PROMTOOL=true`
- `YUUKI_MONITORING_REQUIRE_AMTOOL=true`

Rules to avoid recurrence:
- Use real newline joins for user-facing multiline text: `"\n".join(...)`.
- Do not use escaped-literal joins in outbound formatting paths: `"\\n".join(...)`.
- Do not inject escaped-literal newlines in chunking/building paths like `f"...\\n..."`.

## Repository Hygiene

- `.env` is ignored (never commit secrets).
- Runtime assets are ignored by default:
  - `models/`
  - `vendor/`
  - `logs/`
  - `runtime/`
- Keep deploy/service files and source code in git.

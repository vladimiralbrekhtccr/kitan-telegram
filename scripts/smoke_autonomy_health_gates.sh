#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

TMPD="$(mktemp -d)"
cleanup() {
  rm -rf "${TMPD}"
}
trap cleanup EXIT

WATCHER_COPY="${TMPD}/yuuki_watcher_test.sh"
SELF_IMPROVE_COPY="${TMPD}/self_improve_test.sh"
cp "${REPO_DIR}/scripts/yuuki_watcher.sh" "${WATCHER_COPY}"
cp "${REPO_DIR}/scripts/self_improve_nudge.sh" "${SELF_IMPROVE_COPY}"
chmod +x "${WATCHER_COPY}" "${SELF_IMPROVE_COPY}"

# Force test copies to ignore production .env
sed -i 's|^ENV_FILE="/home/foggen/kitan-telegram/.env"|ENV_FILE="/tmp/yuuki_noenv"|' "${WATCHER_COPY}"
sed -i 's|^ENV_FILE="/home/foggen/kitan-telegram/.env"|ENV_FILE="/tmp/yuuki_noenv"|' "${SELF_IMPROVE_COPY}"

LOGP="${TMPD}/events.jsonl"
TASKP="${TMPD}/LAST_STATE.md"
WSTATE_BAD="${TMPD}/watcher_bad.json"
WSTATE_OK="${TMPD}/watcher_ok.json"
SSTATE_BAD="${TMPD}/self_bad.json"
SSTATE_OK="${TMPD}/self_ok.json"
HEALTH_BAD="${TMPD}/health_critical.json"
HEALTH_OK="${TMPD}/health_ok.json"

python3 - "${LOGP}" "${TASKP}" "${HEALTH_BAD}" "${HEALTH_OK}" <<'PY'
import json
import sys
from datetime import datetime, timezone, timedelta

logp, taskp, hb, ho = sys.argv[1:5]
old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

with open(logp, "w", encoding="utf-8") as f:
    f.write(json.dumps({"ts_utc": old_ts, "event_type": "command_received", "event_fields": {"src": "smoke"}}) + "\n")

with open(taskp, "w", encoding="utf-8") as f:
    f.write("- Next: smoke gate regression\n")

with open(hb, "w", encoding="utf-8") as f:
    f.write(json.dumps({"ts_utc": now_ts, "ok": False, "alert": {"severity": "critical"}}))

with open(ho, "w", encoding="utf-8") as f:
    f.write(json.dumps({"ts_utc": now_ts, "ok": True, "alert": {"severity": "none"}}))
PY

# watcher with critical health: must block by health gate
TELEGRAM_ALLOWED_CHAT_IDS=1 \
TELEGRAM_LOCAL_REPLY_API_ENABLED=true \
TELEGRAM_LOCAL_REPLY_API_TOKEN=dummy \
YUUKI_WATCHER_ENABLED=true \
YUUKI_WATCHER_DRY_RUN=true \
YUUKI_WATCHER_REQUIRE_HEALTHY=true \
YUUKI_WATCHER_IDLE_SEC=30 \
YUUKI_WATCHER_COOLDOWN_SEC=3600 \
YUUKI_WATCHER_STATE_PATH="${WSTATE_BAD}" \
YUUKI_WATCHER_TASK_STATE_PATH="${TASKP}" \
TELEGRAM_JSON_LOG_PATH="${LOGP}" \
TELEGRAM_HEALTH_STATUS_PATH="${HEALTH_BAD}" \
TELEGRAM_STATUS_HEALTH_STALE_SEC=1800 \
"${WATCHER_COPY}" >/dev/null

# watcher with healthy state: health gate must pass and dry-run nudge should trigger
TELEGRAM_ALLOWED_CHAT_IDS=1 \
TELEGRAM_LOCAL_REPLY_API_ENABLED=true \
TELEGRAM_LOCAL_REPLY_API_TOKEN=dummy \
YUUKI_WATCHER_ENABLED=true \
YUUKI_WATCHER_DRY_RUN=true \
YUUKI_WATCHER_REQUIRE_HEALTHY=true \
YUUKI_WATCHER_IDLE_SEC=30 \
YUUKI_WATCHER_COOLDOWN_SEC=3600 \
YUUKI_WATCHER_STATE_PATH="${WSTATE_OK}" \
YUUKI_WATCHER_TASK_STATE_PATH="${TASKP}" \
TELEGRAM_JSON_LOG_PATH="${LOGP}" \
TELEGRAM_HEALTH_STATUS_PATH="${HEALTH_OK}" \
TELEGRAM_STATUS_HEALTH_STALE_SEC=1800 \
"${WATCHER_COPY}" >/dev/null

# self-improve with critical health: must block by health gate
YUUKI_SELF_IMPROVE_ENABLED=true \
YUUKI_SELF_IMPROVE_TARGET_PANE="no:such" \
YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY=true \
YUUKI_SELF_IMPROVE_STATE_PATH="${SSTATE_BAD}" \
YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC=0 \
TELEGRAM_HEALTH_STATUS_PATH="${HEALTH_BAD}" \
TELEGRAM_STATUS_HEALTH_STALE_SEC=1800 \
"${SELF_IMPROVE_COPY}" >/dev/null

# self-improve with healthy state: health gate must pass; next guard should be pane_missing
YUUKI_SELF_IMPROVE_ENABLED=true \
YUUKI_SELF_IMPROVE_TARGET_PANE="no:such" \
YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY=true \
YUUKI_SELF_IMPROVE_STATE_PATH="${SSTATE_OK}" \
YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC=0 \
TELEGRAM_HEALTH_STATUS_PATH="${HEALTH_OK}" \
TELEGRAM_STATUS_HEALTH_STALE_SEC=1800 \
"${SELF_IMPROVE_COPY}" >/dev/null

python3 - "${WSTATE_BAD}" "${WSTATE_OK}" "${SSTATE_BAD}" "${SSTATE_OK}" <<'PY'
import json
import sys
from pathlib import Path

wb, wo, sb, so = [Path(x) for x in sys.argv[1:5]]

def load(p: Path) -> dict:
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data

watch_bad = load(wb)
watch_ok = load(wo)
self_bad = load(sb)
self_ok = load(so)

assert watch_bad.get("last_reason") == "health_not_ok:critical", watch_bad
assert watch_bad.get("health_state") == "critical", watch_bad
assert watch_ok.get("last_action") == "nudge_dry_run", watch_ok
assert watch_ok.get("health_state") == "ok", watch_ok

assert self_bad.get("last_reason") == "health_not_ok:critical", self_bad
assert self_bad.get("health_state") == "critical", self_bad
assert str(self_ok.get("last_reason", "")).startswith("pane_missing:"), self_ok
assert self_ok.get("health_state") == "ok", self_ok

print("PASS: autonomy health-gate smoke checks")
PY


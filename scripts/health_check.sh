#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${TELEGRAM_HEALTH_ENV_FILE:-/home/foggen/kitan-telegram/.env}"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

SERVICE="kitan-telegram-bot.service"
REPLY_HOST="${TELEGRAM_LOCAL_REPLY_API_HOST:-127.0.0.1}"
REPLY_PORT="${TELEGRAM_LOCAL_REPLY_API_PORT:-8788}"
REPLY_URL="http://${REPLY_HOST}:${REPLY_PORT}/reply"
HEALTH_URL="http://${REPLY_HOST}:${REPLY_PORT}/health"
TOKEN="${TELEGRAM_LOCAL_REPLY_API_TOKEN:-}"
REPLY_AUTH_PROBE_ENABLED="${TELEGRAM_HEALTH_CHECK_REPLY_AUTH:-true}"
TARGET_PANE="${TELEGRAM_TMUX_TARGET_PANE:-}"
ALERT_CHAT_ID="${TELEGRAM_HEALTH_ALERT_CHAT_ID:-${TELEGRAM_ALLOWED_CHAT_IDS%%,*}}"
HEALTH_STATUS_PATH="${TELEGRAM_HEALTH_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/health_status.json}"
JSON_LOG_PATH="${TELEGRAM_JSON_LOG_PATH:-/home/foggen/kitan-telegram/logs/bot.jsonl}"
ALERT_STATE_PATH="${TELEGRAM_HEALTH_ALERT_STATE_PATH:-/home/foggen/kitan-telegram/runtime/health_alert_state.json}"
ALERT_COOLDOWN_SEC="${TELEGRAM_HEALTH_ALERT_COOLDOWN_SEC:-900}"
ALERT_COOLDOWN_WARNING_SEC="${TELEGRAM_HEALTH_ALERT_COOLDOWN_WARNING_SEC:-1800}"
ALERT_COOLDOWN_CRITICAL_SEC="${TELEGRAM_HEALTH_ALERT_COOLDOWN_CRITICAL_SEC:-${ALERT_COOLDOWN_SEC}}"
EVENT_LOOKBACK_MIN="${TELEGRAM_ALERT_EVENT_LOOKBACK_MIN:-15}"
EVENT_SCAN_MAX_BYTES="${TELEGRAM_ALERT_EVENT_SCAN_MAX_BYTES:-8388608}"
REPLY_FAILED_THRESHOLD="${TELEGRAM_ALERT_REPLY_FAILED_THRESHOLD:-3}"
RELAY_ERROR_THRESHOLD="${TELEGRAM_ALERT_RELAY_ERROR_THRESHOLD:-5}"
QUEUE_PRESSURE_THRESHOLD="${TELEGRAM_ALERT_QUEUE_PRESSURE_THRESHOLD:-${TELEGRAM_STATUS_QUEUE_PRESSURE_CRITICAL_THRESHOLD:-3}}"
SELF_TEST_STATUS_PATH="${YUUKI_SELF_TEST_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/self_test_latest.json}"
SELF_TEST_MAX_AGE_SEC="${TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC:-7200}"
SELF_TEST_FAIL_STREAK_THRESHOLD="${TELEGRAM_HEALTH_SELF_TEST_FAIL_STREAK_THRESHOLD:-3}"
SELF_TEST_AUTOREPAIR_ENABLED="${TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_ENABLED:-true}"
SELF_TEST_AUTOREPAIR_TIMEOUT_SEC="${TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_TIMEOUT_SEC:-180}"
SELF_TEST_AUTOREPAIR_SCRIPT="${TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_SCRIPT:-/home/foggen/kitan-telegram/scripts/self_test_reliability.sh}"
SELF_TEST_QUICK_STATUS_PATH="${YUUKI_SELF_TEST_QUICK_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/self_test_quick_latest.json}"
SELF_TEST_QUICK_MAX_AGE_SEC="${TELEGRAM_HEALTH_QUICK_SELF_TEST_MAX_AGE_SEC:-1800}"
SELF_TEST_REQUIRE_QUICK="${TELEGRAM_HEALTH_REQUIRE_QUICK_SELF_TEST:-false}"

fails=()

is_true() {
  local v="${1:-}"
  v="${v,,}"
  [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" || "$v" == "on" ]]
}

read_self_test_snapshot() {
  local status_path="$1"
  local max_age_sec="$2"
  python3 - "$status_path" "$max_age_sec" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
max_age_sec = max(1, int(sys.argv[2]))

exists = 0
ok = 0
stale = 1
age_sec = -1
ts_utc = "-"
failed_checks_csv = ""

if path.exists():
    exists = 1
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            ok = 1 if bool(obj.get("ok")) else 0
            failed_checks: list[str] = []
            checks = obj.get("checks")
            if isinstance(checks, list):
                for item in checks:
                    if not isinstance(item, dict):
                        continue
                    if item.get("ok") is False:
                        nm = item.get("name")
                        if isinstance(nm, str) and nm.strip():
                            failed_checks.append(nm.strip())
            if not failed_checks:
                raw_failures = obj.get("failures")
                if isinstance(raw_failures, list):
                    for item in raw_failures:
                        nm = str(item).strip()
                        if nm:
                            failed_checks.append(nm)
            # normalize to stable tokens safe for shell/log signatures
            norm: list[str] = []
            seen = set()
            for name in failed_checks:
                s = str(name).strip().replace(" ", "_")
                s = "".join(ch for ch in s if ch.isalnum() or ch in {"_", "-", ".", ":"})
                if s and s not in seen:
                    seen.add(s)
                    norm.append(s)
            failed_checks_csv = ",".join(norm)
            raw_ts = obj.get("ts_utc")
            if isinstance(raw_ts, str) and raw_ts:
                ts_utc = raw_ts
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    age_sec = int((now - dt).total_seconds())
                    if age_sec < 0:
                        age_sec = 0
                    stale = 1 if age_sec > max_age_sec else 0
                except Exception:
                    stale = 1
    except Exception:
        ok = 0
        stale = 1

print(f"{exists}\t{ok}\t{stale}\t{age_sec}\t{ts_utc}\t{failed_checks_csv}")
PY
}

if ! systemctl is-active --quiet "$SERVICE"; then
  fails+=("service:$SERVICE=down")
fi

if [[ -n "$TARGET_PANE" ]]; then
  session="${TARGET_PANE%%:*}"
  if ! tmux has-session -t "$session" 2>/dev/null; then
    fails+=("tmux_session:$session=missing")
  fi
fi

if ! curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null; then
  fails+=("reply_api=down")
fi

REPLY_AUTH_PROBE_ENABLED_BOOL=0
REPLY_AUTH_PROBE_OK=0
REPLY_AUTH_PROBE_HTTP="-"
if is_true "$REPLY_AUTH_PROBE_ENABLED"; then
  REPLY_AUTH_PROBE_ENABLED_BOOL=1
  auth_headers=()
  if [[ -n "$TOKEN" ]]; then
    auth_headers+=(-H "Authorization: Bearer ${TOKEN}")
  fi
  set +e
  REPLY_AUTH_PROBE_HTTP=$(
    curl -sS -o /dev/null -w "%{http_code}" --max-time 2 -X POST "$REPLY_URL" \
      -H "Content-Type: application/json" \
      "${auth_headers[@]}" \
      -d '{}'
  )
  _reply_auth_probe_rc=$?
  set -e
  if [[ "${_reply_auth_probe_rc}" -ne 0 ]]; then
    fails+=("reply_api_auth_probe=down")
  elif [[ "${REPLY_AUTH_PROBE_HTTP}" != "400" ]]; then
    fails+=("reply_api_auth_probe=http_${REPLY_AUTH_PROBE_HTTP}")
  else
    REPLY_AUTH_PROBE_OK=1
  fi
fi

read -r EVENT_REPLY_FAILED EVENT_RELAY_ERROR EVENT_REPLY_QUEUE_FULL EVENT_REPLY_QUEUE_DROP_OLDEST < <(
python3 - "$JSON_LOG_PATH" "$EVENT_LOOKBACK_MIN" "$EVENT_SCAN_MAX_BYTES" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
lookback_min = int(sys.argv[2])
scan_max_bytes = max(64000, int(sys.argv[3]))
reply_failed = 0
relay_error = 0
reply_queue_full = 0
reply_queue_drop_oldest = 0

def read_tail_lines(p: Path, max_bytes: int):
    if not p.exists():
        return []
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size <= 0:
            return []
        read_size = min(max_bytes, size)
        f.seek(-read_size, 2)
        data = f.read(read_size)
    if read_size < size:
        cut = data.find(b"\n")
        if cut >= 0:
            data = data[cut + 1 :]
    return data.decode("utf-8", errors="ignore").splitlines()

if path.exists():
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - lookback_min * 60
    for line in read_tail_lines(path, scan_max_bytes):
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        et = obj.get("event_type")
        if et not in {"reply_failed", "relay_error", "reply_queue_full", "reply_queue_drop_oldest"}:
            continue
        ts = obj.get("ts_utc")
        if not isinstance(ts, str) or not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.timestamp() < cutoff:
            continue
        if et == "reply_failed":
            reply_failed += 1
        elif et == "relay_error":
            relay_error += 1
        elif et == "reply_queue_full":
            reply_queue_full += 1
        elif et == "reply_queue_drop_oldest":
            reply_queue_drop_oldest += 1

print(f"{reply_failed} {relay_error} {reply_queue_full} {reply_queue_drop_oldest}")
PY
)

if [[ "${EVENT_REPLY_FAILED:-0}" -ge "${REPLY_FAILED_THRESHOLD}" ]]; then
  fails+=("event:reply_failed=${EVENT_REPLY_FAILED}>=${REPLY_FAILED_THRESHOLD}")
fi

if [[ "${EVENT_RELAY_ERROR:-0}" -ge "${RELAY_ERROR_THRESHOLD}" ]]; then
  fails+=("event:relay_error=${EVENT_RELAY_ERROR}>=${RELAY_ERROR_THRESHOLD}")
fi

EVENT_QUEUE_PRESSURE=$(( ${EVENT_REPLY_QUEUE_FULL:-0} + ${EVENT_REPLY_QUEUE_DROP_OLDEST:-0} ))
if [[ "${EVENT_QUEUE_PRESSURE}" -ge "${QUEUE_PRESSURE_THRESHOLD}" ]]; then
  fails+=("event:queue_pressure=${EVENT_QUEUE_PRESSURE}>=${QUEUE_PRESSURE_THRESHOLD}")
fi

read -r SELF_TEST_EXISTS SELF_TEST_OK SELF_TEST_STALE SELF_TEST_AGE_SEC SELF_TEST_TS_UTC SELF_TEST_FAILED_CHECKS_CSV < <(
  read_self_test_snapshot "$SELF_TEST_STATUS_PATH" "$SELF_TEST_MAX_AGE_SEC"
)
read -r SELF_TEST_QUICK_EXISTS SELF_TEST_QUICK_OK SELF_TEST_QUICK_STALE SELF_TEST_QUICK_AGE_SEC SELF_TEST_QUICK_TS_UTC SELF_TEST_QUICK_FAILED_CHECKS_CSV < <(
  read_self_test_snapshot "$SELF_TEST_QUICK_STATUS_PATH" "$SELF_TEST_QUICK_MAX_AGE_SEC"
)

SELF_TEST_AUTOREPAIR_ENABLED_BOOL=0
SELF_TEST_AUTOREPAIR_ATTEMPTED=0
SELF_TEST_AUTOREPAIR_RESULT="disabled"
if is_true "$SELF_TEST_AUTOREPAIR_ENABLED"; then
  SELF_TEST_AUTOREPAIR_ENABLED_BOOL=1
  SELF_TEST_AUTOREPAIR_RESULT="not_needed"
  if [[ "${SELF_TEST_EXISTS:-0}" != "1" || "${SELF_TEST_OK:-0}" != "1" || "${SELF_TEST_STALE:-1}" == "1" ]]; then
    if [[ ! -x "$SELF_TEST_AUTOREPAIR_SCRIPT" ]]; then
      SELF_TEST_AUTOREPAIR_RESULT="script_missing_or_not_executable"
    else
      SELF_TEST_AUTOREPAIR_ATTEMPTED=1
      set +e
      timeout "${SELF_TEST_AUTOREPAIR_TIMEOUT_SEC}" "$SELF_TEST_AUTOREPAIR_SCRIPT" >/dev/null 2>&1
      _self_test_autorepair_rc=$?
      set -e
      read -r SELF_TEST_EXISTS SELF_TEST_OK SELF_TEST_STALE SELF_TEST_AGE_SEC SELF_TEST_TS_UTC SELF_TEST_FAILED_CHECKS_CSV < <(
        read_self_test_snapshot "$SELF_TEST_STATUS_PATH" "$SELF_TEST_MAX_AGE_SEC"
      )
      if [[ "${SELF_TEST_EXISTS:-0}" == "1" && "${SELF_TEST_OK:-0}" == "1" && "${SELF_TEST_STALE:-1}" != "1" ]]; then
        SELF_TEST_AUTOREPAIR_RESULT="recovered"
      elif [[ "${_self_test_autorepair_rc}" == "124" ]]; then
        SELF_TEST_AUTOREPAIR_RESULT="timeout"
      elif [[ "${_self_test_autorepair_rc}" == "0" ]]; then
        SELF_TEST_AUTOREPAIR_RESULT="attempt_no_recovery"
      else
        SELF_TEST_AUTOREPAIR_RESULT="attempt_failed"
      fi
    fi
  fi
fi

if [[ "${SELF_TEST_EXISTS:-0}" != "1" ]]; then
  fails+=("self_test:missing")
elif [[ "${SELF_TEST_OK:-0}" != "1" ]]; then
  fails+=("self_test:failed")
elif [[ "${SELF_TEST_STALE:-1}" == "1" ]]; then
  fails+=("self_test:stale=${SELF_TEST_AGE_SEC}s>${SELF_TEST_MAX_AGE_SEC}s")
fi

if is_true "$SELF_TEST_REQUIRE_QUICK"; then
  if [[ "${SELF_TEST_QUICK_EXISTS:-0}" != "1" ]]; then
    fails+=("self_test_quick:missing")
  elif [[ "${SELF_TEST_QUICK_OK:-0}" != "1" ]]; then
    fails+=("self_test_quick:failed")
  elif [[ "${SELF_TEST_QUICK_STALE:-1}" == "1" ]]; then
    fails+=("self_test_quick:stale=${SELF_TEST_QUICK_AGE_SEC}s>${SELF_TEST_QUICK_MAX_AGE_SEC}s")
  fi
fi

NOW_EPOCH="$(date +%s)"
SELF_TEST_REPEATED_CHECKS_CSV=""
read -r SELF_TEST_REPEATED_CHECKS_CSV < <(
python3 - "$ALERT_STATE_PATH" "${SELF_TEST_FAILED_CHECKS_CSV:-}" "$SELF_TEST_FAIL_STREAK_THRESHOLD" "$NOW_EPOCH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
failed_csv = (sys.argv[2] or "").strip()
threshold = max(1, int(sys.argv[3]))
now_epoch = int(sys.argv[4])

def parse_names(raw: str) -> list[str]:
    out = []
    seen = set()
    if not raw or raw == "-":
        return out
    for part in raw.split(","):
        s = part.strip()
        s = "".join(ch for ch in s if ch.isalnum() or ch in {"_", "-", ".", ":"})
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out

failed_names = parse_names(failed_csv)
state = {}
if path.exists():
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    except Exception:
        state = {}

prev_streaks = state.get("self_test_check_streaks")
if not isinstance(prev_streaks, dict):
    prev_streaks = {}

new_streaks = {}
for name in failed_names:
    prev = prev_streaks.get(name)
    try:
        prev_i = int(prev)
    except Exception:
        prev_i = 0
    if prev_i < 0:
        prev_i = 0
    new_streaks[name] = prev_i + 1

repeated = [name for name, streak in new_streaks.items() if int(streak) >= threshold]
state["self_test_check_streaks"] = new_streaks
state["self_test_last_failed_checks"] = failed_names
state["self_test_repeated_checks"] = repeated
state["self_test_fail_streak_threshold"] = threshold
state["updated_epoch"] = now_epoch
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

repeated_csv = ",".join(repeated)
print(repeated_csv if repeated_csv else "-")
PY
)

if [[ -n "${SELF_TEST_REPEATED_CHECKS_CSV:-}" && "${SELF_TEST_REPEATED_CHECKS_CSV}" != "-" ]]; then
  IFS=',' read -r -a _self_test_repeated <<< "${SELF_TEST_REPEATED_CHECKS_CSV}"
  for _check in "${_self_test_repeated[@]}"; do
    [[ -n "${_check}" ]] && fails+=("self_test:repeated_check:${_check}")
  done
fi

ALERT_SEVERITY="none"
ALERT_COOLDOWN_EFFECTIVE_SEC=0
if [[ ${#fails[@]} -gt 0 ]]; then
  ALERT_SEVERITY="warning"
  for fail in "${fails[@]}"; do
    if [[ "$fail" != self_test:stale=* && "$fail" != self_test_quick:stale=* ]]; then
      ALERT_SEVERITY="critical"
      break
    fi
  done
  if [[ "$ALERT_SEVERITY" == "warning" ]]; then
    ALERT_COOLDOWN_EFFECTIVE_SEC="${ALERT_COOLDOWN_WARNING_SEC}"
  else
    ALERT_COOLDOWN_EFFECTIVE_SEC="${ALERT_COOLDOWN_CRITICAL_SEC}"
  fi
fi

mkdir -p "$(dirname "$HEALTH_STATUS_PATH")"
TS_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 - "$HEALTH_STATUS_PATH" "$TS_UTC" "${#fails[@]}" "${fails[*]:-}" "${EVENT_REPLY_FAILED:-0}" "${EVENT_RELAY_ERROR:-0}" "$EVENT_LOOKBACK_MIN" "$SELF_TEST_STATUS_PATH" "$SELF_TEST_MAX_AGE_SEC" "${SELF_TEST_EXISTS:-0}" "${SELF_TEST_OK:-0}" "${SELF_TEST_STALE:-1}" "${SELF_TEST_AGE_SEC:--1}" "${SELF_TEST_TS_UTC:--}" "$ALERT_SEVERITY" "${ALERT_COOLDOWN_EFFECTIVE_SEC}" "${SELF_TEST_FAILED_CHECKS_CSV:-}" "${SELF_TEST_REPEATED_CHECKS_CSV:-}" "${SELF_TEST_FAIL_STREAK_THRESHOLD}" "${SELF_TEST_AUTOREPAIR_ENABLED_BOOL}" "${SELF_TEST_AUTOREPAIR_ATTEMPTED}" "${SELF_TEST_AUTOREPAIR_RESULT}" "${SELF_TEST_AUTOREPAIR_TIMEOUT_SEC}" "${SELF_TEST_AUTOREPAIR_SCRIPT}" "${EVENT_REPLY_QUEUE_FULL:-0}" "${EVENT_REPLY_QUEUE_DROP_OLDEST:-0}" "${QUEUE_PRESSURE_THRESHOLD}" "${EVENT_QUEUE_PRESSURE:-0}" "${SELF_TEST_REQUIRE_QUICK}" "${SELF_TEST_QUICK_STATUS_PATH}" "${SELF_TEST_QUICK_MAX_AGE_SEC}" "${SELF_TEST_QUICK_EXISTS:-0}" "${SELF_TEST_QUICK_OK:-0}" "${SELF_TEST_QUICK_STALE:-1}" "${SELF_TEST_QUICK_AGE_SEC:--1}" "${SELF_TEST_QUICK_TS_UTC:--}" "${SELF_TEST_QUICK_FAILED_CHECKS_CSV:-}" "${REPLY_AUTH_PROBE_ENABLED_BOOL:-0}" "${REPLY_AUTH_PROBE_OK:-0}" "${REPLY_AUTH_PROBE_HTTP:--}" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
ts_utc = sys.argv[2]
fail_count = int(sys.argv[3])
raw = sys.argv[4].strip() if len(sys.argv) > 4 else ""
event_reply_failed = int(sys.argv[5]) if len(sys.argv) > 5 else 0
event_relay_error = int(sys.argv[6]) if len(sys.argv) > 6 else 0
event_lookback_min = int(sys.argv[7]) if len(sys.argv) > 7 else 15
self_test_path = sys.argv[8] if len(sys.argv) > 8 else ""
self_test_max_age_sec = int(sys.argv[9]) if len(sys.argv) > 9 else 7200
self_test_exists = bool(int(sys.argv[10])) if len(sys.argv) > 10 else False
self_test_ok = bool(int(sys.argv[11])) if len(sys.argv) > 11 else False
self_test_stale = bool(int(sys.argv[12])) if len(sys.argv) > 12 else True
self_test_age_sec = int(sys.argv[13]) if len(sys.argv) > 13 else -1
self_test_ts_utc = sys.argv[14] if len(sys.argv) > 14 else "-"
alert_severity = sys.argv[15] if len(sys.argv) > 15 else "none"
alert_cooldown_sec = int(sys.argv[16]) if len(sys.argv) > 16 else 0
self_test_failed_checks_raw = sys.argv[17] if len(sys.argv) > 17 else ""
self_test_repeated_checks_raw = sys.argv[18] if len(sys.argv) > 18 else ""
self_test_fail_streak_threshold = int(sys.argv[19]) if len(sys.argv) > 19 else 3
autorepair_enabled = bool(int(sys.argv[20])) if len(sys.argv) > 20 else False
autorepair_attempted = bool(int(sys.argv[21])) if len(sys.argv) > 21 else False
autorepair_result = sys.argv[22] if len(sys.argv) > 22 else "disabled"
autorepair_timeout_sec = int(sys.argv[23]) if len(sys.argv) > 23 else 180
autorepair_script = sys.argv[24] if len(sys.argv) > 24 else ""
event_reply_queue_full = int(sys.argv[25]) if len(sys.argv) > 25 else 0
event_reply_queue_drop_oldest = int(sys.argv[26]) if len(sys.argv) > 26 else 0
event_queue_pressure_threshold = int(sys.argv[27]) if len(sys.argv) > 27 else 3
event_queue_pressure = int(sys.argv[28]) if len(sys.argv) > 28 else 0
require_quick_raw = str(sys.argv[29]).strip().lower() if len(sys.argv) > 29 else "false"
self_test_quick_required = require_quick_raw in {"1", "true", "yes", "on"}
self_test_quick_path = sys.argv[30] if len(sys.argv) > 30 else ""
self_test_quick_max_age_sec = int(sys.argv[31]) if len(sys.argv) > 31 else 1800
self_test_quick_exists = bool(int(sys.argv[32])) if len(sys.argv) > 32 else False
self_test_quick_ok = bool(int(sys.argv[33])) if len(sys.argv) > 33 else False
self_test_quick_stale = bool(int(sys.argv[34])) if len(sys.argv) > 34 else True
self_test_quick_age_sec = int(sys.argv[35]) if len(sys.argv) > 35 else -1
self_test_quick_ts_utc = sys.argv[36] if len(sys.argv) > 36 else "-"
self_test_quick_failed_checks_raw = sys.argv[37] if len(sys.argv) > 37 else ""
reply_auth_probe_enabled = bool(int(sys.argv[38])) if len(sys.argv) > 38 else False
reply_auth_probe_ok = bool(int(sys.argv[39])) if len(sys.argv) > 39 else False
reply_auth_probe_http = sys.argv[40] if len(sys.argv) > 40 else "-"
fails = [x for x in raw.split(" ") if x]
self_test_failed_checks = [x for x in self_test_failed_checks_raw.split(",") if x and x != "-"]
self_test_repeated_checks = [x for x in self_test_repeated_checks_raw.split(",") if x and x != "-"]
self_test_quick_failed_checks = [x for x in self_test_quick_failed_checks_raw.split(",") if x and x != "-"]

payload = {
    "ts_utc": ts_utc,
    "ok": fail_count == 0,
    "fail_count": fail_count,
    "fails": fails,
    "events": {
        "lookback_min": event_lookback_min,
        "reply_failed": event_reply_failed,
        "relay_error": event_relay_error,
        "reply_queue_full": event_reply_queue_full,
        "reply_queue_drop_oldest": event_reply_queue_drop_oldest,
        "queue_pressure": event_queue_pressure,
        "queue_pressure_threshold": event_queue_pressure_threshold,
    },
    "reply_api": {
        "auth_probe_enabled": reply_auth_probe_enabled,
        "auth_probe_ok": reply_auth_probe_ok,
        "auth_probe_http": reply_auth_probe_http,
    },
    "self_test": {
        "path": self_test_path,
        "max_age_sec": self_test_max_age_sec,
        "exists": self_test_exists,
        "ok": self_test_ok,
        "stale": self_test_stale,
        "age_sec": self_test_age_sec,
        "ts_utc": self_test_ts_utc,
        "failed_checks": self_test_failed_checks,
        "repeated_fail_checks": self_test_repeated_checks,
        "fail_streak_threshold": self_test_fail_streak_threshold,
        "autorepair_enabled": autorepair_enabled,
        "autorepair_attempted": autorepair_attempted,
        "autorepair_result": autorepair_result,
        "autorepair_timeout_sec": autorepair_timeout_sec,
        "autorepair_script": autorepair_script,
    },
    "self_test_quick": {
        "required": self_test_quick_required,
        "path": self_test_quick_path,
        "max_age_sec": self_test_quick_max_age_sec,
        "exists": self_test_quick_exists,
        "ok": self_test_quick_ok,
        "stale": self_test_quick_stale,
        "age_sec": self_test_quick_age_sec,
        "ts_utc": self_test_quick_ts_utc,
        "failed_checks": self_test_quick_failed_checks,
    },
    "alert": {
        "severity": alert_severity,
        "cooldown_sec_effective": alert_cooldown_sec,
    },
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

if [[ ${#fails[@]} -eq 0 ]]; then
  mkdir -p "$(dirname "$ALERT_STATE_PATH")"
  read -r SHOULD_SEND_RECOVERY PREV_SEVERITY PREV_SIG < <(
python3 - "$ALERT_STATE_PATH" "$NOW_EPOCH" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
now_epoch = int(sys.argv[2])
prev = {}
if path.exists():
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        prev = {}

was_in_alert = bool(prev.get("in_alert", False))
prev_severity = str(prev.get("last_severity") or "unknown")
prev_sig = str(prev.get("last_fingerprint") or "")

state = {
    "in_alert": False,
    "last_alert_epoch": None,
    "last_fingerprint": "",
    "last_severity": "none",
    "self_test_check_streaks": {},
    "self_test_last_failed_checks": [],
    "self_test_repeated_checks": [],
    "last_recovery_epoch": now_epoch if was_in_alert else prev.get("last_recovery_epoch"),
    "updated_epoch": now_epoch,
}
path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{1 if was_in_alert else 0}\t{prev_severity}\t{prev_sig}")
PY
)
  if [[ "${SHOULD_SEND_RECOVERY:-0}" == "1" && -n "$TOKEN" ]]; then
    recovery_text=$'HEALTH_RECOVERY\n'"previous_severity=${PREV_SEVERITY:-unknown}"$'\n'"previous_fail_sig=${PREV_SIG:-none}"
    if [[ -n "$ALERT_CHAT_ID" ]]; then
      recovery_payload=$(python3 - "$ALERT_CHAT_ID" "$recovery_text" <<'PY'
import json, sys
chat_id = int(sys.argv[1])
text = sys.argv[2]
print(json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False))
PY
)
    else
      recovery_payload=$(python3 - "$recovery_text" <<'PY'
import json, sys
text = sys.argv[1]
print(json.dumps({"text": text}, ensure_ascii=False))
PY
)
    fi
    curl -fsS --max-time 3 -X POST "$REPLY_URL" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$recovery_payload" >/dev/null || true
  fi
  exit 0
fi

mkdir -p "$(dirname "$ALERT_STATE_PATH")"
FAIL_SIG="$(printf '%s|' "${fails[@]}")"
SHOULD_SEND=$(
python3 - "$ALERT_STATE_PATH" "$NOW_EPOCH" "$ALERT_COOLDOWN_EFFECTIVE_SEC" "$FAIL_SIG" "$ALERT_SEVERITY" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
now_epoch = int(sys.argv[2])
cooldown = int(sys.argv[3])
sig = sys.argv[4]
severity = sys.argv[5] if len(sys.argv) > 5 else "critical"

prev = {}
if path.exists():
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        prev = {}

in_alert = bool(prev.get("in_alert", False))
last_epoch = int(prev.get("last_alert_epoch") or 0)
last_sig = str(prev.get("last_fingerprint") or "")
last_severity = str(prev.get("last_severity") or "")
self_test_check_streaks = prev.get("self_test_check_streaks")
if not isinstance(self_test_check_streaks, dict):
    self_test_check_streaks = {}
self_test_last_failed_checks = prev.get("self_test_last_failed_checks")
if not isinstance(self_test_last_failed_checks, list):
    self_test_last_failed_checks = []
self_test_repeated_checks = prev.get("self_test_repeated_checks")
if not isinstance(self_test_repeated_checks, list):
    self_test_repeated_checks = []
self_test_fail_streak_threshold = int(prev.get("self_test_fail_streak_threshold") or 3)

should_send = 1
if in_alert and last_sig == sig and last_severity == severity and (now_epoch - last_epoch) < cooldown:
    should_send = 0

state = {
    "in_alert": True,
    "last_alert_epoch": last_epoch if should_send == 0 else now_epoch,
    "last_fingerprint": sig,
    "last_severity": severity,
    "self_test_check_streaks": self_test_check_streaks,
    "self_test_last_failed_checks": self_test_last_failed_checks,
    "self_test_repeated_checks": self_test_repeated_checks,
    "self_test_fail_streak_threshold": self_test_fail_streak_threshold,
    "updated_epoch": now_epoch,
    "cooldown_sec": cooldown,
}
path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print(should_send)
PY
)

if [[ "$SHOULD_SEND" != "1" ]]; then
  exit 1
fi

text=$'HEALTH_ALERT\n'"severity=${ALERT_SEVERITY}"$'\n'"$(printf '%s\n' "${fails[@]}")"

if [[ -n "$ALERT_CHAT_ID" ]]; then
  json_payload=$(python3 - "$ALERT_CHAT_ID" "$text" <<'PY'
import json, sys
chat_id = int(sys.argv[1])
text = sys.argv[2]
print(json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False))
PY
)
else
  json_payload=$(python3 - "$text" <<'PY'
import json, sys
text = sys.argv[1]
print(json.dumps({"text": text}, ensure_ascii=False))
PY
)
fi

if [[ -n "$TOKEN" ]]; then
  curl -fsS --max-time 3 -X POST "$REPLY_URL" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$json_payload" >/dev/null || true
fi

exit 1

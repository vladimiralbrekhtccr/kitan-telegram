#!/usr/bin/env bash
set -euo pipefail

# Keep external env overrides higher priority than .env defaults.
IN_ENABLED="${YUUKI_SELF_IMPROVE_ENABLED-}"
IN_TARGET_PANE="${YUUKI_SELF_IMPROVE_TARGET_PANE-}"
IN_PREFIX="${YUUKI_SELF_IMPROVE_PREFIX-}"
IN_MESSAGE="${YUUKI_SELF_IMPROVE_MESSAGE-}"
IN_STATE_PATH="${YUUKI_SELF_IMPROVE_STATE_PATH-}"
IN_MIN_INTERVAL_SEC="${YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC-}"
IN_REQUIRE_HEALTHY="${YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY-}"
IN_HEALTH_STATUS_PATH="${TELEGRAM_HEALTH_STATUS_PATH-}"
IN_HEALTH_STALE_SEC="${TELEGRAM_STATUS_HEALTH_STALE_SEC-}"
IN_NOTIFY_TELEGRAM="${YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM-}"
IN_NOTIFY_CHAT_ID="${YUUKI_SELF_IMPROVE_NOTIFY_CHAT_ID-}"
IN_NOTIFY_ON_SKIP="${YUUKI_SELF_IMPROVE_NOTIFY_ON_SKIP-}"
IN_REPLY_HOST="${TELEGRAM_LOCAL_REPLY_API_HOST-}"
IN_REPLY_PORT="${TELEGRAM_LOCAL_REPLY_API_PORT-}"
IN_REPLY_TOKEN="${TELEGRAM_LOCAL_REPLY_API_TOKEN-}"
IN_AUTO_PLAN="${YUUKI_SELF_IMPROVE_AUTO_PLAN-}"
IN_PLAN_SCRIPT="${YUUKI_SELF_IMPROVE_PLAN_SCRIPT-}"
IN_PLAN_OUTPUT_PATH="${YUUKI_SELF_IMPROVE_PLAN_OUTPUT_PATH-}"
IN_PLAN_TIMEOUT_SEC="${YUUKI_SELF_IMPROVE_PLAN_TIMEOUT_SEC-}"
IN_SELF_TEST_STATUS_PATH="${YUUKI_SELF_TEST_STATUS_PATH-}"
IN_SELF_TEST_QUICK_STATUS_PATH="${YUUKI_SELF_TEST_QUICK_STATUS_PATH-}"

ENV_FILE="/home/foggen/kitan-telegram/.env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

if [[ -n "${IN_ENABLED}" ]]; then YUUKI_SELF_IMPROVE_ENABLED="${IN_ENABLED}"; fi
if [[ -n "${IN_TARGET_PANE}" ]]; then YUUKI_SELF_IMPROVE_TARGET_PANE="${IN_TARGET_PANE}"; fi
if [[ -n "${IN_PREFIX}" ]]; then YUUKI_SELF_IMPROVE_PREFIX="${IN_PREFIX}"; fi
if [[ -n "${IN_MESSAGE}" ]]; then YUUKI_SELF_IMPROVE_MESSAGE="${IN_MESSAGE}"; fi
if [[ -n "${IN_STATE_PATH}" ]]; then YUUKI_SELF_IMPROVE_STATE_PATH="${IN_STATE_PATH}"; fi
if [[ -n "${IN_MIN_INTERVAL_SEC}" ]]; then YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC="${IN_MIN_INTERVAL_SEC}"; fi
if [[ -n "${IN_REQUIRE_HEALTHY}" ]]; then YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY="${IN_REQUIRE_HEALTHY}"; fi
if [[ -n "${IN_HEALTH_STATUS_PATH}" ]]; then TELEGRAM_HEALTH_STATUS_PATH="${IN_HEALTH_STATUS_PATH}"; fi
if [[ -n "${IN_HEALTH_STALE_SEC}" ]]; then TELEGRAM_STATUS_HEALTH_STALE_SEC="${IN_HEALTH_STALE_SEC}"; fi
if [[ -n "${IN_NOTIFY_TELEGRAM}" ]]; then YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM="${IN_NOTIFY_TELEGRAM}"; fi
if [[ -n "${IN_NOTIFY_CHAT_ID}" ]]; then YUUKI_SELF_IMPROVE_NOTIFY_CHAT_ID="${IN_NOTIFY_CHAT_ID}"; fi
if [[ -n "${IN_NOTIFY_ON_SKIP}" ]]; then YUUKI_SELF_IMPROVE_NOTIFY_ON_SKIP="${IN_NOTIFY_ON_SKIP}"; fi
if [[ -n "${IN_REPLY_HOST}" ]]; then TELEGRAM_LOCAL_REPLY_API_HOST="${IN_REPLY_HOST}"; fi
if [[ -n "${IN_REPLY_PORT}" ]]; then TELEGRAM_LOCAL_REPLY_API_PORT="${IN_REPLY_PORT}"; fi
if [[ -n "${IN_REPLY_TOKEN}" ]]; then TELEGRAM_LOCAL_REPLY_API_TOKEN="${IN_REPLY_TOKEN}"; fi
if [[ -n "${IN_AUTO_PLAN}" ]]; then YUUKI_SELF_IMPROVE_AUTO_PLAN="${IN_AUTO_PLAN}"; fi
if [[ -n "${IN_PLAN_SCRIPT}" ]]; then YUUKI_SELF_IMPROVE_PLAN_SCRIPT="${IN_PLAN_SCRIPT}"; fi
if [[ -n "${IN_PLAN_OUTPUT_PATH}" ]]; then YUUKI_SELF_IMPROVE_PLAN_OUTPUT_PATH="${IN_PLAN_OUTPUT_PATH}"; fi
if [[ -n "${IN_PLAN_TIMEOUT_SEC}" ]]; then YUUKI_SELF_IMPROVE_PLAN_TIMEOUT_SEC="${IN_PLAN_TIMEOUT_SEC}"; fi
if [[ -n "${IN_SELF_TEST_STATUS_PATH}" ]]; then YUUKI_SELF_TEST_STATUS_PATH="${IN_SELF_TEST_STATUS_PATH}"; fi
if [[ -n "${IN_SELF_TEST_QUICK_STATUS_PATH}" ]]; then YUUKI_SELF_TEST_QUICK_STATUS_PATH="${IN_SELF_TEST_QUICK_STATUS_PATH}"; fi

TARGET_PANE="${YUUKI_SELF_IMPROVE_TARGET_PANE:-${TELEGRAM_TMUX_TARGET_PANE:-}}"
PREFIX="${YUUKI_SELF_IMPROVE_PREFIX:-[telegram]}"
MESSAGE="${YUUKI_SELF_IMPROVE_MESSAGE:-Продолжай автономный импрувемент: выбери самый полезный следующий шаг, выполни, проверь, и отправь короткий отчёт в Telegram.}"
ENABLED="${YUUKI_SELF_IMPROVE_ENABLED:-false}"
STATE_PATH="${YUUKI_SELF_IMPROVE_STATE_PATH:-/home/foggen/kitan-telegram/runtime/self_improve_nudge_state.json}"
MIN_INTERVAL_SEC="${YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC:-540}"
REQUIRE_HEALTHY="${YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY:-true}"
HEALTH_STATUS_PATH="${TELEGRAM_HEALTH_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/health_status.json}"
HEALTH_STALE_SEC="${TELEGRAM_STATUS_HEALTH_STALE_SEC:-1800}"
NOTIFY_TELEGRAM="${YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM:-false}"
NOTIFY_CHAT_ID="${YUUKI_SELF_IMPROVE_NOTIFY_CHAT_ID:-${TELEGRAM_HEALTH_ALERT_CHAT_ID:-}}"
NOTIFY_ON_SKIP="${YUUKI_SELF_IMPROVE_NOTIFY_ON_SKIP:-false}"
REPLY_HOST="${TELEGRAM_LOCAL_REPLY_API_HOST:-127.0.0.1}"
REPLY_PORT="${TELEGRAM_LOCAL_REPLY_API_PORT:-8788}"
REPLY_TOKEN="${TELEGRAM_LOCAL_REPLY_API_TOKEN:-}"
AUTO_PLAN="${YUUKI_SELF_IMPROVE_AUTO_PLAN:-false}"
PLAN_SCRIPT="${YUUKI_SELF_IMPROVE_PLAN_SCRIPT:-/home/foggen/kitan-telegram/scripts/self_improve_plan.py}"
PLAN_OUTPUT_PATH="${YUUKI_SELF_IMPROVE_PLAN_OUTPUT_PATH:-/home/foggen/kitan-telegram/runtime/self_improve_plan_latest.json}"
PLAN_TIMEOUT_SEC="${YUUKI_SELF_IMPROVE_PLAN_TIMEOUT_SEC:-8}"
SELF_TEST_STATUS_PATH="${YUUKI_SELF_TEST_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/self_test_latest.json}"
SELF_TEST_QUICK_STATUS_PATH="${YUUKI_SELF_TEST_QUICK_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/self_test_quick_latest.json}"

python3 - "$ENABLED" "$TARGET_PANE" "$PREFIX" "$MESSAGE" "$STATE_PATH" "$MIN_INTERVAL_SEC" "$REQUIRE_HEALTHY" "$HEALTH_STATUS_PATH" "$HEALTH_STALE_SEC" "$NOTIFY_TELEGRAM" "$NOTIFY_CHAT_ID" "$NOTIFY_ON_SKIP" "$REPLY_HOST" "$REPLY_PORT" "$REPLY_TOKEN" "$AUTO_PLAN" "$PLAN_SCRIPT" "$PLAN_OUTPUT_PATH" "$PLAN_TIMEOUT_SEC" "$SELF_TEST_STATUS_PATH" "$SELF_TEST_QUICK_STATUS_PATH" <<'PY'
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.request


(
    enabled_raw,
    target,
    prefix,
    message,
    state_path_raw,
    min_interval_raw,
    require_healthy_raw,
    health_status_raw,
    health_stale_raw,
    notify_enabled_raw,
    notify_chat_id_raw,
    notify_on_skip_raw,
    reply_host_raw,
    reply_port_raw,
    reply_token_raw,
    auto_plan_raw,
    plan_script_raw,
    plan_output_path_raw,
    plan_timeout_raw,
    self_test_status_path_raw,
    self_test_quick_status_path_raw,
) = sys.argv[1:22]


def to_bool(raw: str, default: bool) -> bool:
    val = str(raw).strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


enabled = to_bool(enabled_raw, False)
require_healthy = to_bool(require_healthy_raw, True)
state_path = Path(state_path_raw).expanduser()
health_status_path = Path(health_status_raw).expanduser()
now = datetime.now(timezone.utc)
now_epoch = int(time.time())
try:
    min_interval_sec = max(0, int(min_interval_raw))
except Exception:
    min_interval_sec = 540
try:
    health_stale_threshold_sec = max(60, int(health_stale_raw))
except Exception:
    health_stale_threshold_sec = 1800

action = "noop"
reason = "ok"
sent_line = ""
submit_key = ""
task_running = False
last_sent_epoch = 0
plan_used = False
plan_reason = "disabled"
plan_task_id = ""
plan_title = ""
health_state = "unknown"
health_ts_utc = "(unknown)"
health_age_sec = None
health_stale = None
notify_sent = False
notify_reason = "disabled"

notify_enabled = to_bool(notify_enabled_raw, False)
notify_on_skip = to_bool(notify_on_skip_raw, False)
auto_plan_enabled = to_bool(auto_plan_raw, False)
reply_host = (reply_host_raw or "127.0.0.1").strip() or "127.0.0.1"
try:
    reply_port = max(1, int(str(reply_port_raw or "8788").strip()))
except Exception:
    reply_port = 8788
reply_token = (reply_token_raw or "").strip()
notify_chat_id = (notify_chat_id_raw or "").strip()
if not notify_chat_id:
    notify_chat_id = (os.environ.get("TELEGRAM_HEALTH_ALERT_CHAT_ID") or "").strip()
if not notify_chat_id:
    allowed = [x.strip() for x in (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").split(",") if x.strip()]
    notify_chat_id = allowed[0] if allowed else ""
plan_script = Path(plan_script_raw).expanduser()
plan_output_path = Path(plan_output_path_raw).expanduser()
self_test_status_path = Path(self_test_status_path_raw).expanduser()
self_test_quick_status_path = Path(self_test_quick_status_path_raw).expanduser()
try:
    plan_timeout_sec = max(1, int(str(plan_timeout_raw or "8").strip()))
except Exception:
    plan_timeout_sec = 8


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def parse_ts(ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def derive_health_state(snapshot: dict, stale_threshold_sec: int) -> tuple[str, str, int | None, bool | None]:
    ts_utc = str(snapshot.get("ts_utc") or "")
    age_sec: int | None = None
    stale: bool | None = None
    if ts_utc:
        parsed = parse_ts(ts_utc)
        if parsed is not None:
            age_sec = max(0, int(now_epoch - int(parsed)))
            stale = age_sec > int(stale_threshold_sec)
    health_ok = snapshot.get("ok")
    alert = snapshot.get("alert")
    severity = "unknown"
    if isinstance(alert, dict):
        raw = str(alert.get("severity") or "").strip().lower()
        if raw in {"none", "warning", "critical"}:
            severity = raw
    if severity == "unknown" and health_ok is True:
        severity = "none"
    state = "unknown"
    if health_ok is True:
        state = "stale" if stale is True else "ok"
    elif health_ok is False:
        state = "degraded" if severity == "warning" else "critical"
    elif stale is True:
        state = "stale"
    return state, (ts_utc or "(unknown)"), age_sec, stale


def sanitize(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    return raw[:2000]


def send_notify(action: str, reason: str, health_state: str, submit_key: str) -> tuple[bool, str]:
    if not notify_enabled:
        return False, "notify_disabled"
    if not notify_chat_id:
        return False, "notify_missing_chat_id"
    if not reply_token:
        return False, "notify_missing_token"
    if action == "skipped" and not notify_on_skip:
        return False, "notify_skip_suppressed"
    lines = [
        "Done: self-improve nudge cycle executed.",
        f"Changed: tmux_nudge action={action} submit_key={submit_key or '-'}",
        f"Status: reason={reason}; health={health_state}",
        "Next: /statusbrief for current runtime snapshot.",
    ]
    text = "\n".join(lines)
    try:
        chat_id_int = int(str(notify_chat_id).strip())
    except Exception:
        return False, "notify_invalid_chat_id"
    payload = json.dumps({"chat_id": chat_id_int, "text": text}, ensure_ascii=False).encode("utf-8")
    url = f"http://{reply_host}:{reply_port}/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {reply_token}",
    }
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            if int(resp.getcode() or 0) == 200:
                return True, "notify_sent"
            return False, f"notify_http_{int(resp.getcode() or 0)}"
    except urllib.error.HTTPError as exc:
        return False, f"notify_http_{int(exc.code)}"
    except Exception as exc:
        return False, f"notify_error:{str(exc)[:120]}"


def pane_exists(pane: str) -> bool:
    proc = subprocess.run(
        ["tmux", "list-panes", "-t", pane],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def is_task_running(pane: str) -> bool:
    proc = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane, "-S", "-60"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    if proc.returncode != 0:
        return False
    snap = (proc.stdout or "").lower()
    return ("esc to interrupt" in snap) or ("working (" in snap)


def resolve_message(default_message: str) -> str:
    global plan_used, plan_reason, plan_task_id, plan_title
    if not auto_plan_enabled:
        plan_reason = "disabled"
        return default_message
    if not plan_script.exists():
        plan_reason = "missing_script"
        return default_message
    cmd = [
        sys.executable,
        str(plan_script),
        "--output-json",
        str(plan_output_path),
        "--health-status-path",
        str(health_status_path),
        "--self-test-path",
        str(self_test_status_path),
        "--self-test-quick-path",
        str(self_test_quick_status_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=plan_timeout_sec,
            check=False,
        )
    except Exception as exc:
        plan_reason = f"planner_error:{str(exc)[:80]}"
        return default_message
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().replace("\n", " ")
        plan_reason = f"planner_rc_{proc.returncode}:{err[:80] or 'no_stderr'}"
        return default_message
    msg = sanitize(proc.stdout or "")
    if not msg:
        plan_reason = "planner_empty"
        return default_message
    plan_used = True
    plan_reason = "ok"
    if plan_output_path.exists():
        try:
            data = json.loads(plan_output_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                plan_task_id = str(data.get("task_id") or "")
                plan_title = str(data.get("title") or "")
        except Exception:
            pass
    return msg

if not enabled:
    action = "disabled"
    reason = "self_improve_disabled"
elif not target.strip():
    action = "skipped"
    reason = "missing_target_pane"
else:
    snapshot = load_state(health_status_path)
    health_state, health_ts_utc, health_age_sec, health_stale = derive_health_state(snapshot, health_stale_threshold_sec)
    resolved_message = resolve_message(message)
    line = sanitize(f"{prefix.strip()} {resolved_message.strip()}".strip())
    try:
        prev = load_state(state_path)
        try:
            last_sent_epoch = int(prev.get("last_sent_epoch") or 0)
        except Exception:
            last_sent_epoch = 0
        if require_healthy and health_state != "ok":
            action = "skipped"
            reason = f"health_not_ok:{health_state}"
        elif min_interval_sec > 0 and last_sent_epoch > 0 and (now_epoch - last_sent_epoch) < min_interval_sec:
            action = "skipped"
            reason = "min_interval_guard"
        elif not pane_exists(target):
            action = "skipped"
            reason = f"pane_missing:{target}"
        else:
            subprocess.run(["tmux", "send-keys", "-t", target, "C-u"], check=True)
            subprocess.run(["tmux", "set-buffer", "--", line], check=True)
            subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-t", target], check=True)
            time.sleep(0.08)
            subprocess.run(["tmux", "send-keys", "-t", target, "Left"], check=True)
            subprocess.run(["tmux", "send-keys", "-t", target, "Right"], check=True)
            time.sleep(0.08)
            task_running = is_task_running(target)
            submit_key = "Tab" if task_running else "Enter"
            subprocess.run(["tmux", "send-keys", "-t", target, submit_key], check=True)
            action = "sent"
            reason = "ok"
            sent_line = line
            last_sent_epoch = now_epoch
    except Exception as exc:
        action = "failed"
        reason = f"send_error:{str(exc)[:160]}"

notify_sent, notify_reason = send_notify(action, reason, health_state, submit_key)

state = {
    "last_run_ts_utc": now.isoformat().replace("+00:00", "Z"),
    "enabled": enabled,
    "require_healthy": require_healthy,
    "health_status_path": str(health_status_path),
    "health_state": health_state,
    "health_ts_utc": health_ts_utc,
    "health_age_sec": health_age_sec,
    "health_stale": health_stale,
    "health_stale_threshold_sec": health_stale_threshold_sec,
    "target_pane": target,
    "prefix": prefix,
    "auto_plan_enabled": auto_plan_enabled,
    "plan_script": str(plan_script),
    "plan_output_path": str(plan_output_path),
    "plan_timeout_sec": plan_timeout_sec,
    "plan_used": plan_used,
    "plan_reason": plan_reason,
    "plan_task_id": plan_task_id,
    "plan_title": plan_title,
    "last_action": action,
    "last_reason": reason,
    "submit_key": submit_key,
    "task_running_detected": task_running,
    "min_interval_sec": min_interval_sec,
    "last_sent_epoch": last_sent_epoch,
    "last_sent_message": sent_line[:240] if sent_line else "",
    "notify_telegram_enabled": notify_enabled,
    "notify_on_skip": notify_on_skip,
    "notify_chat_id": notify_chat_id,
    "notify_sent": notify_sent,
    "notify_reason": notify_reason,
}

state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    f"self-improve action={action} reason={reason} health_state={health_state} "
    f"notify_sent={str(notify_sent).lower()} notify_reason={notify_reason}"
)
PY

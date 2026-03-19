#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/home/foggen/kitan-telegram/.env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

python3 - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_ts(ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def derive_health_state(snapshot: dict, stale_threshold_sec: int) -> tuple[str, str, int | None, bool | None]:
    now_epoch = int(time.time())
    health_ok = snapshot.get("ok")
    health_ts_utc = str(snapshot.get("ts_utc") or "")
    health_age_sec: int | None = None
    health_stale: bool | None = None
    if health_ts_utc:
        parsed = parse_ts(health_ts_utc)
        if parsed is not None:
            health_age_sec = max(0, int(now_epoch - int(parsed)))
            health_stale = health_age_sec > int(stale_threshold_sec)
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
        state = "stale" if health_stale is True else "ok"
    elif health_ok is False:
        state = "degraded" if severity == "warning" else "critical"
    elif health_stale is True:
        state = "stale"

    return state, (health_ts_utc or "(unknown)"), health_age_sec, health_stale


def load_task_next(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in lines:
        raw = line.strip()
        low = raw.lower()
        if low.startswith("- next:") or low.startswith("next:"):
            return raw.split(":", 1)[1].strip()[:220]
        if low.startswith("- next action:") or low.startswith("next action:"):
            return raw.split(":", 1)[1].strip()[:220]
    for idx, line in enumerate(lines):
        raw = line.strip()
        low = raw.lower()
        if low in {"## next", "## next action", "# next", "# next action"}:
            for follow in lines[idx + 1 : idx + 6]:
                val = follow.strip().lstrip("-").strip()
                if not val:
                    continue
                return val[:220]
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        return raw[:220]
    return ""


def find_last_activity(path: Path) -> tuple[float | None, str, str]:
    if not path.exists():
        return None, "", ""
    activity_events = {
        "command_received",
        "plain_text_buffered",
        "plain_text_relayed",
        "image_saved",
        "file_saved",
        "image_saved_and_relayed",
        "file_saved_and_relayed",
    }
    last_epoch = None
    last_event = ""
    last_ts = ""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            event_type = str(obj.get("event_type") or "")
            if event_type not in activity_events:
                continue
            ts_utc = str(obj.get("ts_utc") or "")
            if not ts_utc:
                continue
            epoch = parse_ts(ts_utc)
            if epoch is None:
                continue
            if last_epoch is None or epoch > last_epoch:
                last_epoch = epoch
                last_event = event_type
                last_ts = ts_utc
    except Exception:
        return None, "", ""
    return last_epoch, last_event, last_ts


watcher_enabled = bool_env("YUUKI_WATCHER_ENABLED", False)
watcher_dry_run = bool_env("YUUKI_WATCHER_DRY_RUN", False)
watcher_require_healthy = bool_env("YUUKI_WATCHER_REQUIRE_HEALTHY", True)
idle_sec_threshold = max(30, int_env("YUUKI_WATCHER_IDLE_SEC", 600))
cooldown_sec = max(30, int_env("YUUKI_WATCHER_COOLDOWN_SEC", 3600))
state_path = Path(
    (os.environ.get("YUUKI_WATCHER_STATE_PATH") or "/home/foggen/kitan-telegram/runtime/watcher_state.json").strip()
).expanduser()
task_state_path = Path(
    (os.environ.get("YUUKI_WATCHER_TASK_STATE_PATH") or "/home/foggen/AI_github/projects/yuuki-bot-upgrade/LAST_STATE.md").strip()
).expanduser()
json_log_path = Path(
    (os.environ.get("TELEGRAM_JSON_LOG_PATH") or "/home/foggen/kitan-telegram/logs/bot.jsonl").strip()
).expanduser()
health_status_path = Path(
    (os.environ.get("TELEGRAM_HEALTH_STATUS_PATH") or "/home/foggen/kitan-telegram/runtime/health_status.json").strip()
).expanduser()
health_stale_threshold_sec = max(60, int_env("TELEGRAM_STATUS_HEALTH_STALE_SEC", 1800))

reply_enabled = bool_env("TELEGRAM_LOCAL_REPLY_API_ENABLED", True)
reply_host = (os.environ.get("TELEGRAM_LOCAL_REPLY_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
reply_port = max(1, int_env("TELEGRAM_LOCAL_REPLY_API_PORT", 8788))
reply_token = (os.environ.get("TELEGRAM_LOCAL_REPLY_API_TOKEN") or "").strip()
reply_url = f"http://{reply_host}:{reply_port}/reply"

chat_id = (os.environ.get("TELEGRAM_HEALTH_ALERT_CHAT_ID") or "").strip()
if not chat_id:
    allowed = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").split(",")
    allowed = [x.strip() for x in allowed if x.strip()]
    chat_id = allowed[0] if allowed else ""

now_epoch = int(time.time())
now_dt = datetime.now(timezone.utc)
state_prev = load_json(state_path)
last_nudge_epoch = int(state_prev.get("last_nudge_epoch") or 0)
task_next = load_task_next(task_state_path)

last_activity_epoch, last_activity_event, last_activity_ts = find_last_activity(json_log_path)
idle_sec = None if last_activity_epoch is None else max(0, int(now_epoch - int(last_activity_epoch)))
health_snapshot = load_json(health_status_path)
health_state, health_ts_utc, health_age_sec, health_stale = derive_health_state(health_snapshot, health_stale_threshold_sec)

action = "noop"
reason = "ok"

should_nudge = False
if not watcher_enabled:
    action = "disabled"
    reason = "watcher_disabled"
elif idle_sec is None:
    reason = "no_activity_events"
elif idle_sec < idle_sec_threshold:
    reason = "idle_below_threshold"
elif now_epoch - last_nudge_epoch < cooldown_sec:
    reason = "cooldown_active"
elif not reply_enabled:
    reason = "reply_api_disabled"
elif not reply_token:
    reason = "missing_reply_token"
elif not chat_id:
    reason = "missing_chat_id"
elif watcher_require_healthy and health_state != "ok":
    reason = f"health_not_ok:{health_state}"
else:
    should_nudge = True
    reason = "idle_threshold_exceeded"

if should_nudge and watcher_dry_run:
    action = "nudge_dry_run"
    reason = "idle_threshold_exceeded"
elif should_nudge:
    idle_m = (idle_sec or 0) // 60
    idle_s = (idle_sec or 0) % 60
    lines = [
        "Yuuki watcher:",
        f"No inbound activity for {idle_m}m {idle_s}s.",
        "Autonomous mode is active.",
    ]
    if task_next:
        lines.append(f"Next planned: {task_next}")
    lines.append("Use /status for full runtime snapshot.")
    text = "\n".join(lines)
    payload = json.dumps({"chat_id": int(chat_id), "text": text}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {reply_token}",
    }
    req = urllib.request.Request(reply_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = int(getattr(resp, "status", 200))
        if 200 <= code < 300:
            action = "nudge_sent"
            reason = "idle_threshold_exceeded"
            last_nudge_epoch = now_epoch
        else:
            action = "nudge_failed"
            reason = f"http_{code}"
    except urllib.error.HTTPError as exc:
        action = "nudge_failed"
        reason = f"http_{exc.code}"
    except Exception as exc:
        action = "nudge_failed"
        reason = f"error:{str(exc)[:120]}"

state = {
    "last_run_ts_utc": now_dt.isoformat().replace("+00:00", "Z"),
    "watcher_enabled": watcher_enabled,
    "watcher_dry_run": watcher_dry_run,
    "watcher_require_healthy": watcher_require_healthy,
    "idle_threshold_sec": idle_sec_threshold,
    "cooldown_sec": cooldown_sec,
    "idle_sec": idle_sec,
    "last_activity_ts_utc": last_activity_ts or "(unknown)",
    "last_activity_event": last_activity_event or "(unknown)",
    "health_status_path": str(health_status_path),
    "health_state": health_state,
    "health_ts_utc": health_ts_utc,
    "health_age_sec": health_age_sec,
    "health_stale": health_stale,
    "health_stale_threshold_sec": health_stale_threshold_sec,
    "last_action": action,
    "last_reason": reason,
    "last_nudge_epoch": last_nudge_epoch,
    "task_state_path": str(task_state_path),
    "task_next": task_next or "",
}

state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"watcher action={action} reason={reason} health_state={health_state}")
sys.exit(0)
PY

#!/usr/bin/env bash
set -euo pipefail

IN_STATUS_PATH="${YUUKI_SELF_TEST_STATUS_PATH-}"
IN_PROFILE="${YUUKI_SELF_TEST_PROFILE-}"
IN_ONLY="${YUUKI_SELF_TEST_ONLY-}"

ENV_FILE="/home/foggen/kitan-telegram/.env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

if [[ -n "${IN_STATUS_PATH}" ]]; then YUUKI_SELF_TEST_STATUS_PATH="${IN_STATUS_PATH}"; fi
if [[ -n "${IN_PROFILE}" ]]; then YUUKI_SELF_TEST_PROFILE="${IN_PROFILE}"; fi
if [[ -n "${IN_ONLY}" ]]; then YUUKI_SELF_TEST_ONLY="${IN_ONLY}"; fi

STATUS_PATH="${YUUKI_SELF_TEST_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/self_test_latest.json}"

cd /home/foggen/kitan-telegram

if command -v uv >/dev/null 2>&1; then
  RUNNER=(uv run python)
else
  RUNNER=(/home/foggen/kitan-telegram/.venv/bin/python3)
fi

"${RUNNER[@]}" - "$STATUS_PATH" <<'PY'
import json
import hashlib
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import traceback

import bot

status_path = Path(sys.argv[1]).expanduser()
started = time.perf_counter()
profile_raw = (os.environ.get("YUUKI_SELF_TEST_PROFILE") or "full").strip().lower()
if profile_raw not in {"full", "quick"}:
    profile_raw = "full"
only_raw = (os.environ.get("YUUKI_SELF_TEST_ONLY") or "").strip()
requested_checks = [x.strip() for x in re.split(r"[,\s]+", only_raw) if x.strip()]
requested_check_set = set(requested_checks)

checks: list[dict[str, object]] = []
failures: list[str] = []
skipped_checks: list[str] = []


def record(name: str, ok: bool, details: dict[str, object]) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})
    if not ok:
        failures.append(name)


def run_check(name: str, fn):
    try:
        details = fn()
        record(name, True, details if isinstance(details, dict) else {"result": str(details)})
    except Exception as exc:
        record(
            name,
            False,
            {
                "error": str(exc),
                "trace": traceback.format_exc(limit=2),
            },
        )


def check_text_deduper() -> dict[str, object]:
    d = bot.TextDeduper(True, 45)
    a1 = d.allow(1, "[telegram] ping")
    a2 = d.allow(1, "[telegram]    ping ")
    a3 = d.allow(1, "[telegram] pong")
    assert a1 == (True, 1)
    assert a2[0] is False and a2[1] == 2
    assert a3 == (True, 1)
    return {"a1": a1, "a2": a2, "a3": a3}


def check_queue_drop_oldest() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-queue-drop-") as td:
        state_path = Path(td) / "reply_metrics_state.json"
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "reply_api_queue_max": 2,
                "reply_api_queue_drop_oldest": True,
                "reply_api_metrics_state_path": state_path,
            }
        )
        api = bot.LocalReplyAPI(cfg2, bot.LastChatState())
        r1 = api._enqueue_reply(1, "m1")
        r2 = api._enqueue_reply(1, "m2")
        r3 = api._enqueue_reply(1, "m3")
        q = list(api._send_q.queue)
        m = api.metrics_snapshot()
        assert r1 == (True, False)
        assert r2 == (True, False)
        assert r3 == (True, True)
        assert q == [(1, "m2"), (1, "m3")]
        assert int(m.get("queue_dropped_total") or 0) == 1
        return {"enqueue": [r1, r2, r3], "queue": q, "queue_dropped_total": m.get("queue_dropped_total")}


def check_queue_reject() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-queue-reject-") as td:
        state_path = Path(td) / "reply_metrics_state.json"
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "reply_api_queue_max": 2,
                "reply_api_queue_drop_oldest": False,
                "reply_api_metrics_state_path": state_path,
            }
        )
        api = bot.LocalReplyAPI(cfg2, bot.LastChatState())
        r1 = api._enqueue_reply(1, "m1")
        r2 = api._enqueue_reply(1, "m2")
        r3 = api._enqueue_reply(1, "m3")
        q = list(api._send_q.queue)
        m = api.metrics_snapshot()
        assert r1 == (True, False)
        assert r2 == (True, False)
        assert r3 == (False, False)
        assert q == [(1, "m1"), (1, "m2")]
        assert int(m.get("queue_full_rejected_total") or 0) == 1
        return {"enqueue": [r1, r2, r3], "queue": q, "queue_full_rejected_total": m.get("queue_full_rejected_total")}


def check_event_scan_fallback() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    lines = 6000
    payload_blob = "x" * 3800
    fd = tempfile.NamedTemporaryFile(delete=False)
    path = Path(fd.name)
    fd.close()
    try:
        start = now - timedelta(seconds=lines)
        with path.open("w", encoding="utf-8") as f:
            for i in range(lines):
                ts = (start + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
                et = "reply_failed" if i % 111 == 0 else ("relay_error" if i % 173 == 0 else "command_received")
                rec = {"ts_utc": ts, "event_type": et, "event_fields": {"i": i, "blob": payload_blob}}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        full, _ = bot._recent_event_summary(path, 15, path.stat().st_size)
        cfg = bot.load_config()
        cfg2 = cfg.__class__(**{**cfg.__dict__, "json_log_path": path, "status_event_scan_max_bytes": 262_144})
        payload = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload.get("event_scan_retry_used") is True
        assert int(payload.get("event_reply_failed") or 0) == int(full.get("reply_failed") or 0)
        assert int(payload.get("event_relay_error") or 0) == int(full.get("relay_error") or 0)
        return {
            "full_reply_failed": full.get("reply_failed"),
            "full_relay_error": full.get("relay_error"),
            "payload_reply_failed": payload.get("event_reply_failed"),
            "payload_relay_error": payload.get("event_relay_error"),
            "event_scan_retry_used": payload.get("event_scan_retry_used"),
        }
    finally:
        path.unlink(missing_ok=True)


def check_autonomy_health_gates_smoke() -> dict[str, object]:
    import subprocess

    script = Path("/home/foggen/kitan-telegram/scripts/smoke_autonomy_health_gates.sh")
    if not script.exists():
        raise RuntimeError(f"missing script: {script}")
    proc = subprocess.run(
        ["bash", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        text=True,
        check=False,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"smoke failed rc={proc.returncode} out={out[:200]} err={err[:200]}")
    if "PASS: autonomy health-gate smoke checks" not in out:
        raise RuntimeError(f"unexpected smoke output: {out[:240]}")
    return {"rc": proc.returncode, "stdout": out[:240]}


def check_monitoring_config_smoke() -> dict[str, object]:
    import subprocess

    script = Path("/home/foggen/kitan-telegram/scripts/smoke_monitoring_config.sh")
    if not script.exists():
        raise RuntimeError(f"missing script: {script}")
    proc = subprocess.run(
        ["bash", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        text=True,
        check=False,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"monitoring smoke failed rc={proc.returncode} out={out[:220]} err={err[:220]}")
    if "PASS: monitoring config smoke checks" not in out:
        raise RuntimeError(f"unexpected monitoring smoke output: {out[:240]}")
    return {"rc": proc.returncode, "stdout": out[:240]}


def check_status_self_test_observability() -> dict[str, object]:
    cfg = bot.load_config()
    fd = tempfile.NamedTemporaryFile(delete=False)
    path = Path(fd.name)
    fd.close()
    try:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=9_000)).isoformat().replace("+00:00", "Z")
        snapshot = {
            "ts_utc": ts,
            "ok": False,
            "failures": ["queue_drop_oldest"],
            "checks": [
                {"name": "queue_drop_oldest", "ok": False},
                {"name": "queue_reject", "ok": True},
            ],
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "self_test_status_path": path,
                "self_test_max_age_sec": 7200,
            }
        )
        payload = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload.get("self_test_stale") is True
        assert int(payload.get("self_test_checks_total") or 0) == 2
        assert payload.get("self_test_failed_names") == ["queue_drop_oldest"]
        assert payload.get("self_test_failure_count") == 1
        return {
            "self_test_stale": payload.get("self_test_stale"),
            "self_test_checks_total": payload.get("self_test_checks_total"),
            "self_test_failed_names": payload.get("self_test_failed_names"),
            "self_test_failure_count": payload.get("self_test_failure_count"),
        }
    finally:
        path.unlink(missing_ok=True)


def check_status_health_self_test_repeated_signal() -> dict[str, object]:
    cfg = bot.load_config()
    fd = tempfile.NamedTemporaryFile(delete=False)
    path = Path(fd.name)
    fd.close()
    try:
        snapshot = {
            "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "fail_count": 2,
            "fails": ["self_test:failed", "self_test:repeated_check:queue_reject"],
            "self_test": {
                "failed_checks": ["queue_reject"],
                "repeated_fail_checks": ["queue_reject"],
                "fail_streak_threshold": 3,
            },
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "health_status_path": path,
            }
        )
        payload = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload.get("health_self_test_repeated_fail_checks") == ["queue_reject"], payload
        assert payload.get("health_self_test_fail_streak_threshold") == 3, payload
        brief = bot._format_runtime_status_brief(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "repeat_hc=queue_reject" in brief, brief
        assert "repeat_thr=3" in brief, brief
        return {
            "health_self_test_repeated_fail_checks": payload.get("health_self_test_repeated_fail_checks"),
            "health_self_test_fail_streak_threshold": payload.get("health_self_test_fail_streak_threshold"),
            "brief_self_test_line": [ln for ln in brief.splitlines() if ln.startswith("self_test ")][0],
        }
    finally:
        path.unlink(missing_ok=True)


def check_status_health_self_test_autorepair_signal() -> dict[str, object]:
    cfg = bot.load_config()
    fd = tempfile.NamedTemporaryFile(delete=False)
    path = Path(fd.name)
    fd.close()
    try:
        snapshot_attempted = {
            "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "fail_count": 1,
            "fails": ["self_test:failed"],
            "self_test": {
                "failed_checks": ["queue_reject"],
                "repeated_fail_checks": [],
                "fail_streak_threshold": 3,
                "autorepair_enabled": True,
                "autorepair_attempted": True,
                "autorepair_result": "recovered",
                "autorepair_timeout_sec": 180,
                "autorepair_script": "/tmp/self_test_stub.sh",
            },
        }
        path.write_text(json.dumps(snapshot_attempted, ensure_ascii=False), encoding="utf-8")
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "health_status_path": path,
            }
        )
        payload_attempted = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload_attempted.get("health_self_test_autorepair_enabled") is True, payload_attempted
        assert payload_attempted.get("health_self_test_autorepair_attempted") is True, payload_attempted
        assert payload_attempted.get("health_self_test_autorepair_result") == "recovered", payload_attempted
        assert payload_attempted.get("health_self_test_autorepair_timeout_sec") == 180, payload_attempted
        assert payload_attempted.get("health_self_test_autorepair_script") == "/tmp/self_test_stub.sh", payload_attempted
        brief_attempted = bot._format_runtime_status_brief(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "autorepair=recovered" in brief_attempted, brief_attempted

        snapshot_not_attempted = {
            **snapshot_attempted,
            "self_test": {
                **snapshot_attempted["self_test"],
                "autorepair_attempted": False,
                "autorepair_result": "not_needed",
            },
        }
        path.write_text(json.dumps(snapshot_not_attempted, ensure_ascii=False), encoding="utf-8")
        payload_not_attempted = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload_not_attempted.get("health_self_test_autorepair_attempted") is False, payload_not_attempted
        assert payload_not_attempted.get("health_self_test_autorepair_result") == "not_needed", payload_not_attempted
        brief_not_attempted = bot._format_runtime_status_brief(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "autorepair=" not in brief_not_attempted, brief_not_attempted
        return {
            "attempted_result": payload_attempted.get("health_self_test_autorepair_result"),
            "not_attempted_result": payload_not_attempted.get("health_self_test_autorepair_result"),
            "attempted_line": [ln for ln in brief_attempted.splitlines() if ln.startswith("self_test ")][0],
            "not_attempted_line": [ln for ln in brief_not_attempted.splitlines() if ln.startswith("self_test ")][0],
        }
    finally:
        path.unlink(missing_ok=True)


def check_status_health_operator_hint_autorepair() -> dict[str, object]:
    cfg = bot.load_config()
    fd = tempfile.NamedTemporaryFile(delete=False)
    path = Path(fd.name)
    fd.close()
    try:
        snapshot_attempted = {
            "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "fail_count": 2,
            "fails": ["self_test:failed", "event:reply_failed=4>=3"],
            "alert": {"severity": "critical", "cooldown_sec_effective": 900},
            "self_test": {
                "failed_checks": ["queue_reject"],
                "repeated_fail_checks": [],
                "fail_streak_threshold": 3,
                "autorepair_enabled": True,
                "autorepair_attempted": True,
                "autorepair_result": "attempt_no_recovery",
                "autorepair_timeout_sec": 180,
                "autorepair_script": "/tmp/self_test_stub.sh",
            },
        }
        path.write_text(json.dumps(snapshot_attempted, ensure_ascii=False), encoding="utf-8")
        cfg2 = cfg.__class__(**{**cfg.__dict__, "health_status_path": path})
        payload_attempted = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        hint_attempted = str(payload_attempted.get("health_operator_hint") or "")
        hint_code_attempted = str(payload_attempted.get("health_operator_hint_code") or "")
        assert "autorepair=attempt_no_recovery" in hint_attempted, payload_attempted
        assert hint_code_attempted == "critical_autorepair_attempt_no_recovery", payload_attempted
        status_code_attempted = bot._format_runtime_status_code(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert status_code_attempted.startswith("YUUKI_STATUS_CODE "), status_code_attempted
        assert "state=critical" in status_code_attempted, status_code_attempted
        assert "hint_code=critical_autorepair_attempt_no_recovery" in status_code_attempted, status_code_attempted
        assert "fails=2" in status_code_attempted, status_code_attempted
        assert "quick_lane=" in status_code_attempted, status_code_attempted
        assert "in_alert=" in status_code_attempted, status_code_attempted
        assert "sig8=" in status_code_attempted, status_code_attempted
        status_code_json_attempted = bot._format_runtime_status_code_json(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        parsed_attempted = json.loads(status_code_json_attempted)
        assert parsed_attempted.get("state") == "critical", parsed_attempted
        assert parsed_attempted.get("hint_code") == "critical_autorepair_attempt_no_recovery", parsed_attempted
        assert parsed_attempted.get("schema") == "yuuki.statuscode", parsed_attempted
        assert int(parsed_attempted.get("version")) == int(bot.STATUS_CODE_VERSION), parsed_attempted
        assert int(parsed_attempted.get("fails")) == 2, parsed_attempted
        assert parsed_attempted.get("quick_lane") in {"ok", "stale", "missing", "failed", "disabled", "unknown"}, parsed_attempted
        assert parsed_attempted.get("reply_auth") in {"ok", "fail", "disabled", "unknown"}, parsed_attempted
        assert isinstance(parsed_attempted.get("in_alert"), bool), parsed_attempted
        sig8_attempted = str(parsed_attempted.get("sig8"))
        assert sig8_attempted == "(none)" or re.fullmatch(r"[0-9a-f]{8}", sig8_attempted), parsed_attempted

        snapshot_not_attempted = {
            **snapshot_attempted,
            "self_test": {
                **snapshot_attempted["self_test"],
                "autorepair_attempted": False,
                "autorepair_result": "not_needed",
            },
        }
        path.write_text(json.dumps(snapshot_not_attempted, ensure_ascii=False), encoding="utf-8")
        payload_not_attempted = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        hint_not_attempted = str(payload_not_attempted.get("health_operator_hint") or "")
        hint_code_not_attempted = str(payload_not_attempted.get("health_operator_hint_code") or "")
        assert "autorepair=" not in hint_not_attempted, payload_not_attempted
        assert hint_code_not_attempted == "critical_check_service_api_tmux", payload_not_attempted
        status_code_not_attempted = bot._format_runtime_status_code(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "hint_code=critical_check_service_api_tmux" in status_code_not_attempted, status_code_not_attempted
        status_code_json_not_attempted = bot._format_runtime_status_code_json(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        parsed_not_attempted = json.loads(status_code_json_not_attempted)
        assert parsed_not_attempted.get("hint_code") == "critical_check_service_api_tmux", parsed_not_attempted
        assert parsed_not_attempted.get("schema") == "yuuki.statuscode", parsed_not_attempted
        assert int(parsed_not_attempted.get("version")) == int(bot.STATUS_CODE_VERSION), parsed_not_attempted
        assert parsed_not_attempted.get("reply_auth") in {"ok", "fail", "disabled", "unknown"}, parsed_not_attempted

        snapshot_ok = {
            "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": True,
            "fail_count": 0,
            "fails": [],
            "alert": {"severity": "none", "cooldown_sec_effective": 0},
        }
        path.write_text(json.dumps(snapshot_ok, ensure_ascii=False), encoding="utf-8")
        payload_ok = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload_ok.get("health_operator_hint_code") == "ok_none", payload_ok
        status_code_ok = bot._format_runtime_status_code(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "state=ok" in status_code_ok, status_code_ok
        assert "hint_code=ok_none" in status_code_ok, status_code_ok
        status_code_json_ok = bot._format_runtime_status_code_json(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        parsed_ok = json.loads(status_code_json_ok)
        assert parsed_ok.get("state") == "ok", parsed_ok
        assert parsed_ok.get("hint_code") == "ok_none", parsed_ok
        assert parsed_ok.get("schema") == "yuuki.statuscode", parsed_ok
        assert int(parsed_ok.get("version")) == int(bot.STATUS_CODE_VERSION), parsed_ok
        assert parsed_ok.get("quick_lane") in {"ok", "stale", "missing", "failed", "disabled", "unknown"}, parsed_ok
        assert parsed_ok.get("reply_auth") in {"ok", "fail", "disabled", "unknown"}, parsed_ok
        return {
            "hint_attempted": hint_attempted,
            "hint_code_attempted": hint_code_attempted,
            "status_code_attempted": status_code_attempted,
            "status_code_json_attempted": status_code_json_attempted,
            "hint_not_attempted": hint_not_attempted,
            "hint_code_not_attempted": hint_code_not_attempted,
            "status_code_not_attempted": status_code_not_attempted,
            "status_code_json_not_attempted": status_code_json_not_attempted,
            "hint_code_ok": payload_ok.get("health_operator_hint_code"),
            "status_code_ok": status_code_ok,
            "status_code_json_ok": status_code_json_ok,
        }
    finally:
        path.unlink(missing_ok=True)


def check_local_reply_api_status_code_provider() -> dict[str, object]:
    cfg = bot.load_config()
    state = bot.LastChatState()

    def provider(with_schema: bool) -> dict[str, object]:
        base = {
            "state": "ok",
            "hint_code": "ok_none",
            "severity": "none",
            "fails": 0,
            "quick_lane": "ok",
            "reply_auth": "ok",
            "sig8": "(none)",
            "in_alert": False,
            "stale": False,
            "ts": "2026-02-27T00:00:00Z",
        }
        if with_schema:
            base["schema"] = bot.STATUS_CODE_SCHEMA
            base["version"] = bot.STATUS_CODE_VERSION
        return base

    api = bot.LocalReplyAPI(cfg, state, status_code_provider=provider)
    p_no_schema = api._status_code_payload(with_schema=False)
    assert p_no_schema.get("state") == "ok", p_no_schema
    assert p_no_schema.get("hint_code") == "ok_none", p_no_schema
    assert p_no_schema.get("quick_lane") == "ok", p_no_schema
    assert p_no_schema.get("reply_auth") == "ok", p_no_schema
    assert "schema" not in p_no_schema, p_no_schema
    assert "version" not in p_no_schema, p_no_schema

    p_schema = api._status_code_payload(with_schema=True)
    assert p_schema.get("schema") == bot.STATUS_CODE_SCHEMA, p_schema
    assert p_schema.get("version") == bot.STATUS_CODE_VERSION, p_schema

    api_fallback = bot.LocalReplyAPI(cfg, state, status_code_provider=None)
    f_schema = api_fallback._status_code_payload(with_schema=True)
    assert f_schema.get("state") == "unknown", f_schema
    assert f_schema.get("quick_lane") == "unknown", f_schema
    assert f_schema.get("reply_auth") == "unknown", f_schema
    assert f_schema.get("schema") == bot.STATUS_CODE_SCHEMA, f_schema
    assert f_schema.get("version") == bot.STATUS_CODE_VERSION, f_schema
    return {
        "provider_state": p_no_schema.get("state"),
        "provider_hint_code": p_no_schema.get("hint_code"),
        "provider_quick_lane": p_no_schema.get("quick_lane"),
        "provider_reply_auth": p_no_schema.get("reply_auth"),
        "schema": p_schema.get("schema"),
        "version": p_schema.get("version"),
        "fallback_state": f_schema.get("state"),
    }


def check_local_reply_api_metrics_prometheus() -> dict[str, object]:
    cfg = bot.load_config()
    state = bot.LastChatState()
    seq = [True, False, True]
    idx = {"n": 0}

    def provider(with_schema: bool) -> dict[str, object]:
        pos = idx["n"]
        idx["n"] = pos + 1
        in_alert = seq[pos] if pos < len(seq) else seq[-1]
        base = {
            "state": "critical",
            "hint_code": "critical_check_service_api_tmux",
            "severity": "critical",
            "fails": 2,
            "quick_lane": "failed",
            "reply_auth": "fail",
            "sig8": "abcd1234",
            "in_alert": in_alert,
            "stale": False,
            "ts": "2026-02-27T00:00:00Z",
        }
        if with_schema:
            base["schema"] = bot.STATUS_CODE_SCHEMA
            base["version"] = bot.STATUS_CODE_VERSION
        return base

    with tempfile.TemporaryDirectory(prefix="yuuki-metrics-prom-") as td:
        state_path = Path(td) / "reply_metrics_state.json"
        cfg2 = cfg.__class__(**{**cfg.__dict__, "reply_api_metrics_state_path": state_path})

        api = bot.LocalReplyAPI(cfg2, state, status_code_provider=provider)
        text = api._metrics_prometheus_text()
        assert text.endswith("\n"), text
        assert "yuuki_reply_queue_depth " in text, text
        assert "yuuki_reply_sent_total " in text, text
        assert "yuuki_reply_failed_total " in text, text
        assert "yuuki_reply_queue_dropped_total 0" in text, text
        assert "yuuki_reply_queue_full_rejected_total 0" in text, text
        assert "yuuki_health_fail_count 2" in text, text
        assert "yuuki_health_in_alert 1" in text, text
        assert "yuuki_health_stale 0" in text, text
        assert "yuuki_health_quick_lane_state 4" in text, text
        assert "yuuki_reply_auth_probe_state 2" in text, text
        assert "yuuki_alert_should_page 1" in text, text
        assert "yuuki_alert_transitions_total 0" in text, text
        expected_info = (
            'yuuki_statuscode_info{state="critical",hint_code="critical_check_service_api_tmux",severity="critical",'
            f'quick_lane="failed",reply_auth="fail",sig8="abcd1234",schema="{bot.STATUS_CODE_SCHEMA}",version="{bot.STATUS_CODE_VERSION}",'
            'in_alert="true",stale="false"} 1'
        )
        assert expected_info in text, text
        text2 = api._metrics_prometheus_text()
        assert "yuuki_health_in_alert 0" in text2, text2
        assert "yuuki_alert_transitions_total 1" in text2, text2
        text3 = api._metrics_prometheus_text()
        assert "yuuki_health_in_alert 1" in text3, text3
        assert "yuuki_alert_transitions_total 2" in text3, text3
        state_path_unknown = Path(td) / "reply_metrics_state_unknown.json"
        cfg3 = cfg.__class__(**{**cfg.__dict__, "reply_api_metrics_state_path": state_path_unknown})
        api_unknown = bot.LocalReplyAPI(cfg3, state, status_code_provider=None)
        text_unknown = api_unknown._metrics_prometheus_text()
        assert "yuuki_alert_should_page -1" in text_unknown, text_unknown
        assert "yuuki_health_quick_lane_state -1" in text_unknown, text_unknown
        assert "yuuki_reply_auth_probe_state -1" in text_unknown, text_unknown
        assert "yuuki_alert_transitions_total 0" in text_unknown, text_unknown
        return {
            "has_statuscode_info": "yuuki_statuscode_info{" in text,
            "health_fail_count_line": "yuuki_health_fail_count 2",
            "health_in_alert_line": "yuuki_health_in_alert 1",
            "health_quick_lane_line": "yuuki_health_quick_lane_state 4",
            "reply_auth_line": "yuuki_reply_auth_probe_state 2",
            "alert_should_page_line": "yuuki_alert_should_page 1",
            "alert_transitions_line": "yuuki_alert_transitions_total 2",
        }


def check_local_reply_api_alert_transition_persistence() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-reply-metrics-state-") as td:
        state_path = Path(td) / "reply_metrics_state.json"
        cfg2 = cfg.__class__(**{**cfg.__dict__, "reply_api_metrics_state_path": state_path})

        api1 = bot.LocalReplyAPI(cfg2, bot.LastChatState())
        assert api1.metrics_snapshot().get("alert_transitions_total") == 0
        api1._track_alert_transition(True)
        api1._track_alert_transition(False)
        api1._track_alert_transition(True)
        api1._mark_queue_dropped()
        api1._mark_queue_rejected()
        m1 = api1.metrics_snapshot()
        assert m1.get("alert_transitions_total") == 2, m1
        assert m1.get("queue_dropped_total") == 1, m1
        assert m1.get("queue_full_rejected_total") == 1, m1
        assert state_path.exists(), str(state_path)

        saved1 = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved1.get("alert_transitions_total") == 2, saved1
        assert saved1.get("queue_dropped_total") == 1, saved1
        assert saved1.get("queue_full_rejected_total") == 1, saved1
        assert saved1.get("last_in_alert_seen") is True, saved1

        api2 = bot.LocalReplyAPI(cfg2, bot.LastChatState())
        m2 = api2.metrics_snapshot()
        assert m2.get("alert_transitions_total") == 2, m2
        assert m2.get("queue_dropped_total") == 1, m2
        assert m2.get("queue_full_rejected_total") == 1, m2
        api2._track_alert_transition(False)
        api2._mark_queue_rejected()
        m3 = api2.metrics_snapshot()
        assert m3.get("alert_transitions_total") == 3, m3
        assert m3.get("queue_dropped_total") == 1, m3
        assert m3.get("queue_full_rejected_total") == 2, m3

        saved2 = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved2.get("alert_transitions_total") == 3, saved2
        assert saved2.get("queue_dropped_total") == 1, saved2
        assert saved2.get("queue_full_rejected_total") == 2, saved2
        assert saved2.get("last_in_alert_seen") is False, saved2

        return {
            "state_path": str(state_path),
            "loaded_transitions_total": m2.get("alert_transitions_total"),
            "updated_transitions_total": m3.get("alert_transitions_total"),
            "loaded_queue_dropped_total": m2.get("queue_dropped_total"),
            "loaded_queue_full_rejected_total": m2.get("queue_full_rejected_total"),
            "updated_queue_full_rejected_total": m3.get("queue_full_rejected_total"),
        }


def check_local_reply_api_auth_modes() -> dict[str, object]:
    import urllib.error
    import urllib.request

    cfg = bot.load_config()

    def post_json(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return int(resp.status), json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed: dict[str, object]
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw}
            return int(exc.code), parsed

    cfg_token = cfg.__class__(
        **{
            **cfg.__dict__,
            "reply_api_enabled": True,
            "reply_api_host": "127.0.0.1",
            "reply_api_port": 0,
            "reply_api_token": "auth-test-token",
        }
    )
    api = bot.LocalReplyAPI(cfg_token, bot.LastChatState())
    api.start()
    try:
        if api.server is None:
            raise RuntimeError("reply api server not started")
        port = int(api.server.server_address[1])
        url = f"http://127.0.0.1:{port}/reply"

        status_noauth, body_noauth = post_json(url, {"text": "auth-check"})
        assert status_noauth == 401, (status_noauth, body_noauth)
        assert body_noauth.get("error") == "unauthorized", body_noauth

        status_bad, body_bad = post_json(
            url,
            {"text": "auth-check"},
            {"Authorization": "Bearer wrong-token"},
        )
        assert status_bad == 401, (status_bad, body_bad)
        assert body_bad.get("error") == "unauthorized", body_bad

        status_bearer, body_bearer = post_json(
            url,
            {"text": "auth-check"},
            {"Authorization": "Bearer auth-test-token"},
        )
        assert status_bearer == 409, (status_bearer, body_bearer)
        assert body_bearer.get("error") == "no_recent_chat", body_bearer

        status_apikey, body_apikey = post_json(
            url,
            {"text": "auth-check"},
            {"x-api-key": "auth-test-token"},
        )
        assert status_apikey == 409, (status_apikey, body_apikey)
        assert body_apikey.get("error") == "no_recent_chat", body_apikey
    finally:
        if api.server is not None:
            api.server.shutdown()
            api.server.server_close()
        if api.thread is not None:
            api.thread.join(timeout=2)

    cfg_open = cfg.__class__(
        **{
            **cfg.__dict__,
            "reply_api_enabled": True,
            "reply_api_host": "127.0.0.1",
            "reply_api_port": 0,
            "reply_api_token": "",
        }
    )
    api_open = bot.LocalReplyAPI(cfg_open, bot.LastChatState())
    api_open.start()
    try:
        if api_open.server is None:
            raise RuntimeError("reply api open-token server not started")
        open_port = int(api_open.server.server_address[1])
        open_url = f"http://127.0.0.1:{open_port}/reply"
        status_open, body_open = post_json(open_url, {"text": "auth-check"})
        assert status_open == 409, (status_open, body_open)
        assert body_open.get("error") == "no_recent_chat", body_open
    finally:
        if api_open.server is not None:
            api_open.server.shutdown()
            api_open.server.server_close()
        if api_open.thread is not None:
            api_open.thread.join(timeout=2)

    return {
        "no_auth_status": status_noauth,
        "bad_auth_status": status_bad,
        "bearer_status": status_bearer,
        "x_api_key_status": status_apikey,
        "no_token_status": status_open,
    }


def check_status_health_fails_top_signal() -> dict[str, object]:
    cfg = bot.load_config()
    fd = tempfile.NamedTemporaryFile(delete=False)
    path = Path(fd.name)
    fd.close()
    try:
        snapshot = {
            "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "fail_count": 4,
            "fails": [
                "event:reply_failed=7>=3",
                "self_test:failed",
                "self_test:repeated_check:queue_reject",
                "tmux_session:main=missing",
            ],
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "health_status_path": path,
            }
        )
        payload = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        top = payload.get("health_fails_top")
        assert top == [
            "event:reply_failed=7>=3",
            "self_test:failed",
            "self_test:repeated_check:queue_reject",
        ], payload
        brief = bot._format_runtime_status_brief(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "top=event:reply_failed=7>=3,self_test:failed,self_test:repeated_check:queue_reject" in brief, brief
        return {
            "health_fails_top": top,
            "brief_health_line": [ln for ln in brief.splitlines() if ln.startswith("health ")][0],
        }
    finally:
        path.unlink(missing_ok=True)


def check_status_alert_cooldown_left_signal() -> dict[str, object]:
    cfg = bot.load_config()
    hfd = tempfile.NamedTemporaryFile(delete=False)
    afd = tempfile.NamedTemporaryFile(delete=False)
    health_path = Path(hfd.name)
    alert_path = Path(afd.name)
    hfd.close()
    afd.close()
    try:
        now = int(time.time())
        health_snapshot = {
            "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": False,
            "fail_count": 1,
            "fails": ["self_test:failed"],
            "alert": {
                "severity": "critical",
                "cooldown_sec_effective": 900,
            },
        }
        alert_snapshot = {
            "in_alert": True,
            "last_alert_epoch": now - 30,
            "last_fingerprint": "self_test:failed|",
            "last_severity": "critical",
            "cooldown_sec": 120,
            "updated_epoch": now,
        }
        health_path.write_text(json.dumps(health_snapshot, ensure_ascii=False), encoding="utf-8")
        alert_path.write_text(json.dumps(alert_snapshot, ensure_ascii=False), encoding="utf-8")
        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "health_status_path": health_path,
                "health_alert_state_path": alert_path,
            }
        )
        payload = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        left = payload.get("health_alert_cooldown_left_sec")
        assert isinstance(left, int), payload
        assert 1 <= left <= 120, payload
        sig_short = payload.get("health_alert_sig_short")
        assert isinstance(sig_short, str) and sig_short.startswith("self_test:failed"), payload
        sig_hash8 = payload.get("health_alert_sig_hash8")
        expected_sig_hash8 = hashlib.sha256("self_test:failed".encode("utf-8")).hexdigest()[:8]
        assert sig_hash8 == expected_sig_hash8, payload
        last_age = payload.get("health_alert_last_age_sec")
        assert isinstance(last_age, int), payload
        assert last_age >= 30, payload
        assert payload.get("health_alert_in_alert") is True, payload
        brief = bot._format_runtime_status_brief(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "cd_left_s=" in brief, brief
        assert "sig_short=self_test:failed" in brief, brief
        assert f"sig8={expected_sig_hash8}" in brief, brief
        assert "last_age_s=" in brief, brief
        alert_snapshot_idle = {
            "in_alert": False,
            "last_fingerprint": "",
            "last_severity": "none",
            "cooldown_sec": 120,
            "updated_epoch": now,
        }
        alert_path.write_text(json.dumps(alert_snapshot_idle, ensure_ascii=False), encoding="utf-8")
        payload_idle = bot._runtime_status_payload(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert payload_idle.get("health_alert_in_alert") is False, payload_idle
        assert payload_idle.get("health_alert_cooldown_left_sec") == 0, payload_idle
        assert payload_idle.get("health_alert_last_age_sec") == 0, payload_idle
        assert payload_idle.get("health_alert_sig_short") == "(none)", payload_idle
        assert payload_idle.get("health_alert_sig_hash8") == "(none)", payload_idle
        brief_idle = bot._format_runtime_status_brief(
            cfg2,
            bot.TmuxRelay(cfg2),
            bot.PiperTTS(cfg2),
            bot.LocalReplyAPI(cfg2, bot.LastChatState()),
        )
        assert "cd_left_s=0" in brief_idle, brief_idle
        assert "sig_short=(none)" in brief_idle, brief_idle
        assert "sig8=(none)" in brief_idle, brief_idle
        assert "last_age_s=0" in brief_idle, brief_idle
        return {
            "health_alert_in_alert": payload.get("health_alert_in_alert"),
            "health_alert_cooldown_left_sec": left,
            "health_alert_sig_short": sig_short,
            "health_alert_sig_hash8": sig_hash8,
            "health_alert_last_age_sec": last_age,
            "idle_health_alert_last_age_sec": payload_idle.get("health_alert_last_age_sec"),
            "idle_health_alert_sig_short": payload_idle.get("health_alert_sig_short"),
            "idle_health_alert_sig_hash8": payload_idle.get("health_alert_sig_hash8"),
            "brief_health_line": [ln for ln in brief.splitlines() if ln.startswith("health ")][0],
        }
    finally:
        health_path.unlink(missing_ok=True)
        alert_path.unlink(missing_ok=True)


def check_status_brief_alert_transitions_signal() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-brief-alert-transitions-") as td:
        state_path = Path(td) / "reply_metrics_state.json"
        cfg2 = cfg.__class__(**{**cfg.__dict__, "reply_api_metrics_state_path": state_path})
        relay = bot.TmuxRelay(cfg2)
        piper = bot.PiperTTS(cfg2)
        reply_api = bot.LocalReplyAPI(cfg2, bot.LastChatState())

        reply_api._track_alert_transition(True)
        reply_api._track_alert_transition(False)
        reply_api._track_alert_transition(True)

        payload = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload.get("reply_alert_transitions_total") == 2, payload

        brief = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "alert_transitions=2" in brief, brief
        health_line = [ln for ln in brief.splitlines() if ln.startswith("health ")][0]
        assert "alert_transitions=2" in health_line, health_line
        return {
            "reply_alert_transitions_total": payload.get("reply_alert_transitions_total"),
            "brief_health_line": health_line,
        }


def check_status_brief_queue_pressure_signal() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-brief-queue-pressure-") as td:
        td_path = Path(td)
        log_path = td_path / "bot.jsonl"
        state_path = td_path / "reply_metrics_state.json"
        health_path = td_path / "health_status.json"
        alert_path = td_path / "health_alert_state.json"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        health_path.write_text(
            json.dumps(
                {
                    "ts_utc": now,
                    "ok": True,
                    "fail_count": 0,
                    "fails": [],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        alert_path.write_text(
            json.dumps(
                {
                    "last_alert_epoch": 0,
                    "in_alert": False,
                    "last_severity": "none",
                    "last_fingerprint": "",
                    "cooldown_sec": 900,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        rec_warn = {"ts_utc": now, "event_type": "reply_queue_full", "event_fields": {"queue_depth": 500}}
        rec_ok = {"ts_utc": now, "event_type": "command_received", "event_fields": {"cmd": "/ping"}}
        log_path.write_text(
            json.dumps(rec_warn, ensure_ascii=False) + "\n" + json.dumps(rec_ok, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "json_log_path": log_path,
                "status_event_lookback_min": 15,
                "status_event_scan_max_bytes": 262_144,
                "status_queue_pressure_critical_threshold": 3,
                "reply_api_metrics_state_path": state_path,
                "health_status_path": health_path,
                "health_alert_state_path": alert_path,
            }
        )
        relay = bot.TmuxRelay(cfg2)
        piper = bot.PiperTTS(cfg2)
        reply_api = bot.LocalReplyAPI(cfg2, bot.LastChatState())

        payload_warn = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_warn.get("queue_pressure_state") == "warn", payload_warn
        assert payload_warn.get("queue_pressure_count") == 1, payload_warn
        assert payload_warn.get("queue_pressure_threshold") == 3, payload_warn
        assert "queue_full" in (payload_warn.get("queue_pressure_reasons") or []), payload_warn
        assert payload_warn.get("queue_pressure_operator_hint_code") == "queue_pressure_warn_monitor", payload_warn
        assert payload_warn.get("health_operator_hint_code") == "ok_none", payload_warn
        brief_warn = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "pressure=warn" in brief_warn, brief_warn
        assert "pc=1/3" in brief_warn, brief_warn

        log_path.write_text(json.dumps(rec_ok, ensure_ascii=False) + "\n", encoding="utf-8")
        payload_ok = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_ok.get("queue_pressure_state") == "ok", payload_ok
        assert payload_ok.get("queue_pressure_count") == 0, payload_ok
        assert payload_ok.get("queue_pressure_reasons") == [], payload_ok
        assert payload_ok.get("queue_pressure_operator_hint_code") == "queue_pressure_none", payload_ok
        assert payload_ok.get("health_operator_hint_code") == "ok_none", payload_ok
        brief_ok = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "pressure=ok" in brief_ok, brief_ok
        assert "pc=0/3" in brief_ok, brief_ok

        rec_crit1 = {"ts_utc": now, "event_type": "reply_queue_full", "event_fields": {"queue_depth": 500}}
        rec_crit2 = {"ts_utc": now, "event_type": "reply_queue_full", "event_fields": {"queue_depth": 500}}
        rec_crit3 = {"ts_utc": now, "event_type": "reply_queue_drop_oldest", "event_fields": {"queue_depth": 500}}
        log_path.write_text(
            "\n".join(
                [
                    json.dumps(rec_crit1, ensure_ascii=False),
                    json.dumps(rec_crit2, ensure_ascii=False),
                    json.dumps(rec_crit3, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        payload_crit = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_crit.get("queue_pressure_state") == "critical", payload_crit
        assert payload_crit.get("queue_pressure_count") == 3, payload_crit
        assert (
            payload_crit.get("queue_pressure_operator_hint_code") == "queue_pressure_critical_check_backpressure"
        ), payload_crit
        assert payload_crit.get("health_operator_hint_code") == "queue_pressure_critical_check_backpressure", payload_crit
        status_code = bot._runtime_status_code_view(payload_crit, with_schema=True)
        assert status_code.get("state") == "ok", status_code
        assert status_code.get("hint_code") == "queue_pressure_critical_check_backpressure", status_code
        brief_crit = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "pressure=critical" in brief_crit, brief_crit
        assert "pc=3/3" in brief_crit, brief_crit

        return {
            "warn_queue_pressure_state": payload_warn.get("queue_pressure_state"),
            "warn_hint_code": payload_warn.get("health_operator_hint_code"),
            "warn_queue_pressure_reasons": payload_warn.get("queue_pressure_reasons"),
            "ok_queue_pressure_state": payload_ok.get("queue_pressure_state"),
            "ok_hint_code": payload_ok.get("health_operator_hint_code"),
            "critical_queue_pressure_state": payload_crit.get("queue_pressure_state"),
            "critical_hint_code": payload_crit.get("health_operator_hint_code"),
            "brief_reply_line_warn": [ln for ln in brief_warn.splitlines() if ln.startswith("reply ")][0],
            "brief_reply_line_ok": [ln for ln in brief_ok.splitlines() if ln.startswith("reply ")][0],
            "brief_reply_line_critical": [ln for ln in brief_crit.splitlines() if ln.startswith("reply ")][0],
        }


def check_status_brief_quick_lane_signal() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-brief-quick-lane-") as td:
        td_path = Path(td)
        health_path = td_path / "health_status.json"
        state_path = td_path / "reply_metrics_state.json"
        self_test_path = td_path / "self_test_latest.json"
        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        self_test_path.write_text(
            json.dumps(
                {
                    "ts_utc": now_ts,
                    "ok": True,
                    "duration_ms": 1,
                    "failures": [],
                    "checks": [{"name": "stub", "ok": True}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def write_health(required: bool, exists: bool, ok: bool, stale: bool, *, health_ok: bool) -> None:
            sev = "none" if health_ok else "critical"
            payload = {
                "ts_utc": now_ts,
                "ok": health_ok,
                "fail_count": 0 if health_ok else 1,
                "fails": [] if health_ok else ["self_test_quick:missing"],
                "alert": {"severity": sev, "cooldown_sec_effective": 0},
                "self_test_quick": {
                    "required": required,
                    "path": str(td_path / "self_test_quick_latest.json"),
                    "max_age_sec": 1800,
                    "exists": exists,
                    "ok": ok,
                    "stale": stale,
                    "age_sec": 33,
                    "ts_utc": now_ts,
                    "failed_checks": [] if ok else ["quick_stub_fail"],
                },
            }
            health_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "health_status_path": health_path,
                "self_test_status_path": self_test_path,
                "reply_api_metrics_state_path": state_path,
            }
        )
        relay = bot.TmuxRelay(cfg2)
        piper = bot.PiperTTS(cfg2)
        reply_api = bot.LocalReplyAPI(cfg2, bot.LastChatState())

        write_health(required=True, exists=False, ok=False, stale=False, health_ok=False)
        payload_missing = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_missing.get("health_self_test_quick_state") == "missing", payload_missing
        assert payload_missing.get("health_operator_hint_code") == "critical_check_self_test_quick_lane", payload_missing
        brief_missing = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "quick_lane=missing" in brief_missing, brief_missing
        status_code_missing = bot._runtime_status_code_view(payload_missing, with_schema=True)
        assert status_code_missing.get("quick_lane") == "missing", status_code_missing

        write_health(required=True, exists=True, ok=True, stale=False, health_ok=True)
        payload_ok = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_ok.get("health_self_test_quick_state") == "ok", payload_ok
        brief_ok = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "quick_lane=ok" in brief_ok, brief_ok
        quick_line_ok = [ln for ln in brief_ok.splitlines() if ln.startswith("self_test_quick ")][0]
        assert "state=ok" in quick_line_ok, quick_line_ok
        status_code_ok = bot._runtime_status_code_view(payload_ok, with_schema=True)
        assert status_code_ok.get("quick_lane") == "ok", status_code_ok

        write_health(required=False, exists=False, ok=False, stale=False, health_ok=True)
        payload_disabled = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_disabled.get("health_self_test_quick_state") == "disabled", payload_disabled
        brief_disabled = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "quick_lane=disabled" in brief_disabled, brief_disabled
        status_code_disabled = bot._runtime_status_code_view(payload_disabled, with_schema=True)
        assert status_code_disabled.get("quick_lane") == "disabled", status_code_disabled

        return {
            "missing_state": payload_missing.get("health_self_test_quick_state"),
            "missing_hint_code": payload_missing.get("health_operator_hint_code"),
            "ok_state": payload_ok.get("health_self_test_quick_state"),
            "disabled_state": payload_disabled.get("health_self_test_quick_state"),
            "status_code_missing_quick_lane": status_code_missing.get("quick_lane"),
            "status_code_ok_quick_lane": status_code_ok.get("quick_lane"),
            "status_code_disabled_quick_lane": status_code_disabled.get("quick_lane"),
            "brief_health_line_ok": [ln for ln in brief_ok.splitlines() if ln.startswith("health ")][0],
            "brief_quick_line_ok": quick_line_ok,
        }


def check_status_brief_reply_auth_probe_signal() -> dict[str, object]:
    cfg = bot.load_config()
    with tempfile.TemporaryDirectory(prefix="yuuki-brief-reply-auth-probe-") as td:
        td_path = Path(td)
        health_path = td_path / "health_status.json"
        state_path = td_path / "reply_metrics_state.json"
        self_test_path = td_path / "self_test_latest.json"
        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        self_test_path.write_text(
            json.dumps(
                {
                    "ts_utc": now_ts,
                    "ok": True,
                    "duration_ms": 1,
                    "failures": [],
                    "checks": [{"name": "stub", "ok": True}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def write_health(*, health_ok: bool, fail_reason, probe_enabled: bool, probe_ok: bool, probe_http: str) -> None:
            fails = [fail_reason] if fail_reason else []
            sev = "none" if health_ok else "critical"
            payload = {
                "ts_utc": now_ts,
                "ok": health_ok,
                "fail_count": len(fails),
                "fails": fails,
                "alert": {"severity": sev, "cooldown_sec_effective": 0},
                "reply_api": {
                    "auth_probe_enabled": probe_enabled,
                    "auth_probe_ok": probe_ok,
                    "auth_probe_http": probe_http,
                },
            }
            health_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        cfg2 = cfg.__class__(
            **{
                **cfg.__dict__,
                "health_status_path": health_path,
                "self_test_status_path": self_test_path,
                "reply_api_metrics_state_path": state_path,
            }
        )
        relay = bot.TmuxRelay(cfg2)
        piper = bot.PiperTTS(cfg2)
        reply_api = bot.LocalReplyAPI(cfg2, bot.LastChatState())

        write_health(
            health_ok=False,
            fail_reason="reply_api_auth_probe=http_401",
            probe_enabled=True,
            probe_ok=False,
            probe_http="401",
        )
        payload_fail = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_fail.get("health_reply_auth_probe_state") == "fail", payload_fail
        assert payload_fail.get("health_reply_auth_probe_http") == "401", payload_fail
        assert payload_fail.get("health_operator_hint_code") == "critical_check_reply_api_auth_probe", payload_fail
        brief_fail = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "reply_auth=fail:401" in brief_fail, brief_fail

        write_health(
            health_ok=True,
            fail_reason=None,
            probe_enabled=True,
            probe_ok=True,
            probe_http="400",
        )
        payload_ok = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_ok.get("health_reply_auth_probe_state") == "ok", payload_ok
        assert payload_ok.get("health_reply_auth_probe_http") == "400", payload_ok
        brief_ok = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "reply_auth=ok:400" in brief_ok, brief_ok

        write_health(
            health_ok=True,
            fail_reason=None,
            probe_enabled=False,
            probe_ok=False,
            probe_http="-",
        )
        payload_disabled = bot._runtime_status_payload(cfg2, relay, piper, reply_api)
        assert payload_disabled.get("health_reply_auth_probe_state") == "disabled", payload_disabled
        brief_disabled = bot._format_runtime_status_brief(cfg2, relay, piper, reply_api)
        assert "reply_auth=disabled:-" in brief_disabled, brief_disabled

        return {
            "fail_state": payload_fail.get("health_reply_auth_probe_state"),
            "fail_hint_code": payload_fail.get("health_operator_hint_code"),
            "ok_state": payload_ok.get("health_reply_auth_probe_state"),
            "disabled_state": payload_disabled.get("health_reply_auth_probe_state"),
            "brief_health_line_fail": [ln for ln in brief_fail.splitlines() if ln.startswith("health ")][0],
            "brief_health_line_ok": [ln for ln in brief_ok.splitlines() if ln.startswith("health ")][0],
        }


def check_health_self_test_autorepair() -> dict[str, object]:
    import subprocess

    with tempfile.TemporaryDirectory(prefix="yuuki-health-autorepair-") as td:
        tmp = Path(td)
        env_path = tmp / "test.env"
        health_status_path = tmp / "health_status.json"
        alert_state_path = tmp / "alert_state.json"
        self_test_status_path = tmp / "self_test_latest.json"
        json_log_path = tmp / "bot.jsonl"
        marker_path = tmp / "autorepair.marker"
        autorepair_script = tmp / "autorepair_stub.sh"

        json_log_path.write_text("", encoding="utf-8")
        autorepair_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    f"SELF_TEST_STATUS_PATH={str(self_test_status_path)!r}",
                    f"MARKER_PATH={str(marker_path)!r}",
                    "ts_utc=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"",
                    "cat > \"$SELF_TEST_STATUS_PATH\" <<JSON",
                    "{",
                    "  \"ts_utc\": \"$ts_utc\",",
                    "  \"ok\": true,",
                    "  \"duration_ms\": 1,",
                    "  \"failures\": [],",
                    "  \"checks\": [{\"name\": \"autorepair_stub\", \"ok\": true, \"details\": {\"source\": \"stub\"}}]",
                    "}",
                    "JSON",
                    "echo ok > \"$MARKER_PATH\"",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        autorepair_script.chmod(0o755)

        env_path.write_text(
            "\n".join(
                [
                    "TELEGRAM_LOCAL_REPLY_API_HOST=127.0.0.1",
                    "TELEGRAM_LOCAL_REPLY_API_PORT=8788",
                    "TELEGRAM_LOCAL_REPLY_API_TOKEN=",
                    "TELEGRAM_ALLOWED_CHAT_IDS=",
                    "TELEGRAM_HEALTH_ALERT_CHAT_ID=",
                    f"TELEGRAM_HEALTH_STATUS_PATH={health_status_path}",
                    f"TELEGRAM_JSON_LOG_PATH={json_log_path}",
                    f"TELEGRAM_HEALTH_ALERT_STATE_PATH={alert_state_path}",
                    f"YUUKI_SELF_TEST_STATUS_PATH={self_test_status_path}",
                    "TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC=7200",
                    "TELEGRAM_HEALTH_SELF_TEST_FAIL_STREAK_THRESHOLD=3",
                    "TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_ENABLED=true",
                    "TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_TIMEOUT_SEC=20",
                    f"TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_SCRIPT={autorepair_script}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        health_check_script = Path("/home/foggen/kitan-telegram/scripts/health_check.sh")
        env = os.environ.copy()
        env["TELEGRAM_HEALTH_ENV_FILE"] = str(env_path)
        proc = subprocess.run(
            ["bash", str(health_check_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            text=True,
            check=False,
            env=env,
        )
        if not health_status_path.exists():
            raise RuntimeError(
                f"health status not created rc={proc.returncode} out={proc.stdout[:200]} err={proc.stderr[:200]}"
            )

        payload = json.loads(health_status_path.read_text(encoding="utf-8"))
        st = payload.get("self_test") if isinstance(payload, dict) else None
        if not isinstance(st, dict):
            raise RuntimeError(f"missing self_test payload: {payload}")

        assert marker_path.exists(), payload
        assert st.get("autorepair_enabled") is True, payload
        assert st.get("autorepair_attempted") is True, payload
        assert st.get("autorepair_result") == "recovered", payload
        assert st.get("exists") is True, payload
        assert st.get("ok") is True, payload
        assert st.get("stale") is False, payload
        fails = payload.get("fails")
        if isinstance(fails, list):
            assert not any(str(x).startswith("self_test:") for x in fails), payload

        return {
            "health_check_rc": proc.returncode,
            "autorepair_result": st.get("autorepair_result"),
            "self_test_exists": st.get("exists"),
            "self_test_ok": st.get("ok"),
            "self_test_stale": st.get("stale"),
            "fail_count": payload.get("fail_count"),
        }


def check_health_queue_pressure_alert() -> dict[str, object]:
    import subprocess

    with tempfile.TemporaryDirectory(prefix="yuuki-health-queue-pressure-") as td:
        tmp = Path(td)
        env_path = tmp / "test.env"
        health_status_path = tmp / "health_status.json"
        alert_state_path = tmp / "alert_state.json"
        self_test_status_path = tmp / "self_test_latest.json"
        json_log_path = tmp / "bot.jsonl"

        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        json_log_path.write_text(
            "\n".join(
                [
                    json.dumps({"ts_utc": now_ts, "event_type": "reply_queue_full", "event_fields": {"i": 1}}),
                    json.dumps({"ts_utc": now_ts, "event_type": "reply_queue_drop_oldest", "event_fields": {"i": 2}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self_test_status_path.write_text(
            json.dumps(
                {
                    "ts_utc": now_ts,
                    "ok": True,
                    "duration_ms": 1,
                    "failures": [],
                    "checks": [{"name": "stub", "ok": True, "details": {"source": "queue-pressure-test"}}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        env_path.write_text(
            "\n".join(
                [
                    "TELEGRAM_LOCAL_REPLY_API_HOST=127.0.0.1",
                    "TELEGRAM_LOCAL_REPLY_API_PORT=8788",
                    "TELEGRAM_LOCAL_REPLY_API_TOKEN=",
                    "TELEGRAM_ALLOWED_CHAT_IDS=",
                    "TELEGRAM_HEALTH_ALERT_CHAT_ID=",
                    "TELEGRAM_TMUX_TARGET_PANE=",
                    f"TELEGRAM_HEALTH_STATUS_PATH={health_status_path}",
                    f"TELEGRAM_JSON_LOG_PATH={json_log_path}",
                    f"TELEGRAM_HEALTH_ALERT_STATE_PATH={alert_state_path}",
                    f"YUUKI_SELF_TEST_STATUS_PATH={self_test_status_path}",
                    "TELEGRAM_ALERT_EVENT_LOOKBACK_MIN=15",
                    "TELEGRAM_ALERT_EVENT_SCAN_MAX_BYTES=1048576",
                    "TELEGRAM_ALERT_REPLY_FAILED_THRESHOLD=999",
                    "TELEGRAM_ALERT_RELAY_ERROR_THRESHOLD=999",
                    "TELEGRAM_ALERT_QUEUE_PRESSURE_THRESHOLD=2",
                    "TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC=7200",
                    "TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_ENABLED=false",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        health_check_script = Path("/home/foggen/kitan-telegram/scripts/health_check.sh")
        env = os.environ.copy()
        env["TELEGRAM_HEALTH_ENV_FILE"] = str(env_path)
        proc = subprocess.run(
            ["bash", str(health_check_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            text=True,
            check=False,
            env=env,
        )
        if not health_status_path.exists():
            raise RuntimeError(
                f"health status not created rc={proc.returncode} out={proc.stdout[:200]} err={proc.stderr[:200]}"
            )

        payload = json.loads(health_status_path.read_text(encoding="utf-8"))
        fails = payload.get("fails") if isinstance(payload, dict) else None
        if not isinstance(fails, list):
            raise RuntimeError(f"missing fails list: {payload}")
        assert proc.returncode == 1, proc.returncode
        assert any(str(x).startswith("event:queue_pressure=2>=2") for x in fails), payload
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, dict):
            raise RuntimeError(f"missing events payload: {payload}")
        assert events.get("reply_queue_full") == 1, payload
        assert events.get("reply_queue_drop_oldest") == 1, payload
        assert events.get("queue_pressure") == 2, payload
        assert events.get("queue_pressure_threshold") == 2, payload
        alert = payload.get("alert") if isinstance(payload, dict) else None
        if not isinstance(alert, dict):
            raise RuntimeError(f"missing alert payload: {payload}")
        assert alert.get("severity") == "critical", payload
        return {
            "health_check_rc": proc.returncode,
            "queue_pressure_fail": [x for x in fails if str(x).startswith("event:queue_pressure=")],
            "events": events,
            "alert_severity": alert.get("severity"),
        }


def check_health_quick_self_test_gate() -> dict[str, object]:
    import subprocess

    with tempfile.TemporaryDirectory(prefix="yuuki-health-quick-self-test-") as td:
        tmp = Path(td)
        env_path = tmp / "test.env"
        health_status_path = tmp / "health_status.json"
        alert_state_path = tmp / "alert_state.json"
        self_test_status_path = tmp / "self_test_latest.json"
        self_test_quick_path = tmp / "self_test_quick_latest.json"
        json_log_path = tmp / "bot.jsonl"

        now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        json_log_path.write_text(
            json.dumps({"ts_utc": now_ts, "event_type": "command_received", "event_fields": {"src": "quick-gate"}})
            + "\n",
            encoding="utf-8",
        )
        self_test_status_path.write_text(
            json.dumps(
                {
                    "ts_utc": now_ts,
                    "ok": True,
                    "duration_ms": 1,
                    "failures": [],
                    "checks": [{"name": "stub", "ok": True, "details": {"source": "quick-gate-full"}}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        env_path.write_text(
            "\n".join(
                [
                    "TELEGRAM_LOCAL_REPLY_API_HOST=127.0.0.1",
                    "TELEGRAM_LOCAL_REPLY_API_PORT=8788",
                    "TELEGRAM_LOCAL_REPLY_API_TOKEN=",
                    "TELEGRAM_ALLOWED_CHAT_IDS=",
                    "TELEGRAM_HEALTH_ALERT_CHAT_ID=",
                    "TELEGRAM_TMUX_TARGET_PANE=",
                    f"TELEGRAM_HEALTH_STATUS_PATH={health_status_path}",
                    f"TELEGRAM_JSON_LOG_PATH={json_log_path}",
                    f"TELEGRAM_HEALTH_ALERT_STATE_PATH={alert_state_path}",
                    f"YUUKI_SELF_TEST_STATUS_PATH={self_test_status_path}",
                    f"YUUKI_SELF_TEST_QUICK_STATUS_PATH={self_test_quick_path}",
                    "TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC=7200",
                    "TELEGRAM_HEALTH_REQUIRE_QUICK_SELF_TEST=true",
                    "TELEGRAM_HEALTH_QUICK_SELF_TEST_MAX_AGE_SEC=1800",
                    "TELEGRAM_HEALTH_SELF_TEST_AUTOREPAIR_ENABLED=false",
                    "TELEGRAM_ALERT_REPLY_FAILED_THRESHOLD=999",
                    "TELEGRAM_ALERT_RELAY_ERROR_THRESHOLD=999",
                    "TELEGRAM_ALERT_QUEUE_PRESSURE_THRESHOLD=999",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        health_check_script = Path("/home/foggen/kitan-telegram/scripts/health_check.sh")
        env = os.environ.copy()
        env["TELEGRAM_HEALTH_ENV_FILE"] = str(env_path)

        proc1 = subprocess.run(
            ["bash", str(health_check_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            text=True,
            check=False,
            env=env,
        )
        if not health_status_path.exists():
            raise RuntimeError(
                f"health status not created (run1) rc={proc1.returncode} out={proc1.stdout[:200]} err={proc1.stderr[:200]}"
            )
        payload1 = json.loads(health_status_path.read_text(encoding="utf-8"))
        fails1 = payload1.get("fails") if isinstance(payload1, dict) else None
        if not isinstance(fails1, list):
            raise RuntimeError(f"missing fails list (run1): {payload1}")
        assert proc1.returncode == 1, proc1.returncode
        assert any(str(x).startswith("self_test_quick:missing") for x in fails1), payload1

        self_test_quick_path.write_text(
            json.dumps(
                {
                    "ts_utc": now_ts,
                    "ok": True,
                    "duration_ms": 1,
                    "failures": [],
                    "checks": [{"name": "stub_quick", "ok": True, "details": {"source": "quick-gate-quick"}}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        proc2 = subprocess.run(
            ["bash", str(health_check_script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            text=True,
            check=False,
            env=env,
        )
        if not health_status_path.exists():
            raise RuntimeError(
                f"health status not created (run2) rc={proc2.returncode} out={proc2.stdout[:200]} err={proc2.stderr[:200]}"
            )
        payload2 = json.loads(health_status_path.read_text(encoding="utf-8"))
        fails2 = payload2.get("fails") if isinstance(payload2, dict) else None
        if not isinstance(fails2, list):
            raise RuntimeError(f"missing fails list (run2): {payload2}")
        assert not any(str(x).startswith("self_test_quick:") for x in fails2), payload2
        stq = payload2.get("self_test_quick") if isinstance(payload2, dict) else None
        if not isinstance(stq, dict):
            raise RuntimeError(f"missing self_test_quick payload: {payload2}")
        assert stq.get("required") is True, payload2
        assert stq.get("exists") is True, payload2
        assert stq.get("ok") is True, payload2
        assert stq.get("stale") is False, payload2

        return {
            "run1_rc": proc1.returncode,
            "run1_quick_fail": [x for x in fails1 if str(x).startswith("self_test_quick:")],
            "run2_rc": proc2.returncode,
            "run2_fails": fails2,
            "run2_quick_state": stq,
        }


def check_self_improve_notify_transport() -> dict[str, object]:
    import http.server
    import shutil
    import socketserver
    import subprocess
    import threading

    with tempfile.TemporaryDirectory(prefix="yuuki-self-improve-notify-") as td:
        tmp = Path(td)
        script_src = Path("/home/foggen/kitan-telegram/scripts/self_improve_nudge.sh")
        script_copy = tmp / "self_improve_nudge_test.sh"
        shutil.copyfile(script_src, script_copy)
        script_copy.chmod(0o755)

        raw = script_copy.read_text(encoding="utf-8")
        raw = raw.replace('ENV_FILE="/home/foggen/kitan-telegram/.env"', 'ENV_FILE="/tmp/yuuki_noenv"')
        script_copy.write_text(raw, encoding="utf-8")

        records: list[dict[str, object]] = []

        class ReplyHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                records.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization") or "",
                        "body": body.decode("utf-8", errors="replace"),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, fmt, *args):  # noqa: A003
                return

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = ThreadedTCPServer(("127.0.0.1", 0), ReplyHandler)
        port = int(server.server_address[1])
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            state_path = tmp / "state.json"
            env = os.environ.copy()
            env.update(
                {
                    "YUUKI_SELF_IMPROVE_ENABLED": "true",
                    "YUUKI_SELF_IMPROVE_TARGET_PANE": "no:such",
                    "YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY": "false",
                    "YUUKI_SELF_IMPROVE_MIN_INTERVAL_SEC": "0",
                    "YUUKI_SELF_IMPROVE_STATE_PATH": str(state_path),
                    "YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM": "true",
                    "YUUKI_SELF_IMPROVE_NOTIFY_CHAT_ID": "1607870670",
                    "YUUKI_SELF_IMPROVE_NOTIFY_ON_SKIP": "true",
                    "TELEGRAM_LOCAL_REPLY_API_HOST": "127.0.0.1",
                    "TELEGRAM_LOCAL_REPLY_API_PORT": str(port),
                    "TELEGRAM_LOCAL_REPLY_API_TOKEN": "dummy-token",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "",
                    "TELEGRAM_HEALTH_ALERT_CHAT_ID": "",
                }
            )
            proc = subprocess.run(
                ["bash", str(script_copy)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=40,
                text=True,
                check=False,
                env=env,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"self_improve_nudge failed rc={proc.returncode} out={proc.stdout[:240]} err={proc.stderr[:240]}"
                )
            if "notify_sent=true" not in (proc.stdout or ""):
                raise RuntimeError(f"notify success marker missing in stdout: {proc.stdout[:240]}")
            if not state_path.exists():
                raise RuntimeError("state file missing after self_improve_nudge test")
            st = json.loads(state_path.read_text(encoding="utf-8"))
            assert st.get("notify_sent") is True, st
            assert st.get("notify_reason") == "notify_sent", st
            assert str(st.get("last_reason") or "").startswith("pane_missing:"), st
            assert records, "mock /reply did not receive request"
            req = records[0]
            assert req.get("path") == "/reply", req
            assert req.get("authorization") == "Bearer dummy-token", req
            body = json.loads(str(req.get("body") or "{}"))
            assert body.get("chat_id") == 1607870670, body
            text = str(body.get("text") or "")
            assert "Done: self-improve nudge cycle executed." in text, body
            return {
                "stdout": (proc.stdout or "").strip(),
                "notify_reason": st.get("notify_reason"),
                "request_path": req.get("path"),
                "chat_id": body.get("chat_id"),
            }
        finally:
            server.shutdown()
            server.server_close()


check_specs: list[tuple[str, object]] = [
    ("text_deduper", check_text_deduper),
    ("queue_drop_oldest", check_queue_drop_oldest),
    ("queue_reject", check_queue_reject),
    ("event_scan_fallback", check_event_scan_fallback),
    ("autonomy_health_gates_smoke", check_autonomy_health_gates_smoke),
    ("monitoring_config_smoke", check_monitoring_config_smoke),
    ("status_self_test_observability", check_status_self_test_observability),
    ("status_health_self_test_repeated_signal", check_status_health_self_test_repeated_signal),
    ("status_health_self_test_autorepair_signal", check_status_health_self_test_autorepair_signal),
    ("status_health_operator_hint_autorepair", check_status_health_operator_hint_autorepair),
    ("local_reply_api_status_code_provider", check_local_reply_api_status_code_provider),
    ("local_reply_api_metrics_prometheus", check_local_reply_api_metrics_prometheus),
    ("local_reply_api_alert_transition_persistence", check_local_reply_api_alert_transition_persistence),
    ("local_reply_api_auth_modes", check_local_reply_api_auth_modes),
    ("status_health_fails_top_signal", check_status_health_fails_top_signal),
    ("status_alert_cooldown_left_signal", check_status_alert_cooldown_left_signal),
    ("status_brief_alert_transitions_signal", check_status_brief_alert_transitions_signal),
    ("status_brief_queue_pressure_signal", check_status_brief_queue_pressure_signal),
    ("status_brief_quick_lane_signal", check_status_brief_quick_lane_signal),
    ("status_brief_reply_auth_probe_signal", check_status_brief_reply_auth_probe_signal),
    ("health_queue_pressure_alert", check_health_queue_pressure_alert),
    ("health_quick_self_test_gate", check_health_quick_self_test_gate),
    ("self_improve_notify_transport", check_self_improve_notify_transport),
    ("health_self_test_autorepair", check_health_self_test_autorepair),
]

quick_check_names = {
    "text_deduper",
    "queue_drop_oldest",
    "queue_reject",
    "autonomy_health_gates_smoke",
    "monitoring_config_smoke",
    "local_reply_api_status_code_provider",
    "local_reply_api_metrics_prometheus",
    "local_reply_api_auth_modes",
    "status_brief_queue_pressure_signal",
    "status_brief_quick_lane_signal",
    "status_brief_reply_auth_probe_signal",
    "health_queue_pressure_alert",
    "health_quick_self_test_gate",
    "self_improve_notify_transport",
}

available_names = {name for name, _ in check_specs}
unknown_requested = sorted(name for name in requested_check_set if name not in available_names)
if unknown_requested:
    record(
        "requested_checks_valid",
        False,
        {
            "unknown": unknown_requested,
            "available_count": len(available_names),
        },
    )

for check_name, fn in check_specs:
    if requested_check_set:
        should_run = check_name in requested_check_set
    elif profile_raw == "quick":
        should_run = check_name in quick_check_names
    else:
        should_run = True
    if should_run:
        run_check(check_name, fn)
    else:
        skipped_checks.append(check_name)

elapsed_ms = int((time.perf_counter() - started) * 1000)
out = {
    "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "ok": len(failures) == 0,
    "duration_ms": elapsed_ms,
    "profile": profile_raw,
    "requested_checks": requested_checks,
    "executed_checks": [c.get("name") for c in checks],
    "skipped_checks": skipped_checks,
    "failures": failures,
    "checks": checks,
}
status_path.parent.mkdir(parents=True, exist_ok=True)
status_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

print(
    f"self-test ok={out['ok']} profile={profile_raw} duration_ms={elapsed_ms} "
    f"checks={len(checks)} failures={len(failures)}"
)
if failures:
    raise SystemExit(1)
PY

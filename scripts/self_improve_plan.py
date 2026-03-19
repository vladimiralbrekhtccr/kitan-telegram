#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _age_sec(ts: str | None) -> int | None:
    if not ts:
        return None
    parsed = _parse_iso(ts)
    if parsed is None:
        return None
    age = int((datetime.now(timezone.utc) - parsed).total_seconds())
    return max(0, age)


def _derive_health_state(snapshot: dict[str, Any], stale_threshold_sec: int) -> str:
    ts = str(snapshot.get("ts_utc") or "")
    age = _age_sec(ts)
    stale = (age is not None and age > stale_threshold_sec)
    health_ok = snapshot.get("ok")
    severity = "unknown"
    alert = snapshot.get("alert")
    if isinstance(alert, dict):
        severity = str(alert.get("severity") or "unknown").strip().lower()
    if severity == "unknown" and health_ok is True:
        severity = "none"
    if health_ok is True:
        return "stale" if stale else "ok"
    if health_ok is False:
        return "degraded" if severity == "warning" else "critical"
    return "stale" if stale else "unknown"


def _has_recent_decision(path: Path, within_hours: int) -> bool:
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    now = datetime.now(timezone.utc)
    for line in reversed(lines):
        s = line.strip()
        if not s.startswith("## "):
            continue
        # format expected: ## YYYY-MM-DD ...
        parts = s[3:].split()
        if not parts:
            return False
        dt = _parse_iso(parts[0] + "T00:00:00Z")
        if dt is None:
            return False
        return (now - dt).total_seconds() <= within_hours * 3600
    return False


@dataclass(frozen=True)
class PlanItem:
    task_id: str
    title: str
    reason: str
    action: str
    verify: str


def choose_plan(
    *,
    health: dict[str, Any],
    self_test: dict[str, Any],
    self_test_quick: dict[str, Any],
    stale_threshold_sec: int,
    quick_stale_threshold_sec: int,
    decisions_path: Path,
    core_smoke_timer_unit: Path,
) -> PlanItem:
    health_state = _derive_health_state(health, stale_threshold_sec)
    full_ok = bool(self_test.get("ok")) if self_test else False
    quick_ok = bool(self_test_quick.get("ok")) if self_test_quick else False
    quick_age = _age_sec(str(self_test_quick.get("ts_utc") or ""))
    quick_stale = quick_age is None or quick_age > quick_stale_threshold_sec

    if not self_test:
        return PlanItem(
            task_id="repair_full_self_test_missing",
            title="Восстановить full reliability self-test",
            reason="Отсутствует runtime/self_test_latest.json (нет source-of-truth по надежности).",
            action="Запусти ./scripts/self_test_reliability.sh, исправь первый failing check, обнови статус.",
            verify="Команда: ./scripts/self_test_reliability.sh; ожидается ok=True и failures=0.",
        )

    if not full_ok:
        failed = self_test.get("failures") or []
        failed_short = ", ".join(str(x) for x in failed[:2]) if isinstance(failed, list) and failed else "unknown"
        return PlanItem(
            task_id="repair_full_self_test_failed",
            title="Починить failing full self-test",
            reason=f"Full self-test сейчас fail (first={failed_short}).",
            action="Изолируй/почини failing check, затем перезапусти full self-test до green.",
            verify="Команда: ./scripts/self_test_reliability.sh; ожидается ok=True.",
        )

    if not self_test_quick or not quick_ok or quick_stale:
        return PlanItem(
            task_id="repair_quick_lane",
            title="Восстановить quick self-test lane",
            reason="Quick lane отсутствует/устарела/не green, а это критичный ранний сигнал.",
            action="Запусти ./scripts/self_test_transport_quick.sh и почини первый failing check при необходимости.",
            verify="Проверить runtime/self_test_quick_latest.json: ok=True и свежий ts_utc.",
        )

    if health_state != "ok":
        return PlanItem(
            task_id="stabilize_runtime_health",
            title="Вернуть health state в ok",
            reason=f"Текущий health_state={health_state}, это ограничивает автономный throughput.",
            action="Запусти ./scripts/health_check.sh, устрани первый fail из health_status.json, повтори.",
            verify="Проверить runtime/health_status.json: ok=true и alert.severity=none.",
        )

    if not core_smoke_timer_unit.exists():
        return PlanItem(
            task_id="install_core_smoke_timer",
            title="Добавить периодический core smoke таймер",
            reason="Нет systemd таймера для one-command core smoke suite.",
            action=(
                "Создай deploy/kitan-telegram-core-smoke.service + .timer, запускай "
                "./scripts/smoke_reliability_core.sh каждые 15-30 минут, логируй результат в runtime."
            ),
            verify="systemctl status kitan-telegram-core-smoke.timer и последний прогон PASS.",
        )

    if not _has_recent_decision(decisions_path, within_hours=24):
        return PlanItem(
            task_id="decision_log_freshness",
            title="Обновить DECISIONS.md по последнему архитектурному шагу",
            reason="Нет свежей decision-записи за последние 24 часа.",
            action="Добавь decision entry: context → decision → why → alternatives → acceptance criteria.",
            verify="Новая секция в DECISIONS.md с сегодняшней датой.",
        )

    return PlanItem(
        task_id="advance_wave2_router",
        title="Продвинуть Wave-2: project router",
        reason="Базовая надежность green, следующий системный рычаг — роутинг по проектам.",
        action="Реализуй /use <project> + auto-detect router и сохрани выбор проекта в runtime state.",
        verify="Smoke: /use переключает контекст и это видно в /statusjson.",
    )


def build_message(item: PlanItem) -> str:
    return (
        "Продолжай автономный импрувемент: выбери самый полезный следующий шаг и выполни.\n"
        f"Фокус: {item.task_id} — {item.title}.\n"
        f"Почему: {item.reason}\n"
        f"Сделай: {item.action}\n"
        f"Проверка: {item.verify}\n"
        "После этого отправь короткий отчёт в Telegram (Done/Changed/Status/Next)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Pick next high-value self-improvement step for Yuuki.")
    parser.add_argument(
        "--health-status-path",
        default="/home/foggen/kitan-telegram/runtime/health_status.json",
    )
    parser.add_argument(
        "--self-test-path",
        default="/home/foggen/kitan-telegram/runtime/self_test_latest.json",
    )
    parser.add_argument(
        "--self-test-quick-path",
        default="/home/foggen/kitan-telegram/runtime/self_test_quick_latest.json",
    )
    parser.add_argument(
        "--decisions-path",
        default="/home/foggen/AI_github/projects/yuuki-bot-upgrade/DECISIONS.md",
    )
    parser.add_argument(
        "--core-smoke-timer-unit",
        default="/home/foggen/kitan-telegram/deploy/kitan-telegram-core-smoke.timer",
    )
    parser.add_argument(
        "--health-stale-threshold-sec",
        type=int,
        default=1800,
    )
    parser.add_argument(
        "--quick-stale-threshold-sec",
        type=int,
        default=1800,
    )
    parser.add_argument(
        "--output-json",
        default="/home/foggen/kitan-telegram/runtime/self_improve_plan_latest.json",
    )
    args = parser.parse_args()

    health_path = Path(args.health_status_path).expanduser()
    self_test_path = Path(args.self_test_path).expanduser()
    self_test_quick_path = Path(args.self_test_quick_path).expanduser()
    decisions_path = Path(args.decisions_path).expanduser()
    timer_unit = Path(args.core_smoke_timer_unit).expanduser()
    output_json = Path(args.output_json).expanduser()

    health = _read_json(health_path)
    self_test = _read_json(self_test_path)
    self_test_quick = _read_json(self_test_quick_path)
    item = choose_plan(
        health=health,
        self_test=self_test,
        self_test_quick=self_test_quick,
        stale_threshold_sec=max(60, int(args.health_stale_threshold_sec)),
        quick_stale_threshold_sec=max(60, int(args.quick_stale_threshold_sec)),
        decisions_path=decisions_path,
        core_smoke_timer_unit=timer_unit,
    )
    message = build_message(item)

    payload = {
        "generated_at_utc": _now_iso(),
        "task_id": item.task_id,
        "title": item.title,
        "reason": item.reason,
        "action": item.action,
        "verify": item.verify,
        "message": message,
        "inputs": {
            "health_status_path": str(health_path),
            "self_test_path": str(self_test_path),
            "self_test_quick_path": str(self_test_quick_path),
            "decisions_path": str(decisions_path),
            "core_smoke_timer_unit": str(timer_unit),
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


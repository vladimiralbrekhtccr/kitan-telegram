#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

TMP_STATE="$(mktemp /tmp/yuuki-self-improve-state.XXXXXX.json)"
TMP_PLAN="$(mktemp /tmp/yuuki-self-improve-plan.XXXXXX.json)"
cleanup() {
  rm -f "${TMP_STATE}" "${TMP_PLAN}"
}
trap cleanup EXIT

MSG="$("${REPO_DIR}/scripts/self_improve_plan.py" --output-json "${TMP_PLAN}")"
if [[ -z "${MSG}" ]]; then
  echo "FAIL: planner returned empty message"
  exit 1
fi
if ! grep -q "Фокус:" <<<"${MSG}"; then
  echo "FAIL: planner message missing focus block"
  exit 1
fi

YUUKI_SELF_IMPROVE_ENABLED=true \
YUUKI_SELF_IMPROVE_REQUIRE_HEALTHY=false \
YUUKI_SELF_IMPROVE_TARGET_PANE="no:such" \
YUUKI_SELF_IMPROVE_AUTO_PLAN=true \
YUUKI_SELF_IMPROVE_STATE_PATH="${TMP_STATE}" \
YUUKI_SELF_IMPROVE_PLAN_OUTPUT_PATH="${TMP_PLAN}" \
YUUKI_SELF_IMPROVE_NOTIFY_TELEGRAM=false \
"${REPO_DIR}/scripts/self_improve_nudge.sh" >/dev/null

python3 - <<'PY' "${TMP_STATE}" "${TMP_PLAN}"
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
plan = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

if not state.get("auto_plan_enabled"):
    raise SystemExit("FAIL: auto_plan_enabled not set")
if not state.get("plan_used"):
    raise SystemExit(f"FAIL: planner not used: {state.get('plan_reason')}")
if not str(state.get("plan_task_id") or "").strip():
    raise SystemExit("FAIL: state missing plan_task_id")
if not str(plan.get("task_id") or "").strip():
    raise SystemExit("FAIL: plan output missing task_id")
print("PASS: self-improve planner smoke checks")
PY


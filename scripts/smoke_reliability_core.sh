#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

declare -a CHECKS=(
  "./scripts/smoke_reply_auth.sh"
  "./scripts/smoke_autonomy_health_gates.sh"
  "./scripts/smoke_self_improve_planner.sh"
  "./scripts/smoke_repo_hygiene.sh"
  "./scripts/smoke_telegram_multiline.sh"
  "./scripts/smoke_monitoring_config.sh"
  "./scripts/self_test_transport_quick.sh"
)

run_one() {
  local cmd="$1"
  local t0 t1 dt
  t0="$(date +%s)"
  echo "==> ${cmd}"
  bash -lc "${cmd}"
  t1="$(date +%s)"
  dt=$((t1 - t0))
  echo "PASS: ${cmd} (${dt}s)"
}

echo "Running reliability core smoke suite (${#CHECKS[@]} checks)"
for c in "${CHECKS[@]}"; do
  run_one "${c}"
done
echo "PASS: reliability core smoke suite"

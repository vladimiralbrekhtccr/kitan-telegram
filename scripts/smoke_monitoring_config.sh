#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

RULES_PATH="${REPO_DIR}/deploy/monitoring/kitan-telegram-alerts.yml"
AM_PATH="${REPO_DIR}/deploy/monitoring/alertmanager-kitan-example.yml"
REQUIRE_PROMTOOL="${YUUKI_MONITORING_REQUIRE_PROMTOOL:-false}"
REQUIRE_AMTOOL="${YUUKI_MONITORING_REQUIRE_AMTOOL:-false}"

is_true() {
  local v="${1:-}"
  v="${v,,}"
  [[ "$v" == "1" || "$v" == "true" || "$v" == "yes" || "$v" == "on" ]]
}

for p in "${RULES_PATH}" "${AM_PATH}"; do
  if [[ ! -f "${p}" ]]; then
    echo "FAIL: missing file: ${p}"
    exit 1
  fi
done

required_alerts=(
  "YuukiHealthCritical"
  "YuukiHealthStale"
  "YuukiQuickLaneStale"
  "YuukiQuickLaneMissingOrFailed"
  "YuukiReplyAuthProbeFail"
  "YuukiReplyQueueDropOldest"
  "YuukiReplyQueueReject"
  "YuukiReplyFailuresBurst"
  "YuukiReplyFailureRatioHigh"
)

for alert in "${required_alerts[@]}"; do
  if ! grep -Eq "^[[:space:]]*-[[:space:]]*alert:[[:space:]]*${alert}[[:space:]]*$" "${RULES_PATH}"; then
    echo "FAIL: missing required alert: ${alert}"
    exit 1
  fi
done

if ! grep -Eq "^[[:space:]]*-[[:space:]]*record:[[:space:]]*yuuki:reply_failure_ratio_10m[[:space:]]*$" "${RULES_PATH}"; then
  echo "FAIL: missing record rule yuuki:reply_failure_ratio_10m"
  exit 1
fi

if ! grep -Eq "^[[:space:]]*route:[[:space:]]*$" "${AM_PATH}"; then
  echo "FAIL: alertmanager config missing route mapping"
  exit 1
fi
if ! grep -Eq "^[[:space:]]*routes:[[:space:]]*$" "${AM_PATH}"; then
  echo "FAIL: alertmanager config missing routes[]"
  exit 1
fi
if ! grep -Eq 'alertname="YuukiReplyAuthProbeFail"' "${AM_PATH}"; then
  echo "FAIL: alertmanager config missing route for YuukiReplyAuthProbeFail"
  exit 1
fi
for receiver in "yuuki-warning" "yuuki-pager"; do
  if ! grep -Eq "^[[:space:]]*-[[:space:]]*name:[[:space:]]*${receiver}[[:space:]]*$" "${AM_PATH}"; then
    echo "FAIL: missing alertmanager receiver: ${receiver}"
    exit 1
  fi
done

echo "PASS: monitoring config smoke checks"

if command -v promtool >/dev/null 2>&1; then
  promtool check rules "${RULES_PATH}" >/dev/null
  echo "PASS: promtool rules validation"
else
  if is_true "${REQUIRE_PROMTOOL}"; then
    echo "FAIL: promtool is required but not installed"
    exit 1
  fi
  echo "SKIP: promtool not installed"
fi

if command -v amtool >/dev/null 2>&1; then
  amtool check-config "${AM_PATH}" >/dev/null
  echo "PASS: amtool config validation"
else
  if is_true "${REQUIRE_AMTOOL}"; then
    echo "FAIL: amtool is required but not installed"
    exit 1
  fi
  echo "SKIP: amtool not installed"
fi

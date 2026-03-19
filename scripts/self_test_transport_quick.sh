#!/usr/bin/env bash
set -euo pipefail

IN_PROFILE="${YUUKI_SELF_TEST_PROFILE-}"
IN_STATUS_PATH="${YUUKI_SELF_TEST_STATUS_PATH-}"
IN_ONLY="${YUUKI_SELF_TEST_ONLY-}"

ENV_FILE="/home/foggen/kitan-telegram/.env"
[[ -f "$ENV_FILE" ]] && set -a && source "$ENV_FILE" && set +a

if [[ -n "${IN_PROFILE}" ]]; then YUUKI_SELF_TEST_PROFILE="${IN_PROFILE}"; fi
if [[ -n "${IN_STATUS_PATH}" ]]; then YUUKI_SELF_TEST_STATUS_PATH="${IN_STATUS_PATH}"; fi
if [[ -n "${IN_ONLY}" ]]; then YUUKI_SELF_TEST_ONLY="${IN_ONLY}"; fi

# Force quick defaults regardless of .env, while still allowing external overrides.
export YUUKI_SELF_TEST_PROFILE="${IN_PROFILE:-quick}"
export YUUKI_SELF_TEST_STATUS_PATH="${IN_STATUS_PATH:-/home/foggen/kitan-telegram/runtime/self_test_quick_latest.json}"
export YUUKI_SELF_TEST_ONLY="${IN_ONLY:-}"

exec /home/foggen/kitan-telegram/scripts/self_test_reliability.sh

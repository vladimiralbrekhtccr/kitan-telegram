#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

ENV_FILE="/home/foggen/kitan-telegram/.env"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

STATUS_PATH="$(mktemp /tmp/yuuki-reply-auth-selftest.XXXXXX.json)"
cleanup() {
  rm -f "${STATUS_PATH}"
}
trap cleanup EXIT

export YUUKI_SELF_TEST_STATUS_PATH="${STATUS_PATH}"
export YUUKI_SELF_TEST_ONLY="local_reply_api_auth_modes"
export YUUKI_SELF_TEST_PROFILE="${YUUKI_SELF_TEST_PROFILE:-quick}"

/home/foggen/kitan-telegram/scripts/self_test_reliability.sh >/dev/null

python3 - <<'PY' "${STATUS_PATH}"
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
obj = json.loads(p.read_text(encoding="utf-8"))
if not bool(obj.get("ok")):
    raise SystemExit(f"FAIL: auth smoke failed: {obj.get('failures')}")
checks = obj.get("checks") or []
if len(checks) != 1:
    raise SystemExit(f"FAIL: expected one check, got {len(checks)}")
check = checks[0] if isinstance(checks[0], dict) else {}
if check.get("name") != "local_reply_api_auth_modes" or not bool(check.get("ok")):
    raise SystemExit(f"FAIL: unexpected check payload: {check}")
print("PASS: reply auth smoke checks")
PY

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

if ! command -v git >/dev/null 2>&1; then
  echo "FAIL: git is required"
  exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "FAIL: not inside a git repository"
  exit 1
fi

declare -a REQUIRED_GITIGNORE_LINES=(
  ".env"
  "models/"
  "vendor/"
  "logs/"
  "runtime/"
)

declare -a missing_rules=()
for rule in "${REQUIRED_GITIGNORE_LINES[@]}"; do
  if ! grep -qxF "${rule}" .gitignore; then
    missing_rules+=("${rule}")
  fi
done

if ((${#missing_rules[@]} > 0)); then
  printf 'FAIL: missing .gitignore rules: %s\n' "${missing_rules[*]}"
  exit 1
fi

declare -a tracked_blocked=()
while IFS= read -r path; do
  if [[ "${path}" == ".env.example" ]]; then
    continue
  fi
  case "${path}" in
    .env|.env.*|logs/*|runtime/*|models/*|vendor/*|__pycache__/*|*.pyc|*.pyo|.pytest_cache/*|.mypy_cache/*)
      tracked_blocked+=("${path}")
      ;;
  esac
done < <(git ls-files)

if ((${#tracked_blocked[@]} > 0)); then
  printf 'FAIL: blocked paths are tracked in git:\n'
  printf '  - %s\n' "${tracked_blocked[@]}"
  exit 1
fi

echo "PASS: repo hygiene smoke checks"

#!/usr/bin/env bash
set -euo pipefail

# Exchange helper: ask Gemini in tmux, then optionally forward answer to another tmux pane.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="${ROOT_DIR}/gemini_tmux_bridge.sh"
MAILBOX_DIR="${TMUX_GEMINI_MAILBOX_DIR:-/tmp/tmux-gemini-bridge}"
LAST_REPLY_FILE="${MAILBOX_DIR}/gemini_last_reply.txt"
LAST_PROMPT_FILE="${MAILBOX_DIR}/gemini_last_prompt.txt"

usage() {
  cat <<'EOF'
Usage:
  gemini_tmux_exchange.sh ask "<prompt>"
  gemini_tmux_exchange.sh forward "<prompt>" "<target_pane>"
  gemini_tmux_exchange.sh show

Examples:
  gemini_tmux_exchange.sh ask "Summarize today's key tasks in 4 bullets."
  gemini_tmux_exchange.sh forward "Give 3 ideas for viral hooks in RU." "main:0.0"
  gemini_tmux_exchange.sh show
EOF
}

ensure_ready() {
  mkdir -p "${MAILBOX_DIR}"
  if [[ ! -x "${BRIDGE}" ]]; then
    echo "bridge script not executable: ${BRIDGE}" >&2
    exit 1
  fi
}

ask_gemini() {
  local prompt="$1"
  local reply
  reply="$("${BRIDGE}" ask "${prompt}")"
  printf '%s\n' "${prompt}" > "${LAST_PROMPT_FILE}"
  printf '%s\n' "${reply}" > "${LAST_REPLY_FILE}"
  printf '%s\n' "${reply}"
}

forward_to_pane() {
  local text="$1"
  local target="$2"
  local payload
  payload=$'[gemini]\n'"${text}"
  tmux set-buffer -- "${payload}"
  tmux paste-buffer -p -d -t "${target}"
  tmux send-keys -t "${target}" Enter
}

main() {
  ensure_ready
  local cmd="${1:-}"
  case "${cmd}" in
    ask)
      shift || true
      [[ $# -ge 1 ]] || { usage; exit 2; }
      ask_gemini "$1"
      ;;
    forward)
      shift || true
      [[ $# -ge 2 ]] || { usage; exit 2; }
      local response
      response="$(ask_gemini "$1")"
      forward_to_pane "${response}" "$2"
      echo "ok: forwarded response to $2"
      ;;
    show)
      [[ -f "${LAST_PROMPT_FILE}" ]] && { echo "last prompt:"; cat "${LAST_PROMPT_FILE}"; echo; }
      [[ -f "${LAST_REPLY_FILE}" ]] && { echo "last reply:"; cat "${LAST_REPLY_FILE}"; echo; }
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"

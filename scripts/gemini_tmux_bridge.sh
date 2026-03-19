#!/usr/bin/env bash
set -euo pipefail

# Lightweight bridge for a dedicated Gemini chat running inside tmux.
# Uses Gemini CLI interactive mode in a separate tmux window.

SESSION="${TMUX_SESSION:-main}"
WINDOW="${TMUX_GEMINI_WINDOW:-gemini}"
PANE="${SESSION}:${WINDOW}.0"
WORKDIR="${TMUX_GEMINI_WORKDIR:-/home/foggen}"
START_CMD="${TMUX_GEMINI_START_CMD:-gemini --approval-mode yolo}"
DEFAULT_TIMEOUT="${TMUX_GEMINI_ASK_TIMEOUT_SEC:-45}"

usage() {
  cat <<'EOF'
Usage:
  gemini_tmux_bridge.sh start
  gemini_tmux_bridge.sh send "<message>"
  gemini_tmux_bridge.sh capture [lines]
  gemini_tmux_bridge.sh ask "<message>" [timeout_sec]

Env overrides:
  TMUX_SESSION=main
  TMUX_GEMINI_WINDOW=gemini
  TMUX_GEMINI_WORKDIR=/home/foggen
  TMUX_GEMINI_START_CMD="gemini --approval-mode yolo"
  TMUX_GEMINI_ASK_TIMEOUT_SEC=45
EOF
}

ensure_window() {
  if ! tmux has-session -t "${SESSION}" 2>/dev/null; then
    tmux new-session -d -s "${SESSION}" -n "${WINDOW}" -c "${WORKDIR}" "bash"
  fi
  if ! tmux list-windows -t "${SESSION}" -F '#W' | rg -qx "${WINDOW}"; then
    tmux new-window -d -t "${SESSION}" -n "${WINDOW}" -c "${WORKDIR}" "bash"
  fi
}

pane_cmd() {
  tmux display-message -p -t "${PANE}" '#{pane_current_command}'
}

start_gemini() {
  ensure_window
  local cmd
  cmd="$(pane_cmd || true)"
  if [[ "${cmd}" != "node" ]]; then
    tmux send-keys -t "${PANE}" C-c
    tmux send-keys -t "${PANE}" "clear" Enter
    tmux send-keys -t "${PANE}" "${START_CMD}" Enter
    sleep 3
  fi
}

send_message() {
  local text="$1"
  start_gemini
  tmux set-buffer -- "${text}"
  tmux paste-buffer -p -d -t "${PANE}"
  tmux send-keys -t "${PANE}" Enter
  # Gemini UI can occasionally keep text in compose mode; second Enter submits.
  sleep 0.25
  tmux send-keys -t "${PANE}" Enter
}

capture_tail() {
  local lines="${1:-140}"
  tmux capture-pane -pt "${PANE}" -S "-${lines}"
}

ask_once() {
  local prompt="$1"
  local timeout_sec="${2:-$DEFAULT_TIMEOUT}"
  local token="GEMBRIDGE-$(date +%s)-$RANDOM"
  local wrapped=$'Reply with first line exactly: ['"${token}"$']\nThen answer in <=6 lines.\n\n'"${prompt}"
  local deadline=$((SECONDS + timeout_sec))

  send_message "${wrapped}"

  while (( SECONDS < deadline )); do
    local cap
    cap="$(capture_tail 260 || true)"
    if grep -Fq "✦ [${token}]" <<<"${cap}"; then
      awk -v tok="✦ [${token}]" '
        index($0, tok) {flag=1}
        flag {print}
      ' <<<"${cap}" | awk '
        /Type your message or @path\/to\/file/ {exit}
        /GEMINI\.md file/ {exit}
        {print}
      '
      return 0
    fi
    sleep 1
  done

  echo "ERROR: timeout waiting for Gemini response token [${token}]" >&2
  return 1
}

main() {
  local cmd="${1:-}"
  case "${cmd}" in
    start)
      start_gemini
      echo "ok: started ${PANE} with '${START_CMD}'"
      ;;
    send)
      shift || true
      if [[ $# -lt 1 ]]; then
        usage
        exit 2
      fi
      send_message "$*"
      echo "ok: sent to ${PANE}"
      ;;
    capture)
      shift || true
      capture_tail "${1:-140}"
      ;;
    ask)
      shift || true
      if [[ $# -lt 1 ]]; then
        usage
        exit 2
      fi
      local timeout="${2:-$DEFAULT_TIMEOUT}"
      ask_once "$1" "${timeout}"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"

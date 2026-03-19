#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${CODEX_TMUX_SESSION_NAME:-main}"
TARGET_PANE="${CODEX_TMUX_TARGET_PANE:-main:0.0}"
WORKDIR="${CODEX_WORKDIR:-/home/foggen}"
CODEX_BIN="${CODEX_BIN:-/home/foggen/.nvm/versions/node/v20.20.0/bin/codex}"
CHECK_SEC="${CODEX_GUARD_CHECK_SEC:-3}"
RESUME_SESSION_ID="${CODEX_RESUME_SESSION_ID:-}"
LOG_TAG="[codex-tmux-guard]"

log() {
  printf "%s %s\n" "$LOG_TAG" "$*"
}

ensure_session() {
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux new-session -d -s "$SESSION_NAME" -n relay -c "$WORKDIR" "bash"
    log "created tmux session: $SESSION_NAME"
  fi
}

ensure_target_pane() {
  if tmux list-panes -t "$TARGET_PANE" >/dev/null 2>&1; then
    return 0
  fi

  local window_target="${TARGET_PANE%.*}"
  local pane_idx="${TARGET_PANE##*.}"

  if ! tmux list-panes -t "$window_target" >/dev/null 2>&1; then
    tmux new-window -d -t "$window_target" -c "$WORKDIR" -n relay "bash"
    log "created tmux window: $window_target"
  fi

  while ! tmux list-panes -t "$TARGET_PANE" >/dev/null 2>&1; do
    tmux split-window -d -t "$window_target" -c "$WORKDIR" "bash"
  done

  if [[ "$pane_idx" != "0" ]]; then
    log "warning: requested pane index is $pane_idx; created panes until target exists"
  fi
}

spawn_codex() {
  local cmd resume_args
  if [[ -n "$RESUME_SESSION_ID" ]]; then
    resume_args="resume '$RESUME_SESSION_ID'"
  else
    resume_args="resume --last"
  fi
  cmd="cd '$WORKDIR' && exec '$CODEX_BIN' $resume_args --dangerously-bypass-approvals-and-sandbox --no-alt-screen -C '$WORKDIR'"
  tmux respawn-pane -k -t "$TARGET_PANE" "$cmd"
  tmux setw -t "${TARGET_PANE%.*}" remain-on-exit on
  log "respawned codex in $TARGET_PANE"
}

is_codex_alive() {
  local dead cmd
  dead="$(tmux display-message -p -t "$TARGET_PANE" '#{pane_dead}')"
  cmd="$(tmux display-message -p -t "$TARGET_PANE" '#{pane_current_command}')"

  if [[ "$dead" == "1" ]]; then
    return 1
  fi

  case "$cmd" in
    node|codex)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

main() {
  log "starting guard for pane=$TARGET_PANE"
  while true; do
    ensure_session
    ensure_target_pane
    if ! is_codex_alive; then
      spawn_codex
      sleep 2
    fi
    sleep "$CHECK_SEC"
  done
}

main "$@"

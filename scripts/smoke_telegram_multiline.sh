#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

uv run python - <<'PY'
import bot


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


class DummyRelay:
    enabled = True
    target_pane = "main:0.0"
    plain_text_to_tmux = True

    def is_available(self) -> bool:
        return True


help_text = bot.build_help_text()
chunks = bot._chunk_text(help_text)
status_text = bot._format_tmux_status(DummyRelay())

check("\n" in help_text, "help text must include real newlines")
check("\\n" not in help_text, "help text must not include literal \\n")
check(bool(chunks), "chunker returned no chunks")
check("\n" in chunks[0], "chunked help must include real newlines")
check("\\n" not in chunks[0], "chunked help must not include literal \\n")
check("\n" in status_text, "tmux status must include real newlines")
check("\\n" not in status_text, "tmux status must not include literal \\n")

print("PASS: Telegram multiline formatting checks")
PY

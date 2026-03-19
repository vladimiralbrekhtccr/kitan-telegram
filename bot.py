#!/usr/bin/env python3
"""Minimal Telegram bridge: tmux relay + Piper TTS + local /reply API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import httpx

LOG = logging.getLogger("kitan-telegram")

STATUS_CODE_SCHEMA = "yuuki.statuscode"
STATUS_CODE_VERSION = 3


def build_start_text() -> str:
    return "\n".join(
        [
            "Yuuki bot is online.",
            "Plain text -> tmux relay.",
            "",
            "Commands:",
            "/id",
            "/ping",
            "/tmuxstatus",
            "/status",
            "/statusbrief",
            "/statuscode",
            "/statuscodejson",
            "/statusjson",
            "/totmux <text>",
            "/archivecfg",
            "/piperlangs",
            "/piper <lang> <text>",
            "Send image -> save + relay path to tmux",
            "Send document -> save + relay path to tmux",
            "/help",
        ]
    )


def build_help_text() -> str:
    return "\n".join(
        [
            "Usage:",
            "/id - show your chat id",
            "/ping - bot health check",
            "/tmuxstatus - tmux relay status",
            "/status - runtime status summary",
            "/statusbrief - compact runtime status",
            "/statuscode - minimal machine status",
            "/statuscodejson - minimal machine status as JSON",
            "/statusjson - runtime status as JSON",
            "/totmux <text> - send text to tmux pane",
            "/archivecfg - AI_github archive config/status",
            "/piperlangs - list Piper languages",
            "/piper <lang> <text> - text-to-speech voice message",
            "Send image/photo - save on VPS and relay IMAGE_SAVED block to tmux",
            "Send document/file - save on VPS and relay FILE_SAVED block to tmux",
            "/help - this help",
            "",
            "Plain text is relayed to tmux automatically.",
        ]
    )


@dataclass
class Config:
    telegram_token: str
    telegram_allowed_chat_ids: set[str]
    telegram_poll_timeout_sec: int
    telegram_retry_sleep_sec: int
    telegram_force_ipv4: bool
    skip_backlog_on_start: bool
    json_log_enabled: bool
    json_log_path: Path
    health_status_path: Path
    health_alert_state_path: Path
    status_health_stale_sec: int
    status_event_lookback_min: int
    status_event_scan_max_bytes: int
    status_queue_pressure_critical_threshold: int
    alert_event_lookback_min: int
    alert_reply_failed_threshold: int
    alert_relay_error_threshold: int
    watcher_enabled: bool
    watcher_idle_sec: int
    watcher_cooldown_sec: int
    watcher_state_path: Path
    watcher_task_state_path: Path
    self_improve_state_path: Path
    self_test_status_path: Path
    self_test_max_age_sec: int
    image_intake_enabled: bool
    image_save_dir: Path
    file_intake_enabled: bool
    file_save_dir: Path
    ai_archive_enabled: bool
    ai_archive_repo_dir: Path
    ai_archive_push: bool
    ai_archive_max_bytes: int

    piper_enabled: bool
    piper_bin: str
    piper_timeout_sec: int
    piper_max_chars: int
    piper_default_lang: str
    piper_models: dict[str, Path]

    tmux_relay_enabled: bool
    tmux_target_pane: str
    tmux_relay_max_chars: int
    tmux_relay_prefix: str
    tmux_relay_ack: bool
    tmux_plain_text_to_tmux: bool
    tmux_debounce_enabled: bool
    tmux_debounce_sec: int
    tmux_plain_text_dedupe_enabled: bool
    tmux_plain_text_dedupe_sec: int

    plain_text_quick_reply: str

    reply_api_enabled: bool
    reply_api_host: str
    reply_api_port: int
    reply_api_token: str
    reply_api_dedupe_sec: int
    reply_api_send_retries: int
    reply_api_send_backoff_ms: int
    reply_api_queue_max: int
    reply_api_queue_drop_oldest: bool
    reply_api_metrics_state_path: Path


def _read_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _norm_lang(lang: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "", (lang or "").strip().lower().replace("-", "_"))


def _parse_piper_models(raw: str) -> dict[str, Path]:
    models: dict[str, Path] = {}
    if not raw.strip():
        return models
    for chunk in re.split(r"[;,]", raw):
        part = chunk.strip()
        if not part or "=" not in part:
            continue
        lang_raw, path_raw = part.split("=", 1)
        lang = _norm_lang(lang_raw)
        model_path = Path(path_raw.strip()).expanduser()
        if not model_path.is_absolute():
            model_path = (Path(__file__).resolve().parent / model_path).resolve()
        if lang:
            models[lang] = model_path
    return models


def load_config() -> Config:
    _read_dotenv(Path(__file__).resolve().parent / ".env")

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    raw_ids = (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    allowed_ids = {x.strip() for x in raw_ids.split(",") if x.strip()}

    piper_models = _parse_piper_models(os.environ.get("TELEGRAM_PIPER_MODELS", ""))
    default_lang = _norm_lang(os.environ.get("TELEGRAM_PIPER_DEFAULT_LANG", "en"))
    if not default_lang and piper_models:
        default_lang = sorted(piper_models.keys())[0]

    quick_reply = (os.environ.get("TELEGRAM_PLAIN_TEXT_QUICK_REPLY") or "Got it.").strip()
    image_save_dir = Path(
        (os.environ.get("TELEGRAM_IMAGE_SAVE_DIR") or "/tmp/kitan-telegram/images").strip()
    ).expanduser()
    file_save_dir = Path(
        (os.environ.get("TELEGRAM_FILE_SAVE_DIR") or "/tmp/kitan-telegram/files").strip()
    ).expanduser()
    ai_archive_repo_dir = Path(
        (os.environ.get("TELEGRAM_AI_ARCHIVE_REPO_DIR") or "/home/foggen/AI_github").strip()
    ).expanduser()
    json_log_path = Path(
        (os.environ.get("TELEGRAM_JSON_LOG_PATH") or "/home/foggen/kitan-telegram/logs/bot.jsonl").strip()
    ).expanduser()
    health_status_path = Path(
        (os.environ.get("TELEGRAM_HEALTH_STATUS_PATH") or "/home/foggen/kitan-telegram/runtime/health_status.json").strip()
    ).expanduser()
    health_alert_state_path = Path(
        (os.environ.get("TELEGRAM_HEALTH_ALERT_STATE_PATH") or "/home/foggen/kitan-telegram/runtime/health_alert_state.json").strip()
    ).expanduser()
    watcher_state_path = Path(
        (os.environ.get("YUUKI_WATCHER_STATE_PATH") or "/home/foggen/kitan-telegram/runtime/watcher_state.json").strip()
    ).expanduser()
    watcher_task_state_path = Path(
        (os.environ.get("YUUKI_WATCHER_TASK_STATE_PATH") or "/home/foggen/AI_github/projects/yuuki-bot-upgrade/LAST_STATE.md").strip()
    ).expanduser()
    self_improve_state_path = Path(
        (os.environ.get("YUUKI_SELF_IMPROVE_STATE_PATH") or "/home/foggen/kitan-telegram/runtime/self_improve_nudge_state.json").strip()
    ).expanduser()
    self_test_status_path = Path(
        (os.environ.get("YUUKI_SELF_TEST_STATUS_PATH") or "/home/foggen/kitan-telegram/runtime/self_test_latest.json").strip()
    ).expanduser()
    reply_api_metrics_state_path = Path(
        (
            os.environ.get("TELEGRAM_LOCAL_REPLY_API_METRICS_STATE_PATH")
            or "/home/foggen/kitan-telegram/runtime/reply_api_metrics_state.json"
        ).strip()
    ).expanduser()

    return Config(
        telegram_token=token,
        telegram_allowed_chat_ids=allowed_ids,
        telegram_poll_timeout_sec=max(10, _int_env("TELEGRAM_POLL_TIMEOUT_SEC", 50)),
        telegram_retry_sleep_sec=max(1, _int_env("TELEGRAM_RETRY_SLEEP_SEC", 3)),
        telegram_force_ipv4=_bool_env("TELEGRAM_FORCE_IPV4", True),
        skip_backlog_on_start=_bool_env("TELEGRAM_SKIP_BACKLOG_ON_START", False),
        json_log_enabled=_bool_env("TELEGRAM_JSON_LOG_ENABLED", True),
        json_log_path=json_log_path,
        health_status_path=health_status_path,
        health_alert_state_path=health_alert_state_path,
        status_health_stale_sec=max(60, _int_env("TELEGRAM_STATUS_HEALTH_STALE_SEC", 1800)),
        status_event_lookback_min=max(1, _int_env("TELEGRAM_STATUS_EVENT_LOOKBACK_MIN", 15)),
        status_event_scan_max_bytes=max(64_000, _int_env("TELEGRAM_STATUS_EVENT_SCAN_MAX_BYTES", 2_097_152)),
        status_queue_pressure_critical_threshold=max(
            1, _int_env("TELEGRAM_STATUS_QUEUE_PRESSURE_CRITICAL_THRESHOLD", 3)
        ),
        alert_event_lookback_min=max(1, _int_env("TELEGRAM_ALERT_EVENT_LOOKBACK_MIN", 15)),
        alert_reply_failed_threshold=max(1, _int_env("TELEGRAM_ALERT_REPLY_FAILED_THRESHOLD", 3)),
        alert_relay_error_threshold=max(1, _int_env("TELEGRAM_ALERT_RELAY_ERROR_THRESHOLD", 5)),
        watcher_enabled=_bool_env("YUUKI_WATCHER_ENABLED", False),
        watcher_idle_sec=max(30, _int_env("YUUKI_WATCHER_IDLE_SEC", 600)),
        watcher_cooldown_sec=max(30, _int_env("YUUKI_WATCHER_COOLDOWN_SEC", 3600)),
        watcher_state_path=watcher_state_path,
        watcher_task_state_path=watcher_task_state_path,
        self_improve_state_path=self_improve_state_path,
        self_test_status_path=self_test_status_path,
        self_test_max_age_sec=max(60, _int_env("TELEGRAM_HEALTH_SELF_TEST_MAX_AGE_SEC", 7200)),
        image_intake_enabled=_bool_env("TELEGRAM_IMAGE_INTAKE_ENABLED", True),
        image_save_dir=image_save_dir,
        file_intake_enabled=_bool_env("TELEGRAM_FILE_INTAKE_ENABLED", True),
        file_save_dir=file_save_dir,
        ai_archive_enabled=_bool_env("TELEGRAM_AI_ARCHIVE_ENABLED", False),
        ai_archive_repo_dir=ai_archive_repo_dir,
        ai_archive_push=_bool_env("TELEGRAM_AI_ARCHIVE_PUSH", True),
        ai_archive_max_bytes=max(1_000, _int_env("TELEGRAM_AI_ARCHIVE_MAX_BYTES", 5_000_000)),
        piper_enabled=_bool_env("TELEGRAM_PIPER_ENABLED", True),
        piper_bin=(os.environ.get("TELEGRAM_PIPER_BIN") or "piper").strip(),
        piper_timeout_sec=max(5, _int_env("TELEGRAM_PIPER_TIMEOUT_SEC", 120)),
        piper_max_chars=max(20, _int_env("TELEGRAM_PIPER_MAX_CHARS", 600)),
        piper_default_lang=default_lang,
        piper_models=piper_models,
        tmux_relay_enabled=_bool_env("TELEGRAM_TMUX_RELAY_ENABLED", True),
        tmux_target_pane=(os.environ.get("TELEGRAM_TMUX_TARGET_PANE") or "").strip(),
        tmux_relay_max_chars=max(20, _int_env("TELEGRAM_TMUX_RELAY_MAX_CHARS", 2000)),
        tmux_relay_prefix=(os.environ.get("TELEGRAM_TMUX_RELAY_PREFIX") or "").strip(),
        tmux_relay_ack=_bool_env("TELEGRAM_TMUX_RELAY_ACK", False),
        tmux_plain_text_to_tmux=_bool_env("TELEGRAM_TMUX_PLAIN_TEXT_TO_TMUX", True),
        tmux_debounce_enabled=_bool_env("TELEGRAM_TMUX_DEBOUNCE_ENABLED", True),
        tmux_debounce_sec=max(0, _int_env("TELEGRAM_TMUX_DEBOUNCE_SEC", 3)),
        tmux_plain_text_dedupe_enabled=_bool_env("TELEGRAM_TMUX_PLAIN_TEXT_DEDUPE_ENABLED", True),
        tmux_plain_text_dedupe_sec=max(1, _int_env("TELEGRAM_TMUX_PLAIN_TEXT_DEDUPE_SEC", 45)),
        plain_text_quick_reply=quick_reply,
        reply_api_enabled=_bool_env("TELEGRAM_LOCAL_REPLY_API_ENABLED", True),
        reply_api_host=(os.environ.get("TELEGRAM_LOCAL_REPLY_API_HOST") or "127.0.0.1").strip(),
        reply_api_port=max(1, _int_env("TELEGRAM_LOCAL_REPLY_API_PORT", 8788)),
        reply_api_token=(os.environ.get("TELEGRAM_LOCAL_REPLY_API_TOKEN") or "").strip(),
        reply_api_dedupe_sec=max(0, _int_env("TELEGRAM_LOCAL_REPLY_API_DEDUPE_SEC", 10)),
        reply_api_send_retries=max(1, _int_env("TELEGRAM_LOCAL_REPLY_API_SEND_RETRIES", 3)),
        reply_api_send_backoff_ms=max(50, _int_env("TELEGRAM_LOCAL_REPLY_API_SEND_BACKOFF_MS", 400)),
        reply_api_queue_max=max(1, _int_env("TELEGRAM_LOCAL_REPLY_API_QUEUE_MAX", 500)),
        reply_api_queue_drop_oldest=_bool_env("TELEGRAM_LOCAL_REPLY_API_QUEUE_DROP_OLDEST", True),
        reply_api_metrics_state_path=reply_api_metrics_state_path,
    )


def _clean(value: str | None) -> str:
    text = (value or "").strip()
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _chunk_text(text: str, max_len: int = 3800) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return [""]
    chunks: list[str] = []
    current = ""
    for line in raw.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks or [raw[:max_len]]


def _normalize_telegram_text(text: str) -> str:
    # Some upstream paths may accidentally produce escaped newlines.
    # Normalize before send so Telegram always renders multiline text correctly.
    return (text or "").replace("\\r\\n", "\n").replace("\\n", "\n")


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts_utc": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        event_type = getattr(record, "event_type", None)
        event_fields = getattr(record, "event_fields", None)
        if event_type:
            payload["event_type"] = event_type
        if isinstance(event_fields, dict):
            payload["event_fields"] = event_fields
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def log_event(event_type: str, **fields: object) -> None:
    safe: dict[str, object] = {}
    for k, v in fields.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        else:
            safe[k] = str(v)
    LOG.info("event", extra={"event_type": event_type, "event_fields": safe})


class TelegramClient:
    def __init__(self, token: str, timeout_sec: float = 70.0, force_ipv4: bool = False):
        transport = httpx.HTTPTransport(local_address="0.0.0.0") if force_ipv4 else None
        self.client = httpx.Client(timeout=timeout_sec, transport=transport)
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def call(self, method: str, payload: dict) -> dict:
        resp = self.client.post(f"{self.base}/{method}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data}")
        return data

    def get_me(self) -> dict:
        return self.call("getMe", {}).get("result", {})

    def get_updates(self, offset: int, timeout_sec: int) -> list[dict]:
        data = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_sec,
                "allowed_updates": ["message"],
            },
        )
        result = data.get("result")
        return result if isinstance(result, list) else []

    def get_updates_nowait(self, offset: int = 0) -> list[dict]:
        return self.get_updates(offset=offset, timeout_sec=0)

    def get_file(self, file_id: str) -> dict:
        data = self.call("getFile", {"file_id": file_id})
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Telegram getFile returned invalid result for file_id={file_id}")
        return result

    def send(self, chat_id: int, text: str) -> None:
        normalized = _normalize_telegram_text(text)
        for chunk in _chunk_text(normalized):
            self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )

    def download_file_to(self, file_path: str, dst_path: Path) -> None:
        if not file_path:
            raise RuntimeError("Telegram file_path is empty")
        url = f"{self.file_base}/{file_path}"
        resp = self.client.get(url)
        resp.raise_for_status()
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(resp.content)

    def send_voice(self, chat_id: int, path: Path, caption: str = "") -> None:
        with path.open("rb") as fh:
            resp = self.client.post(
                f"{self.base}/sendVoice",
                data={
                    "chat_id": str(chat_id),
                    "caption": caption[:1024],
                },
                files={"voice": (path.name, fh, "audio/ogg")},
            )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendVoice failed: {data}")


class LastChatState:
    def __init__(self):
        self._lock = threading.Lock()
        self._chat_id: int | None = None
        self._updated_at: float = 0.0

    def set(self, chat_id: int) -> None:
        with self._lock:
            self._chat_id = int(chat_id)
            self._updated_at = time.time()

    def snapshot(self) -> tuple[int | None, float]:
        with self._lock:
            return self._chat_id, self._updated_at


class PiperTTS:
    def __init__(self, cfg: Config):
        self.enabled = cfg.piper_enabled
        self.bin = cfg.piper_bin
        self.timeout_sec = cfg.piper_timeout_sec
        self.max_chars = cfg.piper_max_chars
        self.default_lang = cfg.piper_default_lang
        self.models = dict(cfg.piper_models)

    def available_langs(self) -> list[str]:
        return sorted(self.models.keys())

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        return bool(self.available_langs()) and bool(self._resolve_bin())

    def _resolve_bin(self) -> str | None:
        if Path(self.bin).exists():
            return str(Path(self.bin))
        return shutil.which(self.bin)

    def _resolve_ffmpeg(self) -> str | None:
        return shutil.which("ffmpeg")

    def synthesize(self, lang: str, text: str) -> Path:
        if not self.enabled:
            raise RuntimeError("Piper is disabled")

        lang_norm = _norm_lang(lang)
        if lang_norm not in self.models:
            raise RuntimeError(f"Language '{lang}' is not configured")

        clean_text = _clean(text)
        if not clean_text:
            raise RuntimeError("Text is empty")
        if len(clean_text) > self.max_chars:
            raise RuntimeError(f"Text too long ({len(clean_text)} chars > {self.max_chars})")

        piper_bin = self._resolve_bin()
        if not piper_bin:
            raise RuntimeError("Piper binary not found. Set TELEGRAM_PIPER_BIN")
        ffmpeg_bin = self._resolve_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg not found. Install ffmpeg to send playable Telegram audio")

        model_path = self.models[lang_norm]
        if not model_path.exists():
            raise RuntimeError(f"Piper model not found: {model_path}")

        fd, tmp_wav = tempfile.mkstemp(prefix=f"piper-{lang_norm}-", suffix=".wav")
        os.close(fd)
        wav_path = Path(tmp_wav)

        fd, tmp_ogg = tempfile.mkstemp(prefix=f"piper-{lang_norm}-", suffix=".ogg")
        os.close(fd)
        ogg_path = Path(tmp_ogg)

        cmd = [piper_bin, "--model", str(model_path), "--output_file", str(wav_path)]
        try:
            proc = subprocess.run(
                cmd,
                input=(clean_text + "\n").encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            wav_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)
            raise RuntimeError(f"Piper timeout after {self.timeout_sec}s") from exc

        if proc.returncode != 0:
            wav_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)
            err = (proc.stderr.decode("utf-8", errors="ignore") or "piper failed").strip()
            raise RuntimeError(err[-300:])

        if not wav_path.exists() or wav_path.stat().st_size < 100:
            wav_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)
            raise RuntimeError("Piper generated empty audio")

        ffmpeg_cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-vn",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(ogg_path),
        ]
        try:
            ffmpeg_proc = subprocess.run(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            wav_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg timeout after {self.timeout_sec}s") from exc
        finally:
            wav_path.unlink(missing_ok=True)

        if ffmpeg_proc.returncode != 0:
            ogg_path.unlink(missing_ok=True)
            err = (ffmpeg_proc.stderr.decode("utf-8", errors="ignore") or "ffmpeg failed").strip()
            raise RuntimeError(err[-300:])

        if not ogg_path.exists() or ogg_path.stat().st_size < 100:
            ogg_path.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg generated empty audio")

        return ogg_path


class TmuxRelay:
    def __init__(self, cfg: Config):
        self.enabled = cfg.tmux_relay_enabled
        self.target_pane = cfg.tmux_target_pane
        self.max_chars = cfg.tmux_relay_max_chars
        self.prefix = cfg.tmux_relay_prefix
        self.ack = cfg.tmux_relay_ack
        self.plain_text_to_tmux = cfg.tmux_plain_text_to_tmux

    def _resolve_tmux(self) -> str | None:
        return shutil.which("tmux")

    def _sanitize(self, text: str) -> str:
        value = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
        if len(value) > self.max_chars:
            value = value[: self.max_chars]
        return value

    def is_available(self) -> bool:
        return self.enabled and bool(self.target_pane) and bool(self._resolve_tmux())

    def _is_task_running(self, tmux_bin: str) -> bool:
        try:
            proc = subprocess.run(
                [tmux_bin, "capture-pane", "-pt", self.target_pane, "-S", "-60"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=4,
                check=False,
                text=True,
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        snap = proc.stdout.lower()
        return ("esc to interrupt" in snap) or ("working (" in snap)

    def forward(self, text: str) -> None:
        if not self.enabled:
            raise RuntimeError("tmux relay is disabled")
        tmux_bin = self._resolve_tmux()
        if not tmux_bin:
            raise RuntimeError("tmux not found on host")
        if not self.target_pane:
            raise RuntimeError("TELEGRAM_TMUX_TARGET_PANE is not set")

        payload = self._sanitize(text)
        if not payload:
            raise RuntimeError("message is empty")
        if self.prefix:
            sep = "" if self.prefix.endswith((" ", "\t", "\n")) else " "
            payload = f"{self.prefix}{sep}{payload}"

        subprocess.run(
            [tmux_bin, "send-keys", "-t", self.target_pane, "C-u"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )
        subprocess.run(
            [tmux_bin, "set-buffer", "--", payload],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )
        subprocess.run(
            [tmux_bin, "paste-buffer", "-p", "-d", "-t", self.target_pane],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )
        time.sleep(0.08)
        subprocess.run(
            [tmux_bin, "send-keys", "-t", self.target_pane, "Left"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )
        subprocess.run(
            [tmux_bin, "send-keys", "-t", self.target_pane, "Right"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )
        time.sleep(0.08)
        submit_key = "Tab" if self._is_task_running(tmux_bin) else "Enter"
        subprocess.run(
            [tmux_bin, "send-keys", "-t", self.target_pane, submit_key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )


class TextDebouncer:
    def __init__(self, enabled: bool, debounce_sec: int):
        self.enabled = bool(enabled) and int(debounce_sec) > 0
        self.debounce_sec = max(0, int(debounce_sec))
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, object]] = {}

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def add(self, chat_id: int, text: str) -> bool:
        """Add text to debounce buffer.

        Returns True when a new burst is started (first message for chat),
        False when message is appended to an existing pending burst.
        """
        if not self.enabled:
            return True
        now = time.time()
        with self._lock:
            cur = self._pending.get(int(chat_id))
            if cur is None:
                self._pending[int(chat_id)] = {
                    "texts": [text],
                    "deadline": now + float(self.debounce_sec),
                }
                return True
            texts = cur.get("texts")
            if isinstance(texts, list):
                texts.append(text)
            else:
                cur["texts"] = [text]
            cur["deadline"] = now + float(self.debounce_sec)
            return False

    def pop_due(self) -> list[tuple[int, str]]:
        if not self.enabled:
            return []
        now = time.time()
        due: list[tuple[int, str]] = []
        with self._lock:
            ready_ids = [
                chat_id
                for chat_id, item in self._pending.items()
                if float(item.get("deadline", now + 999)) <= now
            ]
            for chat_id in ready_ids:
                item = self._pending.pop(chat_id, {})
                texts = item.get("texts")
                if isinstance(texts, list):
                    merged = "\n".join(x for x in texts if isinstance(x, str) and x.strip()).strip()
                    if merged:
                        due.append((int(chat_id), merged))
        return due


class TextDeduper:
    def __init__(self, enabled: bool, dedupe_sec: int):
        self.enabled = bool(enabled) and int(dedupe_sec) > 0
        self.dedupe_sec = max(1, int(dedupe_sec))
        self._lock = threading.Lock()
        self._last_by_chat: dict[int, dict[str, object]] = {}

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def allow(self, chat_id: int, text: str) -> tuple[bool, int]:
        if not self.enabled:
            return True, 1
        now = time.time()
        norm = self._norm(text)
        with self._lock:
            prev = self._last_by_chat.get(int(chat_id))
            if prev is not None:
                prev_text = str(prev.get("text") or "")
                prev_ts = float(prev.get("ts") or 0.0)
                prev_repeat = int(prev.get("repeat") or 1)
                if prev_text == norm and (now - prev_ts) <= float(self.dedupe_sec):
                    repeat = prev_repeat + 1
                    prev["ts"] = now
                    prev["repeat"] = repeat
                    return False, repeat
            self._last_by_chat[int(chat_id)] = {"text": norm, "ts": now, "repeat": 1}
            return True, 1


class LocalReplyAPI:
    def __init__(
        self,
        cfg: Config,
        state: LastChatState,
        status_code_provider: Callable[[bool], dict[str, object]] | None = None,
    ):
        self.enabled = cfg.reply_api_enabled
        self.host = cfg.reply_api_host
        self.port = cfg.reply_api_port
        self.token = cfg.reply_api_token
        self.dedupe_sec = cfg.reply_api_dedupe_sec
        self.send_retries = cfg.reply_api_send_retries
        self.send_backoff_ms = cfg.reply_api_send_backoff_ms
        self.queue_max = max(1, int(cfg.reply_api_queue_max))
        self.queue_drop_oldest = bool(cfg.reply_api_queue_drop_oldest)
        self.metrics_state_path = cfg.reply_api_metrics_state_path
        self.state = state
        self.tg = TelegramClient(cfg.telegram_token, timeout_sec=20.0, force_ipv4=cfg.telegram_force_ipv4)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._sender_thread: threading.Thread | None = None
        self._send_q: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=self.queue_max)
        self._dedupe_lock = threading.Lock()
        self._recent_sends: dict[tuple[int, str], float] = {}
        self._metrics_lock = threading.Lock()
        self._started_at = time.time()
        self._queued_total = 0
        self._duplicate_total = 0
        self._queue_dropped_total = 0
        self._queue_full_rejected_total = 0
        self._sent_total = 0
        self._failed_total = 0
        self._latency_ms: deque[int] = deque(maxlen=200)
        self._last_send_error = ""
        self._alert_transition_total = 0
        self._last_in_alert_seen: bool | None = None
        self._status_code_provider = status_code_provider
        self._load_metrics_state()

    def _auth_ok(self, headers: dict[str, str]) -> bool:
        if not self.token:
            return True
        auth = (headers.get("authorization") or "").strip()
        if auth == f"Bearer {self.token}":
            return True
        return (headers.get("x-api-key") or "").strip() == self.token

    def start(self) -> None:
        if not self.enabled:
            return

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _fmt: str, *_args) -> None:
                return

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
                data = (text or "").encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _headers_lower(self) -> dict[str, str]:
                return {k.lower(): v for k, v in self.headers.items()}

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._json(200, {"ok": True})
                    return
                if self.path == "/metrics":
                    self._json(200, outer.metrics_snapshot())
                    return
                if self.path == "/metrics.prom":
                    self._text(200, outer._metrics_prometheus_text(), "text/plain; version=0.0.4; charset=utf-8")
                    return
                if self.path == "/statuscode":
                    self._json(200, outer._status_code_payload(with_schema=False))
                    return
                if self.path == "/statuscodejson":
                    self._json(200, outer._status_code_payload(with_schema=True))
                    return
                if self.path == "/last_chat":
                    chat_id, updated_at = outer.state.snapshot()
                    self._json(
                        200,
                        {
                            "ok": True,
                            "last_chat_id": chat_id,
                            "updated_at": datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat()
                            if updated_at > 0
                            else None,
                        },
                    )
                    return
                self._json(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/reply":
                    self._json(404, {"ok": False, "error": "not_found"})
                    return

                headers = self._headers_lower()
                if not outer._auth_ok(headers):
                    self._json(401, {"ok": False, "error": "unauthorized"})
                    return

                raw_len = (headers.get("content-length") or "0").strip()
                try:
                    content_length = int(raw_len)
                except ValueError:
                    content_length = 0
                body = self.rfile.read(max(content_length, 0))

                chat_id: int | None = None
                text = ""
                content_type = (headers.get("content-type") or "").lower()
                if "application/json" in content_type:
                    try:
                        payload = json.loads(body.decode("utf-8", errors="ignore") or "{}")
                    except json.JSONDecodeError:
                        self._json(400, {"ok": False, "error": "invalid_json"})
                        return
                    if not isinstance(payload, dict):
                        self._json(400, {"ok": False, "error": "invalid_payload"})
                        return
                    raw_chat_id = payload.get("chat_id")
                    if raw_chat_id is not None and str(raw_chat_id).strip():
                        try:
                            chat_id = int(str(raw_chat_id).strip())
                        except ValueError:
                            self._json(400, {"ok": False, "error": "invalid_chat_id"})
                            return
                    text = (payload.get("text") or "").strip()
                else:
                    text = body.decode("utf-8", errors="ignore").strip()

                text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
                if not text:
                    self._json(400, {"ok": False, "error": "empty_text"})
                    return

                if chat_id is None:
                    chat_id, _ = outer.state.snapshot()
                if chat_id is None:
                    self._json(409, {"ok": False, "error": "no_recent_chat"})
                    return

                if outer._is_duplicate(chat_id, text):
                    LOG.info("reply-api duplicate suppressed chat_id=%s chars=%s", chat_id, len(text))
                    outer._mark_duplicate()
                    log_event("reply_duplicate_suppressed", chat_id=int(chat_id), chars=len(text))
                    self._json(200, {"ok": True, "chat_id": int(chat_id), "chars": len(text), "duplicate": True})
                    return

                queued, dropped_oldest = outer._enqueue_reply(int(chat_id), text)
                if not queued:
                    self._json(
                        429,
                        {
                            "ok": False,
                            "error": "queue_full",
                            "chat_id": int(chat_id),
                            "chars": len(text),
                            "queue_depth": int(outer._send_q.qsize()),
                        },
                    )
                    return
                self._json(
                    200,
                    {
                        "ok": True,
                        "chat_id": int(chat_id),
                        "chars": len(text),
                        "queued": True,
                        "dropped_oldest": bool(dropped_oldest),
                    },
                )

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="telegram-local-reply-api",
        )
        self.thread.start()
        self._sender_thread = threading.Thread(
            target=self._send_worker,
            daemon=True,
            name="telegram-local-reply-sender",
        )
        self._sender_thread.start()
        LOG.info("local reply api enabled url=http://%s:%s/reply token=%s", self.host, self.port, "set" if self.token else "unset")

    def _status_code_payload(self, *, with_schema: bool) -> dict[str, object]:
        if callable(self._status_code_provider):
            try:
                payload = self._status_code_provider(bool(with_schema))
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                LOG.warning("status code provider failed: %s", exc)
        fallback: dict[str, object] = {
            "state": "unknown",
            "hint_code": "unknown_check_status",
            "severity": "unknown",
            "fails": "(unknown)",
            "quick_lane": "unknown",
            "reply_auth": "unknown",
            "sig8": "(unknown)",
            "in_alert": "(unknown)",
            "stale": "(unknown)",
            "ts": "(unknown)",
        }
        if with_schema:
            fallback["schema"] = STATUS_CODE_SCHEMA
            fallback["version"] = STATUS_CODE_VERSION
        return fallback

    def _is_duplicate(self, chat_id: int, text: str) -> bool:
        if self.dedupe_sec <= 0:
            return False
        now = time.time()
        key = (int(chat_id), text)
        with self._dedupe_lock:
            cutoff = now - float(self.dedupe_sec)
            if self._recent_sends:
                stale = [k for k, ts in self._recent_sends.items() if ts < cutoff]
                for k in stale:
                    self._recent_sends.pop(k, None)
            prev = self._recent_sends.get(key)
            if prev is not None and now - prev <= float(self.dedupe_sec):
                return True
            self._recent_sends[key] = now
            return False

    def _mark_queued(self) -> None:
        with self._metrics_lock:
            self._queued_total += 1

    def _mark_duplicate(self) -> None:
        with self._metrics_lock:
            self._duplicate_total += 1

    def _mark_queue_dropped(self) -> None:
        changed = False
        with self._metrics_lock:
            self._queue_dropped_total += 1
            changed = True
        if changed:
            self._save_metrics_state()

    def _mark_queue_rejected(self) -> None:
        changed = False
        with self._metrics_lock:
            self._queue_full_rejected_total += 1
            changed = True
        if changed:
            self._save_metrics_state()

    def _mark_sent(self, latency_ms: int) -> None:
        with self._metrics_lock:
            self._sent_total += 1
            self._latency_ms.append(max(0, int(latency_ms)))

    def _mark_failed(self, error: str) -> None:
        with self._metrics_lock:
            self._failed_total += 1
            self._last_send_error = (error or "").strip()[:300]

    def _load_metrics_state(self) -> None:
        path = self.metrics_state_path
        if not isinstance(path, Path):
            return
        try:
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(payload, dict):
                return
            transition_total_raw = payload.get("alert_transitions_total")
            transition_total = None
            try:
                transition_total = int(transition_total_raw)  # type: ignore[arg-type]
            except Exception:
                transition_total = None
            queue_dropped_total_raw = payload.get("queue_dropped_total")
            queue_dropped_total = None
            try:
                queue_dropped_total = int(queue_dropped_total_raw)  # type: ignore[arg-type]
            except Exception:
                queue_dropped_total = None
            queue_full_rejected_total_raw = payload.get("queue_full_rejected_total")
            queue_full_rejected_total = None
            try:
                queue_full_rejected_total = int(queue_full_rejected_total_raw)  # type: ignore[arg-type]
            except Exception:
                queue_full_rejected_total = None
            last_seen_raw = payload.get("last_in_alert_seen")
            last_seen = last_seen_raw if isinstance(last_seen_raw, bool) else None
            with self._metrics_lock:
                if transition_total is not None and transition_total >= 0:
                    self._alert_transition_total = transition_total
                if queue_dropped_total is not None and queue_dropped_total >= 0:
                    self._queue_dropped_total = queue_dropped_total
                if queue_full_rejected_total is not None and queue_full_rejected_total >= 0:
                    self._queue_full_rejected_total = queue_full_rejected_total
                if last_seen is not None:
                    self._last_in_alert_seen = last_seen
        except Exception as exc:
            LOG.warning("failed to load reply metrics state path=%s err=%s", path, exc)

    def _save_metrics_state(self) -> None:
        path = self.metrics_state_path
        if not isinstance(path, Path):
            return
        with self._metrics_lock:
            payload = {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "alert_transitions_total": int(self._alert_transition_total),
                "queue_dropped_total": int(self._queue_dropped_total),
                "queue_full_rejected_total": int(self._queue_full_rejected_total),
                "last_in_alert_seen": self._last_in_alert_seen if isinstance(self._last_in_alert_seen, bool) else None,
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)
        except Exception as exc:
            LOG.warning("failed to save reply metrics state path=%s err=%s", path, exc)

    def _track_alert_transition(self, in_alert_value: object) -> None:
        in_alert_bool = in_alert_value if isinstance(in_alert_value, bool) else None
        if in_alert_bool is None:
            return
        changed = False
        with self._metrics_lock:
            if self._last_in_alert_seen is None:
                self._last_in_alert_seen = in_alert_bool
                changed = True
            elif self._last_in_alert_seen != in_alert_bool:
                self._alert_transition_total += 1
                self._last_in_alert_seen = in_alert_bool
                changed = True
        if changed:
            self._save_metrics_state()

    def metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            latencies = sorted(self._latency_ms)
            avg_ms = int(sum(latencies) / len(latencies)) if latencies else None
            p95_ms = None
            if latencies:
                idx = int(round(0.95 * (len(latencies) - 1)))
                p95_ms = int(latencies[idx])
            return {
                "ok": True,
                "enabled": bool(self.enabled),
                "started_at": datetime.fromtimestamp(self._started_at, tz=timezone.utc).isoformat(),
                "uptime_sec": int(max(0, time.time() - self._started_at)),
                "queue_depth": int(self._send_q.qsize()),
                "queue_max": int(self.queue_max),
                "queue_drop_oldest": bool(self.queue_drop_oldest),
                "queued_total": int(self._queued_total),
                "sent_total": int(self._sent_total),
                "failed_total": int(self._failed_total),
                "duplicate_total": int(self._duplicate_total),
                "queue_dropped_total": int(self._queue_dropped_total),
                "queue_full_rejected_total": int(self._queue_full_rejected_total),
                "latency_samples": int(len(latencies)),
                "latency_avg_ms": avg_ms,
                "latency_p95_ms": p95_ms,
                "last_send_error": self._last_send_error or None,
                "alert_transitions_total": int(self._alert_transition_total),
            }

    @staticmethod
    def _prom_escape(value: object) -> str:
        return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def _metrics_prometheus_text(self) -> str:
        code = self._status_code_payload(with_schema=True)
        self._track_alert_transition(code.get("in_alert"))
        metrics = self.metrics_snapshot()

        def _num(value: object, default: float = 0.0) -> float:
            try:
                if isinstance(value, bool):
                    return 1.0 if value else 0.0
                return float(value)  # type: ignore[arg-type]
            except Exception:
                return float(default)

        def _bool_num(value: object) -> float:
            if isinstance(value, bool):
                return 1.0 if value else 0.0
            return -1.0

        in_alert_val = _bool_num(code.get("in_alert"))
        stale_val = _bool_num(code.get("stale"))
        fails_val = _num(code.get("fails"), -1.0)
        state_raw = str(code.get("state", "unknown")).strip().lower()
        in_alert_raw = code.get("in_alert")
        in_alert_bool = in_alert_raw if isinstance(in_alert_raw, bool) else None
        try:
            fails_raw_num = float(code.get("fails"))  # type: ignore[arg-type]
        except Exception:
            fails_raw_num = None
        if in_alert_bool is None or fails_raw_num is None:
            should_page_val = -1.0
        elif state_raw == "critical" and in_alert_bool and fails_raw_num > 0:
            should_page_val = 1.0
        else:
            should_page_val = 0.0
        quick_lane_raw = str(code.get("quick_lane", "unknown")).strip().lower()
        quick_lane_state_val = {
            "ok": 0.0,
            "disabled": 1.0,
            "stale": 2.0,
            "missing": 3.0,
            "failed": 4.0,
        }.get(quick_lane_raw, -1.0)
        reply_auth_raw = str(code.get("reply_auth", "unknown")).strip().lower()
        reply_auth_state_val = {
            "ok": 0.0,
            "disabled": 1.0,
            "fail": 2.0,
        }.get(reply_auth_raw, -1.0)

        state = self._prom_escape(code.get("state", "unknown"))
        hint_code = self._prom_escape(code.get("hint_code", "unknown_check_status"))
        severity = self._prom_escape(code.get("severity", "unknown"))
        quick_lane = self._prom_escape(code.get("quick_lane", "unknown"))
        reply_auth = self._prom_escape(code.get("reply_auth", "unknown"))
        sig8 = self._prom_escape(code.get("sig8", "(unknown)"))
        schema = self._prom_escape(code.get("schema", STATUS_CODE_SCHEMA))
        version = self._prom_escape(code.get("version", STATUS_CODE_VERSION))
        in_alert_label = self._prom_escape(str(code.get("in_alert", "(unknown)")).lower())
        stale_label = self._prom_escape(str(code.get("stale", "(unknown)")).lower())

        lines = [
            "# HELP yuuki_reply_queue_depth Current reply queue depth.",
            "# TYPE yuuki_reply_queue_depth gauge",
            f"yuuki_reply_queue_depth {_num(metrics.get('queue_depth'), 0.0):g}",
            "# HELP yuuki_reply_sent_total Total replies sent.",
            "# TYPE yuuki_reply_sent_total counter",
            f"yuuki_reply_sent_total {_num(metrics.get('sent_total'), 0.0):g}",
            "# HELP yuuki_reply_failed_total Total reply send failures.",
            "# TYPE yuuki_reply_failed_total counter",
            f"yuuki_reply_failed_total {_num(metrics.get('failed_total'), 0.0):g}",
            "# HELP yuuki_reply_queue_dropped_total Total dropped queued replies due to drop-oldest policy.",
            "# TYPE yuuki_reply_queue_dropped_total counter",
            f"yuuki_reply_queue_dropped_total {_num(metrics.get('queue_dropped_total'), 0.0):g}",
            "# HELP yuuki_reply_queue_full_rejected_total Total rejected replies when queue is full and drop-oldest is disabled.",
            "# TYPE yuuki_reply_queue_full_rejected_total counter",
            f"yuuki_reply_queue_full_rejected_total {_num(metrics.get('queue_full_rejected_total'), 0.0):g}",
            "# HELP yuuki_health_fail_count Health fail count from statuscode.",
            "# TYPE yuuki_health_fail_count gauge",
            f"yuuki_health_fail_count {fails_val:g}",
            "# HELP yuuki_health_in_alert Whether alert is active (1=true,0=false,-1=unknown).",
            "# TYPE yuuki_health_in_alert gauge",
            f"yuuki_health_in_alert {in_alert_val:g}",
            "# HELP yuuki_health_stale Whether health snapshot is stale (1=true,0=false,-1=unknown).",
            "# TYPE yuuki_health_stale gauge",
            f"yuuki_health_stale {stale_val:g}",
            "# HELP yuuki_health_quick_lane_state Quick-lane state (ok=0,disabled=1,stale=2,missing=3,failed=4,unknown=-1).",
            "# TYPE yuuki_health_quick_lane_state gauge",
            f"yuuki_health_quick_lane_state {quick_lane_state_val:g}",
            "# HELP yuuki_reply_auth_probe_state Reply auth probe state from statuscode (ok=0,disabled=1,fail=2,unknown=-1).",
            "# TYPE yuuki_reply_auth_probe_state gauge",
            f"yuuki_reply_auth_probe_state {reply_auth_state_val:g}",
            "# HELP yuuki_alert_should_page Whether alert should page (1=yes,0=no,-1=unknown).",
            "# TYPE yuuki_alert_should_page gauge",
            f"yuuki_alert_should_page {should_page_val:g}",
            "# HELP yuuki_alert_transitions_total Total in_alert state transitions (flapping signal).",
            "# TYPE yuuki_alert_transitions_total counter",
            f"yuuki_alert_transitions_total {_num(metrics.get('alert_transitions_total'), 0.0):g}",
            "# HELP yuuki_statuscode_info Compact status labels exported as info metric.",
            "# TYPE yuuki_statuscode_info gauge",
            (
                "yuuki_statuscode_info{"
                f'state="{state}",'
                f'hint_code="{hint_code}",'
                f'severity="{severity}",'
                f'quick_lane="{quick_lane}",'
                f'reply_auth="{reply_auth}",'
                f'sig8="{sig8}",'
                f'schema="{schema}",'
                f'version="{version}",'
                f'in_alert="{in_alert_label}",'
                f'stale="{stale_label}"'
                "} 1"
            ),
        ]
        return "\n".join(lines) + "\n"

    def _enqueue_reply(self, chat_id: int, text: str) -> tuple[bool, bool]:
        payload = (int(chat_id), text)
        try:
            self._send_q.put_nowait(payload)
            self._mark_queued()
            log_event("reply_queued", chat_id=int(chat_id), chars=len(text), queue_depth=int(self._send_q.qsize()))
            return True, False
        except queue.Full:
            if self.queue_drop_oldest:
                try:
                    dropped_chat_id, dropped_text = self._send_q.get_nowait()
                    self._send_q.task_done()
                    self._mark_queue_dropped()
                    log_event(
                        "reply_queue_drop_oldest",
                        dropped_chat_id=int(dropped_chat_id),
                        dropped_chars=len(dropped_text or ""),
                        queue_depth=int(self._send_q.qsize()),
                    )
                except queue.Empty:
                    pass
                try:
                    self._send_q.put_nowait(payload)
                    self._mark_queued()
                    log_event(
                        "reply_queued",
                        chat_id=int(chat_id),
                        chars=len(text),
                        queue_depth=int(self._send_q.qsize()),
                        dropped_oldest=True,
                    )
                    return True, True
                except queue.Full:
                    self._mark_queue_rejected()
                    log_event(
                        "reply_queue_full",
                        chat_id=int(chat_id),
                        chars=len(text),
                        queue_depth=int(self._send_q.qsize()),
                        drop_oldest=bool(self.queue_drop_oldest),
                    )
                    return False, False
            self._mark_queue_rejected()
            log_event(
                "reply_queue_full",
                chat_id=int(chat_id),
                chars=len(text),
                queue_depth=int(self._send_q.qsize()),
                drop_oldest=bool(self.queue_drop_oldest),
            )
            return False, False

    def _send_worker(self) -> None:
        while True:
            chat_id, text = self._send_q.get()
            try:
                sent = False
                last_err = ""
                for attempt in range(1, int(self.send_retries) + 1):
                    try:
                        t0 = time.time()
                        self.tg.send(int(chat_id), text)
                        elapsed_ms = int((time.time() - t0) * 1000)
                        self._mark_sent(elapsed_ms)
                        log_event(
                            "reply_sent",
                            chat_id=int(chat_id),
                            chars=len(text),
                            attempt=attempt,
                            latency_ms=elapsed_ms,
                        )
                        sent = True
                        break
                    except Exception as exc:
                        last_err = str(exc)
                        if attempt >= int(self.send_retries):
                            break
                        sleep_s = (int(self.send_backoff_ms) / 1000.0) * (2 ** (attempt - 1))
                        LOG.warning(
                            "reply-api send retry chat_id=%s attempt=%s/%s backoff=%.2fs err=%s",
                            chat_id,
                            attempt,
                            self.send_retries,
                            sleep_s,
                            last_err[:180],
                        )
                        log_event(
                            "reply_retry",
                            chat_id=int(chat_id),
                            chars=len(text),
                            attempt=attempt,
                            retries=int(self.send_retries),
                            error=last_err[:180],
                        )
                        time.sleep(sleep_s)
                if not sent:
                    self._mark_failed(last_err)
                    LOG.error(
                        "reply-api send failed chat_id=%s retries=%s err=%s",
                        chat_id,
                        self.send_retries,
                        last_err[:220],
                    )
                    log_event(
                        "reply_failed",
                        chat_id=int(chat_id),
                        chars=len(text),
                        retries=int(self.send_retries),
                        error=last_err[:220],
                    )
            finally:
                self._send_q.task_done()


def _cmd_and_arg(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if not raw.startswith("/"):
        return "", raw

    parts = raw.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def _pick_image_file(msg: dict) -> tuple[str, str] | None:
    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        item = photos[-1] if isinstance(photos[-1], dict) else {}
        file_id = (item.get("file_id") or "").strip()
        if file_id:
            return file_id, ".jpg"

    doc = msg.get("document")
    if not isinstance(doc, dict):
        return None

    mime = (doc.get("mime_type") or "").strip().lower()
    if not mime.startswith("image/"):
        return None

    file_id = (doc.get("file_id") or "").strip()
    if not file_id:
        return None

    suffix = Path((doc.get("file_name") or "").strip()).suffix.lower()
    if suffix:
        return file_id, suffix
    if mime == "image/png":
        return file_id, ".png"
    if mime == "image/webp":
        return file_id, ".webp"
    return file_id, ".jpg"


def _safe_file_name(value: str, fallback: str = "file") -> str:
    raw = Path((value or "").strip()).name
    stem = raw or fallback
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    if not stem:
        stem = fallback
    return stem[:120]


def _pick_document_file(msg: dict) -> tuple[str, str, str, int] | None:
    doc = msg.get("document")
    if not isinstance(doc, dict):
        return None

    mime = (doc.get("mime_type") or "").strip().lower()
    if mime.startswith("image/"):
        return None

    file_id = (doc.get("file_id") or "").strip()
    if not file_id:
        return None

    file_name = _safe_file_name(str(doc.get("file_name") or ""))
    if "." not in file_name and mime == "text/markdown":
        file_name = f"{file_name}.md"
    elif "." not in file_name and mime == "text/plain":
        file_name = f"{file_name}.txt"
    elif "." not in file_name and mime == "application/pdf":
        file_name = f"{file_name}.pdf"

    size = int(doc.get("file_size") or 0)
    return file_id, file_name, mime, max(0, size)


def _build_image_saved_text(path: Path, caption: str, chat_id: int, message_id: int, relayed: bool) -> str:
    return "\n".join(
        [
            "IMAGE_SAVED",
            f"path={path}",
            f"caption={caption or '(empty)'}",
            f"chat_id={chat_id}",
            f"message_id={message_id}",
            f"relayed_to_tmux={str(relayed).lower()}",
        ]
    )


def _build_file_saved_text(
    path: Path,
    file_name: str,
    mime_type: str,
    size_bytes: int,
    caption: str,
    chat_id: int,
    message_id: int,
    relayed: bool,
) -> str:
    return "\n".join(
        [
            "FILE_SAVED",
            f"path={path}",
            f"file_name={file_name or '(unknown)'}",
            f"mime_type={mime_type or '(unknown)'}",
            f"size_bytes={max(0, int(size_bytes))}",
            f"caption={caption or '(empty)'}",
            f"chat_id={chat_id}",
            f"message_id={message_id}",
            f"relayed_to_tmux={str(relayed).lower()}",
        ]
    )


def _format_archive_status(cfg: Config) -> str:
    return "\n".join(
        [
            f"enabled={cfg.ai_archive_enabled}",
            f"repo={cfg.ai_archive_repo_dir}",
            f"push={cfg.ai_archive_push}",
            f"max_bytes={cfg.ai_archive_max_bytes}",
        ]
    )


def _read_json_dict(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def _read_health_snapshot(path: Path) -> dict[str, object]:
    return _read_json_dict(path)


def _read_alert_state_snapshot(path: Path) -> dict[str, object]:
    return _read_json_dict(path)


def _read_watcher_snapshot(path: Path) -> dict[str, object]:
    return _read_json_dict(path)


def _read_self_improve_snapshot(path: Path) -> dict[str, object]:
    return _read_json_dict(path)


def _read_self_test_snapshot(path: Path) -> dict[str, object]:
    return _read_json_dict(path)


def _read_tail_lines(path: Path, max_bytes: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size <= 0:
                return []
            read_size = min(int(max_bytes), int(size))
            f.seek(-read_size, 2)
            data = f.read(read_size)
        if read_size < size:
            cut = data.find(b"\n")
            if cut >= 0:
                data = data[cut + 1 :]
        return data.decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return []


def _recent_event_summary(log_path: Path, lookback_min: int, scan_max_bytes: int) -> tuple[dict[str, int], dict[str, object]]:
    keys = [
        "reply_retry",
        "reply_failed",
        "relay_error",
        "image_intake_failed",
        "file_intake_failed",
        "plain_text_deduped",
        "reply_queue_full",
        "reply_queue_drop_oldest",
    ]
    counts: dict[str, int] = {k: 0 for k in keys}
    meta: dict[str, object] = {
        "scan_bytes": 0,
        "file_size_bytes": 0,
        "complete": True,
        "oldest_ts_utc": "(unknown)",
    }
    if not log_path.exists():
        return counts, meta
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - float(max(1, lookback_min) * 60)
    try:
        file_size = int(log_path.stat().st_size)
        scan_bytes = min(file_size, max(64_000, int(scan_max_bytes)))
        lines = _read_tail_lines(log_path, scan_bytes)
        oldest_ts_epoch: float | None = None
        oldest_ts_utc = "(unknown)"
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            et = obj.get("event_type")
            if et not in counts:
                continue
            ts = obj.get("ts_utc")
            if not isinstance(ts, str) or not ts:
                continue
            try:
                ts_norm = ts.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_norm)
                ts_epoch = dt.timestamp()
            except Exception:
                continue
            if oldest_ts_epoch is None or ts_epoch < oldest_ts_epoch:
                oldest_ts_epoch = ts_epoch
                oldest_ts_utc = ts
            if ts_epoch >= cutoff:
                counts[str(et)] += 1
        complete = bool(file_size <= scan_bytes or (oldest_ts_epoch is not None and oldest_ts_epoch <= cutoff))
        meta = {
            "scan_bytes": int(scan_bytes),
            "file_size_bytes": int(file_size),
            "complete": bool(complete),
            "oldest_ts_utc": oldest_ts_utc,
        }
    except Exception:
        return counts, meta
    return counts, meta


def _archive_file_to_ai_repo(
    cfg: Config,
    source_path: Path,
    file_name: str,
    mime_type: str,
    size_bytes: int,
    caption: str,
    chat_id: int,
    message_id: int,
) -> tuple[bool, str]:
    if not cfg.ai_archive_enabled:
        return False, "archive_disabled"

    repo = cfg.ai_archive_repo_dir
    if not repo.exists():
        return False, f"repo_not_found:{repo}"
    if not (repo / ".git").exists():
        return False, f"not_git_repo:{repo}"
    if not source_path.exists():
        return False, f"source_missing:{source_path}"
    if int(size_bytes) > int(cfg.ai_archive_max_bytes):
        return False, f"too_large:{size_bytes}>{cfg.ai_archive_max_bytes}"

    try:
        now = datetime.now(timezone.utc)
        out_dir = repo / "human_posts" / now.strftime("%Y%m%d")
        out_dir.mkdir(parents=True, exist_ok=True)

        safe_name = _safe_file_name(file_name or source_path.name, fallback=source_path.name)
        dst = out_dir / f"tg_{chat_id}_{message_id}_{safe_name}"
        shutil.copy2(source_path, dst)

        meta = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "saved_at_utc": now.isoformat(),
            "source_path": str(source_path),
            "archived_path": str(dst),
            "file_name": file_name,
            "mime_type": mime_type,
            "size_bytes": int(size_bytes),
            "caption": caption or "",
        }
        meta_path = out_dir / f"{dst.name}.meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        rel_dst = dst.relative_to(repo)
        rel_meta = meta_path.relative_to(repo)

        subprocess.run(
            ["git", "-C", str(repo), "add", "--", str(rel_dst), str(rel_meta)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            text=True,
        )

        staged = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            text=True,
        )
        if not staged.stdout.strip():
            return True, f"no_changes:{rel_dst}"

        commit_msg = f"context: archive telegram file chat={chat_id} msg={message_id}"
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", commit_msg],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=40,
            text=True,
        )
        rev = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            text=True,
        ).stdout.strip()

        if cfg.ai_archive_push:
            subprocess.run(
                ["git", "-C", str(repo), "push"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=90,
                text=True,
            )
            return True, f"archived:{rel_dst} commit={rev} pushed=true"
        return True, f"archived:{rel_dst} commit={rev} pushed=false"
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or exc.stdout or str(exc)).strip().replace("\n", " ")
        return False, f"git_error:{msg[:220]}"
    except Exception as exc:
        return False, f"archive_error:{str(exc)[:220]}"


def _format_tmux_status(relay: TmuxRelay) -> str:
    return "\n".join(
        [
            f"enabled={relay.enabled}",
            f"available={relay.is_available()}",
            f"target={relay.target_pane or '(unset)'}",
            f"plain_text_to_tmux={relay.plain_text_to_tmux}",
        ]
    )


def _runtime_status_payload(cfg: Config, relay: TmuxRelay, piper: PiperTTS, reply_api: LocalReplyAPI) -> dict[str, object]:
    metrics = reply_api.metrics_snapshot()
    health = _read_health_snapshot(cfg.health_status_path)
    alert_state = _read_alert_state_snapshot(cfg.health_alert_state_path)
    watcher = _read_watcher_snapshot(cfg.watcher_state_path)
    self_improve = _read_self_improve_snapshot(cfg.self_improve_state_path)
    self_test = _read_self_test_snapshot(cfg.self_test_status_path)
    events, event_meta = _recent_event_summary(
        cfg.json_log_path,
        cfg.status_event_lookback_min,
        cfg.status_event_scan_max_bytes,
    )
    event_scan_retry_used = False
    event_scan_retry_count = 0
    max_scan_retries = 4
    while (not bool(event_meta.get("complete"))) and event_scan_retry_count < max_scan_retries:
        file_size = int(event_meta.get("file_size_bytes") or 0)
        current_scan = int(event_meta.get("scan_bytes") or cfg.status_event_scan_max_bytes)
        if file_size <= 0 or current_scan >= file_size:
            break
        retry_bytes = min(file_size, max(current_scan * 2, current_scan + 1_048_576))
        if retry_bytes <= current_scan:
            break
        events_retry, event_meta_retry = _recent_event_summary(
            cfg.json_log_path,
            cfg.status_event_lookback_min,
            retry_bytes,
        )
        events = events_retry
        event_meta = event_meta_retry
        event_scan_retry_used = True
        event_scan_retry_count += 1
    health_ok = health.get("ok")
    health_last = health.get("ts_utc")
    health_fail_count = health.get("fail_count")
    health_fails = health.get("fails")
    health_fails_list = health_fails if isinstance(health_fails, list) else []
    health_fails_top: list[str] = []
    for item in health_fails_list[:3]:
        s = str(item).strip()
        if not s:
            continue
        if len(s) > 80:
            s = s[:77] + "..."
        health_fails_top.append(s)
    health_age_sec: object = "(unknown)"
    health_stale: object = "(unknown)"
    if isinstance(health_last, str) and health_last.strip():
        try:
            dt = datetime.fromisoformat(health_last.replace("Z", "+00:00"))
            age = int((datetime.now(timezone.utc) - dt).total_seconds())
            if age < 0:
                age = 0
            health_age_sec = age
            health_stale = age > int(cfg.status_health_stale_sec)
        except Exception:
            health_age_sec = "(unknown)"
            health_stale = "(unknown)"
    health_alert = health.get("alert")
    health_alert_severity = "(unknown)"
    health_alert_cooldown_sec = "(unknown)"
    if isinstance(health_alert, dict):
        health_alert_severity = str(health_alert.get("severity") or "(unknown)")
        health_alert_cooldown_sec = (
            health_alert.get("cooldown_sec_effective")
            if health_alert.get("cooldown_sec_effective") is not None
            else "(unknown)"
        )
    elif health_ok is True:
        health_alert_severity = "none"
        health_alert_cooldown_sec = 0
    now_epoch = int(time.time())
    alert_in_alert = alert_state.get("in_alert")
    alert_last_epoch = alert_state.get("last_alert_epoch")
    alert_cooldown = alert_state.get("cooldown_sec")
    alert_last_severity = alert_state.get("last_severity") or "(unknown)"
    alert_last_fingerprint = alert_state.get("last_fingerprint") or "(unknown)"
    alert_sig_short = "(unknown)"
    alert_sig_hash8 = "(unknown)"
    raw_alert_sig = str(alert_last_fingerprint).strip()
    if raw_alert_sig and raw_alert_sig != "(unknown)":
        normalized_sig = raw_alert_sig.rstrip("|") or raw_alert_sig
        alert_sig_short = normalized_sig if len(normalized_sig) <= 72 else (normalized_sig[:69] + "...")
        alert_sig_hash8 = hashlib.sha256(normalized_sig.encode("utf-8")).hexdigest()[:8]
    alert_cooldown_left_sec: object = "(unknown)"
    alert_last_age_sec: object = "(unknown)"
    in_alert_bool: bool | None = None
    if isinstance(alert_in_alert, bool):
        in_alert_bool = alert_in_alert
        if not alert_in_alert:
            alert_cooldown_left_sec = 0
            if raw_alert_sig in {"", "(unknown)"}:
                alert_sig_short = "(none)"
                alert_sig_hash8 = "(none)"
    try:
        last_epoch_i = int(alert_last_epoch)
        age = now_epoch - last_epoch_i
        if age < 0:
            age = 0
        alert_last_age_sec = age
    except Exception:
        alert_last_age_sec = 0 if in_alert_bool is False else "(unknown)"
    if in_alert_bool is True:
        try:
            last_epoch_i = int(alert_last_epoch)
            cooldown_i = max(0, int(alert_cooldown))
            left = (last_epoch_i + cooldown_i) - now_epoch
            alert_cooldown_left_sec = left if left > 0 else 0
        except Exception:
            alert_cooldown_left_sec = "(unknown)"
    health_effective_state = "unknown"
    if health_ok is True:
        health_effective_state = "stale" if health_stale is True else "ok"
    elif health_ok is False:
        sev = str(health_alert_severity).strip().lower()
        health_effective_state = "degraded" if sev == "warning" else "critical"
    elif health_stale is True:
        health_effective_state = "stale"
    health_operator_hint = "check /status and health snapshot"
    health_operator_hint_code = "unknown_check_status"
    if health_effective_state == "ok":
        health_operator_hint = "none"
        health_operator_hint_code = "ok_none"
    elif health_effective_state == "stale":
        health_operator_hint = "run ./scripts/health_check.sh and verify health-check timer"
        health_operator_hint_code = "stale_run_health_check"
    elif health_effective_state == "degraded":
        health_operator_hint = "inspect self_test freshness and relay/reply error counters"
        health_operator_hint_code = "degraded_check_selftest_counters"
    elif health_effective_state == "critical":
        health_operator_hint = "check bot service, /health endpoint, and tmux target session"
        health_operator_hint_code = "critical_check_service_api_tmux"
    health_self_test = health.get("self_test")
    if isinstance(health_self_test, dict):
        _health_self_test_failed_checks = health_self_test.get("failed_checks")
        _health_self_test_repeated_checks = health_self_test.get("repeated_fail_checks")
        health_self_test_failed_checks = (
            [str(x).strip() for x in _health_self_test_failed_checks if str(x).strip()]
            if isinstance(_health_self_test_failed_checks, list)
            else []
        )
        health_self_test_repeated_checks = (
            [str(x).strip() for x in _health_self_test_repeated_checks if str(x).strip()]
            if isinstance(_health_self_test_repeated_checks, list)
            else []
        )
        health_self_test_fail_streak_threshold = (
            health_self_test.get("fail_streak_threshold")
            if health_self_test.get("fail_streak_threshold") is not None
            else "(unknown)"
        )
        _autorepair_enabled = health_self_test.get("autorepair_enabled")
        health_self_test_autorepair_enabled = (
            bool(_autorepair_enabled) if isinstance(_autorepair_enabled, bool) else "(unknown)"
        )
        _autorepair_attempted = health_self_test.get("autorepair_attempted")
        health_self_test_autorepair_attempted = (
            bool(_autorepair_attempted) if isinstance(_autorepair_attempted, bool) else "(unknown)"
        )
        health_self_test_autorepair_result = str(health_self_test.get("autorepair_result") or "(unknown)")
        health_self_test_autorepair_timeout_sec = (
            health_self_test.get("autorepair_timeout_sec")
            if health_self_test.get("autorepair_timeout_sec") is not None
            else "(unknown)"
        )
        health_self_test_autorepair_script = str(health_self_test.get("autorepair_script") or "(unknown)")
    else:
        health_self_test_failed_checks = []
        health_self_test_repeated_checks = []
        health_self_test_fail_streak_threshold = "(unknown)"
        health_self_test_autorepair_enabled = "(unknown)"
        health_self_test_autorepair_attempted = "(unknown)"
        health_self_test_autorepair_result = "(unknown)"
        health_self_test_autorepair_timeout_sec = "(unknown)"
        health_self_test_autorepair_script = "(unknown)"
    health_self_test_quick = health.get("self_test_quick")
    if isinstance(health_self_test_quick, dict):
        _quick_required = health_self_test_quick.get("required")
        health_self_test_quick_required = bool(_quick_required) if isinstance(_quick_required, bool) else "(unknown)"
        health_self_test_quick_path = str(health_self_test_quick.get("path") or "(unknown)")
        health_self_test_quick_max_age_sec = (
            health_self_test_quick.get("max_age_sec")
            if health_self_test_quick.get("max_age_sec") is not None
            else "(unknown)"
        )
        _quick_exists = health_self_test_quick.get("exists")
        health_self_test_quick_exists = bool(_quick_exists) if isinstance(_quick_exists, bool) else "(unknown)"
        _quick_ok = health_self_test_quick.get("ok")
        health_self_test_quick_ok = bool(_quick_ok) if isinstance(_quick_ok, bool) else "(unknown)"
        _quick_stale = health_self_test_quick.get("stale")
        health_self_test_quick_stale = bool(_quick_stale) if isinstance(_quick_stale, bool) else "(unknown)"
        health_self_test_quick_age_sec = (
            health_self_test_quick.get("age_sec")
            if health_self_test_quick.get("age_sec") is not None
            else "(unknown)"
        )
        health_self_test_quick_ts_utc = str(health_self_test_quick.get("ts_utc") or "(unknown)")
        _quick_failed_checks = health_self_test_quick.get("failed_checks")
        health_self_test_quick_failed_checks = (
            [str(x).strip() for x in _quick_failed_checks if str(x).strip()]
            if isinstance(_quick_failed_checks, list)
            else []
        )
    else:
        health_self_test_quick_required = "(unknown)"
        health_self_test_quick_path = "(unknown)"
        health_self_test_quick_max_age_sec = "(unknown)"
        health_self_test_quick_exists = "(unknown)"
        health_self_test_quick_ok = "(unknown)"
        health_self_test_quick_stale = "(unknown)"
        health_self_test_quick_age_sec = "(unknown)"
        health_self_test_quick_ts_utc = "(unknown)"
        health_self_test_quick_failed_checks = []
    if health_self_test_quick_required is True:
        if health_self_test_quick_exists is not True:
            health_self_test_quick_state = "missing"
        elif health_self_test_quick_ok is not True:
            health_self_test_quick_state = "failed"
        elif health_self_test_quick_stale is True:
            health_self_test_quick_state = "stale"
        else:
            health_self_test_quick_state = "ok"
    elif health_self_test_quick_required is False:
        health_self_test_quick_state = "disabled"
    else:
        health_self_test_quick_state = "unknown"
    health_self_test_quick_operator_hint = "none"
    health_self_test_quick_operator_hint_code = "quick_lane_none"
    if health_self_test_quick_state in {"missing", "failed", "stale"}:
        health_self_test_quick_operator_hint = (
            "inspect quick self-test timer/service and quick snapshot freshness"
        )
        health_self_test_quick_operator_hint_code = f"quick_lane_{health_self_test_quick_state}_check_service_timer"

    health_reply_api = health.get("reply_api")
    if isinstance(health_reply_api, dict):
        _reply_auth_probe_enabled = health_reply_api.get("auth_probe_enabled")
        health_reply_auth_probe_enabled = (
            bool(_reply_auth_probe_enabled) if isinstance(_reply_auth_probe_enabled, bool) else "(unknown)"
        )
        _reply_auth_probe_ok = health_reply_api.get("auth_probe_ok")
        health_reply_auth_probe_ok = (
            bool(_reply_auth_probe_ok) if isinstance(_reply_auth_probe_ok, bool) else "(unknown)"
        )
        health_reply_auth_probe_http = str(health_reply_api.get("auth_probe_http") or "(unknown)")
    else:
        health_reply_auth_probe_enabled = "(unknown)"
        health_reply_auth_probe_ok = "(unknown)"
        health_reply_auth_probe_http = "(unknown)"
    if health_reply_auth_probe_enabled is True:
        health_reply_auth_probe_state = "ok" if health_reply_auth_probe_ok is True else "fail"
    elif health_reply_auth_probe_enabled is False:
        health_reply_auth_probe_state = "disabled"
    else:
        health_reply_auth_probe_state = "unknown"

    if health_effective_state in {"degraded", "critical"} and any(
        str(x).startswith("self_test_quick:") for x in health_fails_list
    ):
        health_operator_hint = "inspect quick self-test timer/service and quick snapshot path"
        health_operator_hint_code = f"{health_effective_state}_check_self_test_quick_lane"
    if health_effective_state in {"degraded", "critical"} and any(
        str(x).startswith("reply_api_auth_probe=") for x in health_fails_list
    ):
        health_operator_hint = "inspect local /reply auth probe path, token wiring, and reply API service state"
        health_operator_hint_code = f"{health_effective_state}_check_reply_api_auth_probe"
    if health_effective_state in {"degraded", "critical"} and health_self_test_autorepair_attempted is True:
        ar = str(health_self_test_autorepair_result).strip() or "(unknown)"
        ar_token = re.sub(r"[^a-z0-9_]+", "_", ar.lower()).strip("_") or "unknown"
        if ar == "recovered":
            health_operator_hint = "self-test autorepair recovered; verify counters and rerun health-check"
            health_operator_hint_code = f"{health_effective_state}_autorepair_recovered"
        else:
            health_operator_hint = (
                f"self-test autorepair={ar}; inspect self-test script/logs and rerun health-check"
            )
            health_operator_hint_code = f"{health_effective_state}_autorepair_{ar_token}"
    self_test_last = self_test.get("ts_utc")
    self_test_age_sec: object = "(unknown)"
    self_test_stale: object = "(unknown)"
    if isinstance(self_test_last, str) and self_test_last.strip():
        try:
            dt = datetime.fromisoformat(self_test_last.replace("Z", "+00:00"))
            age = int((datetime.now(timezone.utc) - dt).total_seconds())
            if age < 0:
                age = 0
            self_test_age_sec = age
            self_test_stale = age > int(cfg.self_test_max_age_sec)
        except Exception:
            self_test_age_sec = "(unknown)"
            self_test_stale = "(unknown)"
    self_test_checks = self_test.get("checks")
    self_test_checks_total: object = "(unknown)"
    self_test_failed_names: list[str] = []
    if isinstance(self_test_checks, list):
        self_test_checks_total = len(self_test_checks)
        for item in self_test_checks:
            if not isinstance(item, dict):
                continue
            if item.get("ok") is False:
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    self_test_failed_names.append(name.strip())
    if not self_test_failed_names and isinstance(self_test.get("failures"), list):
        self_test_failed_names = [str(x).strip() for x in self_test.get("failures") if str(x).strip()]
    event_reply_queue_full = int(events.get("reply_queue_full", 0) or 0)
    event_reply_queue_drop_oldest = int(events.get("reply_queue_drop_oldest", 0) or 0)
    queue_pressure_reasons: list[str] = []
    if event_reply_queue_full > 0:
        queue_pressure_reasons.append("queue_full")
    if event_reply_queue_drop_oldest > 0:
        queue_pressure_reasons.append("drop_oldest")
    queue_pressure_count = event_reply_queue_full + event_reply_queue_drop_oldest
    queue_pressure_threshold = int(cfg.status_queue_pressure_critical_threshold)
    if queue_pressure_count >= queue_pressure_threshold:
        queue_pressure_state = "critical"
    elif queue_pressure_reasons:
        queue_pressure_state = "warn"
    else:
        queue_pressure_state = "ok"
    queue_pressure_operator_hint = "none"
    queue_pressure_operator_hint_code = "queue_pressure_none"
    if queue_pressure_state == "critical":
        queue_pressure_operator_hint = (
            "reply queue pressure is critical; inspect burst traffic and tune queue/retry/backoff settings"
        )
        queue_pressure_operator_hint_code = "queue_pressure_critical_check_backpressure"
    elif queue_pressure_state == "warn":
        queue_pressure_operator_hint = "queue pressure detected; monitor reply queue and recent burst traffic"
        queue_pressure_operator_hint_code = "queue_pressure_warn_monitor"
    if queue_pressure_state == "critical":
        health_operator_hint = queue_pressure_operator_hint
        health_operator_hint_code = queue_pressure_operator_hint_code
    return {
        "tmux_available": relay.is_available(),
        "tmux_target": relay.target_pane or "(unset)",
        "tmux_debounce_enabled": cfg.tmux_debounce_enabled,
        "tmux_debounce_sec": cfg.tmux_debounce_sec,
        "tmux_plain_text_dedupe_enabled": cfg.tmux_plain_text_dedupe_enabled,
        "tmux_plain_text_dedupe_sec": cfg.tmux_plain_text_dedupe_sec,
        "piper_enabled": piper.enabled,
        "piper_langs": piper.available_langs(),
        "archive_enabled": cfg.ai_archive_enabled,
        "archive_repo": str(cfg.ai_archive_repo_dir),
        "json_log_enabled": cfg.json_log_enabled,
        "json_log_path": str(cfg.json_log_path),
        "health_status_path": str(cfg.health_status_path),
        "health_alert_state_path": str(cfg.health_alert_state_path),
        "watcher_enabled": cfg.watcher_enabled,
        "watcher_idle_sec": cfg.watcher_idle_sec,
        "watcher_cooldown_sec": cfg.watcher_cooldown_sec,
        "watcher_state_path": str(cfg.watcher_state_path),
        "watcher_task_state_path": str(cfg.watcher_task_state_path),
        "watcher_last_run_ts_utc": watcher.get("last_run_ts_utc") or "(unknown)",
        "watcher_last_action": watcher.get("last_action") or "(unknown)",
        "watcher_last_reason": watcher.get("last_reason") or "(unknown)",
        "watcher_last_idle_sec": watcher.get("idle_sec") if watcher.get("idle_sec") is not None else "(unknown)",
        "watcher_require_healthy": watcher.get("watcher_require_healthy")
        if watcher.get("watcher_require_healthy") is not None
        else "(unknown)",
        "watcher_health_state": watcher.get("health_state") or "(unknown)",
        "watcher_health_age_sec": watcher.get("health_age_sec")
        if watcher.get("health_age_sec") is not None
        else "(unknown)",
        "watcher_health_stale": watcher.get("health_stale")
        if watcher.get("health_stale") is not None
        else "(unknown)",
        "self_improve_state_path": str(cfg.self_improve_state_path),
        "self_improve_last_run_ts_utc": self_improve.get("last_run_ts_utc") or "(unknown)",
        "self_improve_last_action": self_improve.get("last_action") or "(unknown)",
        "self_improve_last_reason": self_improve.get("last_reason") or "(unknown)",
        "self_improve_require_healthy": self_improve.get("require_healthy")
        if self_improve.get("require_healthy") is not None
        else "(unknown)",
        "self_improve_health_state": self_improve.get("health_state") or "(unknown)",
        "self_improve_health_age_sec": self_improve.get("health_age_sec")
        if self_improve.get("health_age_sec") is not None
        else "(unknown)",
        "self_improve_health_stale": self_improve.get("health_stale")
        if self_improve.get("health_stale") is not None
        else "(unknown)",
        "self_improve_target_pane": self_improve.get("target_pane") or "(unknown)",
        "self_improve_submit_key": self_improve.get("submit_key") or "(unknown)",
        "self_improve_task_running_detected": self_improve.get("task_running_detected")
        if self_improve.get("task_running_detected") is not None
        else "(unknown)",
        "self_improve_min_interval_sec": self_improve.get("min_interval_sec")
        if self_improve.get("min_interval_sec") is not None
        else "(unknown)",
        "self_improve_last_sent_epoch": self_improve.get("last_sent_epoch")
        if self_improve.get("last_sent_epoch") is not None
        else "(unknown)",
        "self_test_status_path": str(cfg.self_test_status_path),
        "self_test_last_run_ts_utc": self_test.get("ts_utc") or "(unknown)",
        "self_test_ok": self_test.get("ok") if self_test.get("ok") is not None else "(unknown)",
        "self_test_max_age_sec": cfg.self_test_max_age_sec,
        "self_test_age_sec": self_test_age_sec,
        "self_test_stale": self_test_stale,
        "self_test_checks_total": self_test_checks_total,
        "self_test_failed_names": self_test_failed_names,
        "self_test_failure_count": len(self_test.get("failures") or [])
        if isinstance(self_test.get("failures"), list)
        else "(unknown)",
        "health_last_ts_utc": health_last or "(unknown)",
        "health_ok": health_ok if health_ok is not None else "(unknown)",
        "health_fail_count": health_fail_count if health_fail_count is not None else "(unknown)",
        "health_fails": health_fails_list,
        "health_fails_top": health_fails_top,
        "health_age_sec": health_age_sec,
        "health_stale": health_stale,
        "health_stale_threshold_sec": cfg.status_health_stale_sec,
        "health_alert_severity": health_alert_severity,
        "health_alert_cooldown_sec": health_alert_cooldown_sec,
        "health_alert_in_alert": alert_in_alert if isinstance(alert_in_alert, bool) else "(unknown)",
        "health_alert_last_epoch": alert_last_epoch if alert_last_epoch is not None else "(unknown)",
        "health_alert_last_age_sec": alert_last_age_sec,
        "health_alert_last_severity": str(alert_last_severity),
        "health_alert_last_fingerprint": str(alert_last_fingerprint),
        "health_alert_sig_short": alert_sig_short,
        "health_alert_sig_hash8": alert_sig_hash8,
        "health_alert_cooldown_left_sec": alert_cooldown_left_sec,
        "health_effective_state": health_effective_state,
        "health_operator_hint": health_operator_hint,
        "health_operator_hint_code": health_operator_hint_code,
        "health_self_test_failed_checks": health_self_test_failed_checks,
        "health_self_test_repeated_fail_checks": health_self_test_repeated_checks,
        "health_self_test_fail_streak_threshold": health_self_test_fail_streak_threshold,
        "health_self_test_autorepair_enabled": health_self_test_autorepair_enabled,
        "health_self_test_autorepair_attempted": health_self_test_autorepair_attempted,
        "health_self_test_autorepair_result": health_self_test_autorepair_result,
        "health_self_test_autorepair_timeout_sec": health_self_test_autorepair_timeout_sec,
        "health_self_test_autorepair_script": health_self_test_autorepair_script,
        "health_self_test_quick_required": health_self_test_quick_required,
        "health_self_test_quick_state": health_self_test_quick_state,
        "health_self_test_quick_path": health_self_test_quick_path,
        "health_self_test_quick_max_age_sec": health_self_test_quick_max_age_sec,
        "health_self_test_quick_exists": health_self_test_quick_exists,
        "health_self_test_quick_ok": health_self_test_quick_ok,
        "health_self_test_quick_stale": health_self_test_quick_stale,
        "health_self_test_quick_age_sec": health_self_test_quick_age_sec,
        "health_self_test_quick_ts_utc": health_self_test_quick_ts_utc,
        "health_self_test_quick_failed_checks": health_self_test_quick_failed_checks,
        "health_self_test_quick_operator_hint": health_self_test_quick_operator_hint,
        "health_self_test_quick_operator_hint_code": health_self_test_quick_operator_hint_code,
        "health_reply_auth_probe_enabled": health_reply_auth_probe_enabled,
        "health_reply_auth_probe_ok": health_reply_auth_probe_ok,
        "health_reply_auth_probe_http": health_reply_auth_probe_http,
        "health_reply_auth_probe_state": health_reply_auth_probe_state,
        "event_lookback_min": cfg.status_event_lookback_min,
        "event_scan_max_bytes": cfg.status_event_scan_max_bytes,
        "queue_pressure_critical_threshold": cfg.status_queue_pressure_critical_threshold,
        "event_scan_complete": event_meta.get("complete"),
        "event_scan_bytes": event_meta.get("scan_bytes"),
        "event_file_size_bytes": event_meta.get("file_size_bytes"),
        "event_oldest_ts_utc": event_meta.get("oldest_ts_utc"),
        "event_scan_retry_used": event_scan_retry_used,
        "event_scan_retry_count": int(event_scan_retry_count),
        "event_reply_retry": events.get("reply_retry", 0),
        "event_reply_failed": events.get("reply_failed", 0),
        "event_relay_error": events.get("relay_error", 0),
        "event_image_intake_failed": events.get("image_intake_failed", 0),
        "event_file_intake_failed": events.get("file_intake_failed", 0),
        "event_plain_text_deduped": events.get("plain_text_deduped", 0),
        "alert_event_lookback_min": cfg.alert_event_lookback_min,
        "alert_reply_failed_threshold": cfg.alert_reply_failed_threshold,
        "alert_relay_error_threshold": cfg.alert_relay_error_threshold,
        "reply_queue_max": cfg.reply_api_queue_max,
        "reply_queue_drop_oldest": cfg.reply_api_queue_drop_oldest,
        "reply_metrics_state_path": str(cfg.reply_api_metrics_state_path),
        "reply_queue_depth": metrics.get("queue_depth"),
        "reply_queue_dropped_total": metrics.get("queue_dropped_total"),
        "reply_queue_full_rejected_total": metrics.get("queue_full_rejected_total"),
        "reply_sent_total": metrics.get("sent_total"),
        "reply_failed_total": metrics.get("failed_total"),
        "reply_latency_p95_ms": metrics.get("latency_p95_ms"),
        "reply_last_error": metrics.get("last_send_error") or "(none)",
        "reply_alert_transitions_total": metrics.get("alert_transitions_total"),
        "queue_pressure_state": queue_pressure_state,
        "queue_pressure_count": queue_pressure_count,
        "queue_pressure_threshold": queue_pressure_threshold,
        "queue_pressure_reasons": queue_pressure_reasons,
        "queue_pressure_operator_hint": queue_pressure_operator_hint,
        "queue_pressure_operator_hint_code": queue_pressure_operator_hint_code,
        "event_reply_queue_full": event_reply_queue_full,
        "event_reply_queue_drop_oldest": event_reply_queue_drop_oldest,
    }


def _format_runtime_status(cfg: Config, relay: TmuxRelay, piper: PiperTTS, reply_api: LocalReplyAPI) -> str:
    payload = _runtime_status_payload(cfg, relay, piper, reply_api)
    health_fails = payload.get("health_fails")
    if isinstance(health_fails, list):
        health_fails_text = ",".join(str(x) for x in health_fails[:5]) or "(none)"
    else:
        health_fails_text = "(none)"
    return "\n".join(
        [
            "YUUKI_STATUS",
            f"tmux_available={payload.get('tmux_available')}",
            f"tmux_target={payload.get('tmux_target')}",
            f"tmux_debounce_enabled={payload.get('tmux_debounce_enabled')}",
            f"tmux_debounce_sec={payload.get('tmux_debounce_sec')}",
            f"tmux_plain_text_dedupe_enabled={payload.get('tmux_plain_text_dedupe_enabled')}",
            f"tmux_plain_text_dedupe_sec={payload.get('tmux_plain_text_dedupe_sec')}",
            f"piper_enabled={payload.get('piper_enabled')}",
            f"piper_langs={','.join(payload.get('piper_langs') or []) if payload.get('piper_langs') else '(none)'}",
            f"archive_enabled={payload.get('archive_enabled')}",
            f"archive_repo={payload.get('archive_repo')}",
            f"json_log_enabled={payload.get('json_log_enabled')}",
            f"json_log_path={payload.get('json_log_path')}",
            f"health_status_path={payload.get('health_status_path')}",
            f"health_alert_state_path={payload.get('health_alert_state_path')}",
            f"watcher_enabled={payload.get('watcher_enabled')}",
            f"watcher_idle_sec={payload.get('watcher_idle_sec')}",
            f"watcher_cooldown_sec={payload.get('watcher_cooldown_sec')}",
            f"watcher_state_path={payload.get('watcher_state_path')}",
            f"watcher_task_state_path={payload.get('watcher_task_state_path')}",
            f"watcher_last_run_ts_utc={payload.get('watcher_last_run_ts_utc')}",
            f"watcher_last_action={payload.get('watcher_last_action')}",
            f"watcher_last_reason={payload.get('watcher_last_reason')}",
            f"watcher_last_idle_sec={payload.get('watcher_last_idle_sec')}",
            f"watcher_require_healthy={payload.get('watcher_require_healthy')}",
            f"watcher_health_state={payload.get('watcher_health_state')}",
            f"watcher_health_age_sec={payload.get('watcher_health_age_sec')}",
            f"watcher_health_stale={payload.get('watcher_health_stale')}",
            f"self_improve_state_path={payload.get('self_improve_state_path')}",
            f"self_improve_last_run_ts_utc={payload.get('self_improve_last_run_ts_utc')}",
            f"self_improve_last_action={payload.get('self_improve_last_action')}",
            f"self_improve_last_reason={payload.get('self_improve_last_reason')}",
            f"self_improve_require_healthy={payload.get('self_improve_require_healthy')}",
            f"self_improve_health_state={payload.get('self_improve_health_state')}",
            f"self_improve_health_age_sec={payload.get('self_improve_health_age_sec')}",
            f"self_improve_health_stale={payload.get('self_improve_health_stale')}",
            f"self_improve_target_pane={payload.get('self_improve_target_pane')}",
            f"self_improve_submit_key={payload.get('self_improve_submit_key')}",
            f"self_improve_task_running_detected={payload.get('self_improve_task_running_detected')}",
            f"self_improve_min_interval_sec={payload.get('self_improve_min_interval_sec')}",
            f"self_improve_last_sent_epoch={payload.get('self_improve_last_sent_epoch')}",
            f"self_test_status_path={payload.get('self_test_status_path')}",
            f"self_test_last_run_ts_utc={payload.get('self_test_last_run_ts_utc')}",
            f"self_test_ok={payload.get('self_test_ok')}",
            f"self_test_max_age_sec={payload.get('self_test_max_age_sec')}",
            f"self_test_age_sec={payload.get('self_test_age_sec')}",
            f"self_test_stale={payload.get('self_test_stale')}",
            f"self_test_checks_total={payload.get('self_test_checks_total')}",
            f"self_test_failed_names={','.join(payload.get('self_test_failed_names') or []) if payload.get('self_test_failed_names') else '(none)'}",
            f"self_test_failure_count={payload.get('self_test_failure_count')}",
            f"health_last_ts_utc={payload.get('health_last_ts_utc')}",
            f"health_ok={payload.get('health_ok')}",
            f"health_fail_count={payload.get('health_fail_count')}",
            f"health_fails={health_fails_text}",
            f"health_fails_top={','.join(payload.get('health_fails_top') or []) if payload.get('health_fails_top') else '(none)'}",
            f"health_age_sec={payload.get('health_age_sec')}",
            f"health_stale={payload.get('health_stale')}",
            f"health_stale_threshold_sec={payload.get('health_stale_threshold_sec')}",
            f"health_alert_severity={payload.get('health_alert_severity')}",
            f"health_alert_cooldown_sec={payload.get('health_alert_cooldown_sec')}",
            f"health_alert_in_alert={payload.get('health_alert_in_alert')}",
            f"health_alert_last_epoch={payload.get('health_alert_last_epoch')}",
            f"health_alert_last_age_sec={payload.get('health_alert_last_age_sec')}",
            f"health_alert_last_severity={payload.get('health_alert_last_severity')}",
            f"health_alert_last_fingerprint={payload.get('health_alert_last_fingerprint')}",
            f"health_alert_sig_short={payload.get('health_alert_sig_short')}",
            f"health_alert_sig_hash8={payload.get('health_alert_sig_hash8')}",
            f"health_alert_cooldown_left_sec={payload.get('health_alert_cooldown_left_sec')}",
            f"health_effective_state={payload.get('health_effective_state')}",
            f"health_operator_hint={payload.get('health_operator_hint')}",
            f"health_operator_hint_code={payload.get('health_operator_hint_code')}",
            f"health_self_test_failed_checks={','.join(payload.get('health_self_test_failed_checks') or []) if payload.get('health_self_test_failed_checks') else '(none)'}",
            f"health_self_test_repeated_fail_checks={','.join(payload.get('health_self_test_repeated_fail_checks') or []) if payload.get('health_self_test_repeated_fail_checks') else '(none)'}",
            f"health_self_test_fail_streak_threshold={payload.get('health_self_test_fail_streak_threshold')}",
            f"health_self_test_autorepair_enabled={payload.get('health_self_test_autorepair_enabled')}",
            f"health_self_test_autorepair_attempted={payload.get('health_self_test_autorepair_attempted')}",
            f"health_self_test_autorepair_result={payload.get('health_self_test_autorepair_result')}",
            f"health_self_test_autorepair_timeout_sec={payload.get('health_self_test_autorepair_timeout_sec')}",
            f"health_self_test_autorepair_script={payload.get('health_self_test_autorepair_script')}",
            f"health_self_test_quick_required={payload.get('health_self_test_quick_required')}",
            f"health_self_test_quick_state={payload.get('health_self_test_quick_state')}",
            f"health_self_test_quick_path={payload.get('health_self_test_quick_path')}",
            f"health_self_test_quick_max_age_sec={payload.get('health_self_test_quick_max_age_sec')}",
            f"health_self_test_quick_exists={payload.get('health_self_test_quick_exists')}",
            f"health_self_test_quick_ok={payload.get('health_self_test_quick_ok')}",
            f"health_self_test_quick_stale={payload.get('health_self_test_quick_stale')}",
            f"health_self_test_quick_age_sec={payload.get('health_self_test_quick_age_sec')}",
            f"health_self_test_quick_ts_utc={payload.get('health_self_test_quick_ts_utc')}",
            f"health_self_test_quick_failed_checks={','.join(payload.get('health_self_test_quick_failed_checks') or []) if payload.get('health_self_test_quick_failed_checks') else '(none)'}",
            f"health_self_test_quick_operator_hint={payload.get('health_self_test_quick_operator_hint')}",
            f"health_self_test_quick_operator_hint_code={payload.get('health_self_test_quick_operator_hint_code')}",
            f"health_reply_auth_probe_enabled={payload.get('health_reply_auth_probe_enabled')}",
            f"health_reply_auth_probe_ok={payload.get('health_reply_auth_probe_ok')}",
            f"health_reply_auth_probe_http={payload.get('health_reply_auth_probe_http')}",
            f"health_reply_auth_probe_state={payload.get('health_reply_auth_probe_state')}",
            f"event_lookback_min={payload.get('event_lookback_min')}",
            f"event_scan_max_bytes={payload.get('event_scan_max_bytes')}",
            f"queue_pressure_critical_threshold={payload.get('queue_pressure_critical_threshold')}",
            f"event_scan_complete={payload.get('event_scan_complete')}",
            f"event_scan_bytes={payload.get('event_scan_bytes')}",
            f"event_file_size_bytes={payload.get('event_file_size_bytes')}",
            f"event_oldest_ts_utc={payload.get('event_oldest_ts_utc')}",
            f"event_scan_retry_used={payload.get('event_scan_retry_used')}",
            f"event_scan_retry_count={payload.get('event_scan_retry_count')}",
            f"event_reply_retry={payload.get('event_reply_retry')}",
            f"event_reply_failed={payload.get('event_reply_failed')}",
            f"event_relay_error={payload.get('event_relay_error')}",
            f"event_image_intake_failed={payload.get('event_image_intake_failed')}",
            f"event_file_intake_failed={payload.get('event_file_intake_failed')}",
            f"event_plain_text_deduped={payload.get('event_plain_text_deduped')}",
            f"alert_event_lookback_min={payload.get('alert_event_lookback_min')}",
            f"alert_reply_failed_threshold={payload.get('alert_reply_failed_threshold')}",
            f"alert_relay_error_threshold={payload.get('alert_relay_error_threshold')}",
            f"reply_queue_max={payload.get('reply_queue_max')}",
            f"reply_queue_drop_oldest={payload.get('reply_queue_drop_oldest')}",
            f"reply_metrics_state_path={payload.get('reply_metrics_state_path')}",
            f"reply_queue_depth={payload.get('reply_queue_depth')}",
            f"reply_queue_dropped_total={payload.get('reply_queue_dropped_total')}",
            f"reply_queue_full_rejected_total={payload.get('reply_queue_full_rejected_total')}",
            f"reply_sent_total={payload.get('reply_sent_total')}",
            f"reply_failed_total={payload.get('reply_failed_total')}",
            f"reply_latency_p95_ms={payload.get('reply_latency_p95_ms')}",
            f"reply_last_error={payload.get('reply_last_error')}",
            f"reply_alert_transitions_total={payload.get('reply_alert_transitions_total')}",
            f"queue_pressure_state={payload.get('queue_pressure_state')}",
            f"queue_pressure_count={payload.get('queue_pressure_count')}",
            f"queue_pressure_threshold={payload.get('queue_pressure_threshold')}",
            f"queue_pressure_reasons={','.join(payload.get('queue_pressure_reasons') or []) if payload.get('queue_pressure_reasons') else '(none)'}",
            f"queue_pressure_operator_hint={payload.get('queue_pressure_operator_hint')}",
            f"queue_pressure_operator_hint_code={payload.get('queue_pressure_operator_hint_code')}",
            f"event_reply_queue_full={payload.get('event_reply_queue_full')}",
            f"event_reply_queue_drop_oldest={payload.get('event_reply_queue_drop_oldest')}",
        ]
    )


def _format_runtime_status_brief(cfg: Config, relay: TmuxRelay, piper: PiperTTS, reply_api: LocalReplyAPI) -> str:
    payload = _runtime_status_payload(cfg, relay, piper, reply_api)
    self_test_autorepair_part = ""
    if payload.get("health_self_test_autorepair_attempted") is True:
        self_test_autorepair_part = f"autorepair={payload.get('health_self_test_autorepair_result')} "
    return "\n".join(
        [
            "YUUKI_STATUS_BRIEF",
            f"tmux={payload.get('tmux_available')} target={payload.get('tmux_target')}",
            (
                "health "
                f"ok={payload.get('health_ok')} "
                f"state={payload.get('health_effective_state')} "
                f"severity={payload.get('health_alert_severity')} "
                f"fails={payload.get('health_fail_count')} "
                f"top={','.join(payload.get('health_fails_top') or []) if payload.get('health_fails_top') else '(none)'} "
                f"cd_left_s={payload.get('health_alert_cooldown_left_sec')} "
                f"sig_short={payload.get('health_alert_sig_short')} "
                f"sig8={payload.get('health_alert_sig_hash8')} "
                f"alert_transitions={payload.get('reply_alert_transitions_total')} "
                f"last_age_s={payload.get('health_alert_last_age_sec')} "
                f"stale={payload.get('health_stale')} "
                f"age_s={payload.get('health_age_sec')} "
                f"quick_lane={payload.get('health_self_test_quick_state')} "
                f"reply_auth={payload.get('health_reply_auth_probe_state')}:{payload.get('health_reply_auth_probe_http')} "
                f"hint_code={payload.get('health_operator_hint_code')} "
                f"last={payload.get('health_last_ts_utc')}"
            ),
            (
                "reply "
                f"q={payload.get('reply_queue_depth')}/{payload.get('reply_queue_max')} "
                f"sent={payload.get('reply_sent_total')} failed={payload.get('reply_failed_total')} "
                f"drop={payload.get('reply_queue_dropped_total')} rej={payload.get('reply_queue_full_rejected_total')} "
                f"p95ms={payload.get('reply_latency_p95_ms')} "
                f"pressure={payload.get('queue_pressure_state')} "
                f"pc={payload.get('queue_pressure_count')}/{payload.get('queue_pressure_threshold')}"
            ),
            (
                "events "
                f"retry={payload.get('event_reply_retry')} fail={payload.get('event_reply_failed')} "
                f"relay_err={payload.get('event_relay_error')} deduped={payload.get('event_plain_text_deduped')} "
                f"scan_complete={payload.get('event_scan_complete')} "
                f"scan_retry={payload.get('event_scan_retry_used')}:{payload.get('event_scan_retry_count')}"
            ),
            (
                "watcher "
                f"enabled={payload.get('watcher_enabled')} action={payload.get('watcher_last_action')} "
                f"reason={payload.get('watcher_last_reason')} "
                f"health={payload.get('watcher_health_state')} "
                f"stale={payload.get('watcher_health_stale')}"
            ),
            (
                "self_improve "
                f"action={payload.get('self_improve_last_action')} reason={payload.get('self_improve_last_reason')} "
                f"health={payload.get('self_improve_health_state')} "
                f"submit={payload.get('self_improve_submit_key')}"
            ),
            (
                "self_test "
                f"ok={payload.get('self_test_ok')} "
                f"checks={payload.get('self_test_checks_total')} "
                f"fails={payload.get('self_test_failure_count')} "
                f"stale={payload.get('self_test_stale')} "
                f"age_s={payload.get('self_test_age_sec')} "
                f"failed={','.join(payload.get('self_test_failed_names') or []) if payload.get('self_test_failed_names') else '(none)'} "
                f"repeat_hc={','.join(payload.get('health_self_test_repeated_fail_checks') or []) if payload.get('health_self_test_repeated_fail_checks') else '(none)'} "
                f"repeat_thr={payload.get('health_self_test_fail_streak_threshold')} "
                f"{self_test_autorepair_part}"
                f"last={payload.get('self_test_last_run_ts_utc')}"
            ),
            (
                "self_test_quick "
                f"req={payload.get('health_self_test_quick_required')} "
                f"state={payload.get('health_self_test_quick_state')} "
                f"ok={payload.get('health_self_test_quick_ok')} "
                f"stale={payload.get('health_self_test_quick_stale')} "
                f"age_s={payload.get('health_self_test_quick_age_sec')} "
                f"failed={','.join(payload.get('health_self_test_quick_failed_checks') or []) if payload.get('health_self_test_quick_failed_checks') else '(none)'} "
                f"last={payload.get('health_self_test_quick_ts_utc')}"
            ),
            f"piper={payload.get('piper_enabled')} langs={','.join(payload.get('piper_langs') or []) if payload.get('piper_langs') else '(none)'}",
        ]
    )


def _runtime_status_code_view(payload: dict[str, object], *, with_schema: bool = False) -> dict[str, object]:
    code = {
        "state": payload.get("health_effective_state"),
        "hint_code": payload.get("health_operator_hint_code"),
        "severity": payload.get("health_alert_severity"),
        "fails": payload.get("health_fail_count"),
        "quick_lane": payload.get("health_self_test_quick_state"),
        "reply_auth": payload.get("health_reply_auth_probe_state"),
        "sig8": payload.get("health_alert_sig_hash8"),
        "in_alert": payload.get("health_alert_in_alert"),
        "stale": payload.get("health_stale"),
        "ts": payload.get("health_last_ts_utc"),
    }
    if with_schema:
        code["schema"] = STATUS_CODE_SCHEMA
        code["version"] = STATUS_CODE_VERSION
    return code


def _format_runtime_status_code(cfg: Config, relay: TmuxRelay, piper: PiperTTS, reply_api: LocalReplyAPI) -> str:
    code = _runtime_status_code_view(_runtime_status_payload(cfg, relay, piper, reply_api))
    return (
        "YUUKI_STATUS_CODE "
        f"state={code.get('state')} "
        f"hint_code={code.get('hint_code')} "
        f"severity={code.get('severity')} "
        f"fails={code.get('fails')} "
        f"quick_lane={code.get('quick_lane')} "
        f"reply_auth={code.get('reply_auth')} "
        f"sig8={code.get('sig8')} "
        f"in_alert={code.get('in_alert')} "
        f"stale={code.get('stale')} "
        f"ts={code.get('ts')}"
    )


def _format_runtime_status_code_json(cfg: Config, relay: TmuxRelay, piper: PiperTTS, reply_api: LocalReplyAPI) -> str:
    code = _runtime_status_code_view(_runtime_status_payload(cfg, relay, piper, reply_api), with_schema=True)
    return json.dumps(code, ensure_ascii=False, sort_keys=True)


def run() -> None:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, (os.environ.get("TELEGRAM_LOG_LEVEL") or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if cfg.json_log_enabled:
        try:
            cfg.json_log_path.parent.mkdir(parents=True, exist_ok=True)
            json_handler = logging.FileHandler(cfg.json_log_path, encoding="utf-8")
            json_handler.setFormatter(JsonLineFormatter())
            logging.getLogger().addHandler(json_handler)
            LOG.info("json logging enabled path=%s", cfg.json_log_path)
        except Exception as exc:
            LOG.warning("json logging disabled due to setup error: %s", exc)

    tg = TelegramClient(cfg.telegram_token, timeout_sec=cfg.telegram_poll_timeout_sec + 20, force_ipv4=cfg.telegram_force_ipv4)
    me = tg.get_me()
    LOG.info("Connected as @%s", me.get("username") or me.get("id"))
    LOG.info("telegram transport force_ipv4=%s", cfg.telegram_force_ipv4)
    LOG.info(
        "image intake enabled=%s dir=%s",
        cfg.image_intake_enabled,
        cfg.image_save_dir,
    )
    LOG.info(
        "file intake enabled=%s dir=%s",
        cfg.file_intake_enabled,
        cfg.file_save_dir,
    )

    piper = PiperTTS(cfg)
    relay = TmuxRelay(cfg)
    debouncer = TextDebouncer(cfg.tmux_debounce_enabled, cfg.tmux_debounce_sec)
    deduper = TextDeduper(cfg.tmux_plain_text_dedupe_enabled, cfg.tmux_plain_text_dedupe_sec)
    last_chat_state = LastChatState()

    reply_api = LocalReplyAPI(
        cfg,
        last_chat_state,
        status_code_provider=lambda with_schema=False: _runtime_status_code_view(
            _runtime_status_payload(cfg, relay, piper, reply_api),
            with_schema=bool(with_schema),
        ),
    )
    reply_api.start()

    offset = 0
    if cfg.skip_backlog_on_start:
        updates = tg.get_updates_nowait(offset=0)
        if updates:
            offset = max(int(u.get("update_id", 0)) for u in updates) + 1
            LOG.info("Skipping backlog on start. next_offset=%s dropped=%s", offset, len(updates))

    while True:
        try:
            poll_timeout = 1 if debouncer.has_pending() else cfg.telegram_poll_timeout_sec
            updates = tg.get_updates(offset=offset, timeout_sec=poll_timeout)
            for upd in updates:
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                chat_id_s = str(chat_id)
                if cfg.telegram_allowed_chat_ids and chat_id_s not in cfg.telegram_allowed_chat_ids:
                    continue

                text = msg.get("text") or ""
                if not text and not msg.get("photo") and not msg.get("document"):
                    continue

                last_chat_state.set(int(chat_id))

                if cfg.image_intake_enabled:
                    picked = _pick_image_file(msg)
                    if picked is not None:
                        file_id, suffix = picked
                        message_id = int(msg.get("message_id") or 0)
                        caption = (msg.get("caption") or "").strip()
                        now = datetime.now(timezone.utc)
                        out_dir = cfg.image_save_dir / now.strftime("%Y%m%d")
                        out_dir.mkdir(parents=True, exist_ok=True)
                        filename = f"tg_{int(chat_id)}_{message_id}_{now.strftime('%H%M%S')}{suffix}"
                        out_path = out_dir / filename
                        relayed = False
                        try:
                            tg_file = tg.get_file(file_id)
                            file_path = (tg_file.get("file_path") or "").strip()
                            tg.download_file_to(file_path, out_path)
                            relay_error = ""
                            if relay.is_available():
                                relay_payload = _build_image_saved_text(
                                    out_path,
                                    caption,
                                    int(chat_id),
                                    message_id,
                                    relayed=True,
                                )
                                try:
                                    relay.forward(relay_payload)
                                    relayed = True
                                except Exception as relay_exc:
                                    relay_error = str(relay_exc)
                                    LOG.warning(
                                        "image relay failed chat_id=%s msg_id=%s: %s",
                                        chat_id,
                                        message_id,
                                        relay_exc,
                                    )
                                    log_event(
                                        "relay_error",
                                        relay_type="image",
                                        chat_id=int(chat_id),
                                        message_id=int(message_id),
                                        error=str(relay_exc)[:180],
                                    )
                            image_text = _build_image_saved_text(
                                out_path,
                                caption,
                                int(chat_id),
                                message_id,
                                relayed=relayed,
                            )
                            if relay_error:
                                image_text = f"{image_text}\nrelay_error={relay_error[:180]}"
                            LOG.info(
                                "saved image chat_id=%s msg_id=%s path=%s relayed=%s",
                                chat_id,
                                message_id,
                                out_path,
                                relayed,
                            )
                            log_event(
                                "image_saved",
                                chat_id=int(chat_id),
                                message_id=int(message_id),
                                path=str(out_path),
                                relayed=bool(relayed),
                            )
                            tg.send(int(chat_id), image_text)
                        except Exception as exc:
                            LOG.exception("image intake failed chat_id=%s msg_id=%s: %s", chat_id, message_id, exc)
                            log_event(
                                "image_intake_failed",
                                chat_id=int(chat_id),
                                message_id=int(message_id),
                                error=str(exc)[:200],
                            )
                            tg.send(int(chat_id), f"image intake error: {exc}")
                        continue

                if cfg.file_intake_enabled:
                    picked_doc = _pick_document_file(msg)
                    if picked_doc is not None:
                        file_id, file_name, mime_type, file_size = picked_doc
                        message_id = int(msg.get("message_id") or 0)
                        caption = (msg.get("caption") or "").strip()
                        now = datetime.now(timezone.utc)
                        out_dir = cfg.file_save_dir / now.strftime("%Y%m%d")
                        out_dir.mkdir(parents=True, exist_ok=True)
                        filename = f"tg_{int(chat_id)}_{message_id}_{now.strftime('%H%M%S')}_{file_name}"
                        out_path = out_dir / _safe_file_name(filename, fallback=f"tg_{int(chat_id)}_{message_id}")
                        relayed = False
                        try:
                            tg_file = tg.get_file(file_id)
                            file_path = (tg_file.get("file_path") or "").strip()
                            tg.download_file_to(file_path, out_path)
                            relay_error = ""
                            if relay.is_available():
                                relay_payload = _build_file_saved_text(
                                    out_path,
                                    file_name=file_name,
                                    mime_type=mime_type,
                                    size_bytes=file_size,
                                    caption=caption,
                                    chat_id=int(chat_id),
                                    message_id=message_id,
                                    relayed=True,
                                )
                                try:
                                    relay.forward(relay_payload)
                                    relayed = True
                                except Exception as relay_exc:
                                    relay_error = str(relay_exc)
                                    LOG.warning(
                                        "file relay failed chat_id=%s msg_id=%s: %s",
                                        chat_id,
                                        message_id,
                                        relay_exc,
                                    )
                                    log_event(
                                        "relay_error",
                                        relay_type="file",
                                        chat_id=int(chat_id),
                                        message_id=int(message_id),
                                        error=str(relay_exc)[:180],
                                    )
                            file_text = _build_file_saved_text(
                                out_path,
                                file_name=file_name,
                                mime_type=mime_type,
                                size_bytes=file_size,
                                caption=caption,
                                chat_id=int(chat_id),
                                message_id=message_id,
                                relayed=relayed,
                            )
                            if relay_error:
                                file_text = f"{file_text}\nrelay_error={relay_error[:180]}"
                            archive_ok, archive_note = _archive_file_to_ai_repo(
                                cfg,
                                source_path=out_path,
                                file_name=file_name,
                                mime_type=mime_type,
                                size_bytes=file_size,
                                caption=caption,
                                chat_id=int(chat_id),
                                message_id=message_id,
                            )
                            file_text = (
                                f"{file_text}\n"
                                f"archived_to_ai_github={str(archive_ok).lower()}\n"
                                f"archive_note={archive_note.replace(chr(10), ' ')[:240]}"
                            )
                            LOG.info(
                                "saved file chat_id=%s msg_id=%s path=%s relayed=%s",
                                chat_id,
                                message_id,
                                out_path,
                                relayed,
                            )
                            log_event(
                                "file_saved",
                                chat_id=int(chat_id),
                                message_id=int(message_id),
                                path=str(out_path),
                                relayed=bool(relayed),
                                archived=bool(archive_ok),
                                archive_note=str(archive_note)[:200],
                            )
                            tg.send(int(chat_id), file_text)
                        except Exception as exc:
                            LOG.exception("file intake failed chat_id=%s msg_id=%s: %s", chat_id, message_id, exc)
                            log_event(
                                "file_intake_failed",
                                chat_id=int(chat_id),
                                message_id=int(message_id),
                                error=str(exc)[:200],
                            )
                            tg.send(int(chat_id), f"file intake error: {exc}")
                        continue

                if not text:
                    continue

                cmd, arg = _cmd_and_arg(text)
                LOG.info("command chat_id=%s cmd=%s text_len=%s", chat_id, cmd or "(plain)", len(text))
                log_event(
                    "command_received",
                    chat_id=int(chat_id),
                    cmd=(cmd or "plain"),
                    text_len=len(text),
                )

                if cmd == "/start":
                    tg.send(int(chat_id), build_start_text())
                    continue
                if cmd == "/help":
                    tg.send(int(chat_id), build_help_text())
                    continue
                if cmd == "/id":
                    tg.send(int(chat_id), f"chat_id={chat_id}")
                    continue
                if cmd == "/ping":
                    tg.send(int(chat_id), "pong")
                    continue
                if cmd == "/tmuxstatus":
                    tg.send(int(chat_id), _format_tmux_status(relay))
                    continue
                if cmd == "/status":
                    tg.send(int(chat_id), _format_runtime_status(cfg, relay, piper, reply_api))
                    continue
                if cmd == "/statusbrief":
                    tg.send(int(chat_id), _format_runtime_status_brief(cfg, relay, piper, reply_api))
                    continue
                if cmd == "/statuscode":
                    tg.send(int(chat_id), _format_runtime_status_code(cfg, relay, piper, reply_api))
                    continue
                if cmd == "/statuscodejson":
                    tg.send(int(chat_id), _format_runtime_status_code_json(cfg, relay, piper, reply_api))
                    continue
                if cmd == "/statusjson":
                    payload = _runtime_status_payload(cfg, relay, piper, reply_api)
                    tg.send(int(chat_id), json.dumps(payload, ensure_ascii=False, indent=2))
                    continue
                if cmd == "/archivecfg":
                    tg.send(int(chat_id), _format_archive_status(cfg))
                    continue
                if cmd == "/totmux":
                    if not arg:
                        tg.send(int(chat_id), "usage: /totmux <text>")
                        continue
                    try:
                        relay.forward(arg)
                    except Exception as exc:
                        tg.send(int(chat_id), f"tmux relay error: {exc}")
                    else:
                        if relay.ack:
                            tg.send(int(chat_id), "sent to tmux")
                    continue
                if cmd == "/piperlangs":
                    langs = piper.available_langs()
                    if not langs:
                        tg.send(int(chat_id), "No Piper languages configured.")
                    else:
                        tg.send(int(chat_id), "Piper languages: " + ", ".join(langs))
                    continue
                if cmd == "/piper":
                    if not arg:
                        tg.send(int(chat_id), "usage: /piper <lang> <text>")
                        continue
                    parts = arg.split(maxsplit=1)
                    candidate = _norm_lang(parts[0])
                    if candidate in piper.models and len(parts) > 1:
                        lang = candidate
                        utterance = parts[1]
                    else:
                        lang = piper.default_lang
                        utterance = arg
                    try:
                        out = piper.synthesize(lang, utterance)
                        caption = f"lang={lang} chars={len(_clean(utterance))}"
                        tg.send_voice(int(chat_id), out, caption=caption)
                    except Exception as exc:
                        tg.send(int(chat_id), f"piper error: {exc}")
                    finally:
                        try:
                            out.unlink(missing_ok=True)  # type: ignore[name-defined]
                        except Exception:
                            pass
                    continue
                if cmd.startswith("/"):
                    tg.send(int(chat_id), "Unknown command. Use /help")
                    continue

                allow_plain, repeat_count = deduper.allow(int(chat_id), text)
                if not allow_plain:
                    log_event(
                        "plain_text_deduped",
                        chat_id=int(chat_id),
                        text_len=len(text),
                        repeat=repeat_count,
                        dedupe_sec=int(cfg.tmux_plain_text_dedupe_sec),
                    )
                    continue

                if relay.plain_text_to_tmux and relay.is_available():
                    if debouncer.enabled:
                        is_new_burst = debouncer.add(int(chat_id), text)
                        log_event("plain_text_buffered", chat_id=int(chat_id), text_len=len(text))
                        if cfg.plain_text_quick_reply and is_new_burst:
                            tg.send(int(chat_id), cfg.plain_text_quick_reply)
                    else:
                        try:
                            relay.forward(text)
                            log_event("plain_text_relayed", chat_id=int(chat_id), text_len=len(text), debounced=False)
                            if cfg.plain_text_quick_reply:
                                tg.send(int(chat_id), cfg.plain_text_quick_reply)
                        except Exception as exc:
                            log_event("relay_error", relay_type="plain", chat_id=int(chat_id), error=str(exc)[:180])
                            tg.send(int(chat_id), f"tmux relay error: {exc}")
                elif cfg.plain_text_quick_reply:
                    tg.send(int(chat_id), cfg.plain_text_quick_reply)

            if debouncer.enabled:
                for due_chat_id, merged_text in debouncer.pop_due():
                    if relay.plain_text_to_tmux and relay.is_available():
                        try:
                            relay.forward(merged_text)
                            log_event(
                                "plain_text_relayed",
                                chat_id=int(due_chat_id),
                                text_len=len(merged_text),
                                debounced=True,
                            )
                        except Exception as exc:
                            log_event("relay_error", relay_type="plain", chat_id=int(due_chat_id), error=str(exc)[:180])
                            tg.send(int(due_chat_id), f"tmux relay error: {exc}")
                    else:
                        log_event("relay_unavailable", relay_type="plain", chat_id=int(due_chat_id))
                        tg.send(int(due_chat_id), "tmux relay unavailable")

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            LOG.exception("Polling loop error: %s", exc)
            time.sleep(cfg.telegram_retry_sleep_sec)


if __name__ == "__main__":
    run()

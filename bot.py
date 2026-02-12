#!/usr/bin/env python3
"""Telegram bot bridge for Paper Parser API (long polling)."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

import httpx

LOG = logging.getLogger("kitan-telegram")

START_TEXT = (
    "ScholarStream Telegram bot is online.\n"
    "Send plain text and I will reply in Telegram.\n\n"
    "Commands:\n"
    "/id\n"
    "/ask <text>\n"
    "/aistatus\n"
    "/ping\n"
    "/search <query>\n"
    "/toai <text>\n"
    "/totmux <text>\n"
    "/tmuxstatus\n"
    "/paper <id>\n"
    "/stats\n"
    "/piperlangs\n"
    "/piper <lang> <text>\n"
    "/help"
)

HELP_TEXT = (
    "Usage:\n"
    "/id - show your chat id\n"
    "/ask <text> - ask Codex (alias of /toai)\n"
    "/aistatus - show Codex assistant status\n"
    "/ping - instant bot health check\n"
    "/search <query> - search papers (explicit)\n"
    "/toai <text> - ask Codex\n"
    "/totmux <text> - send text to Codex tmux terminal\n"
    "/tmuxstatus - show tmux relay status\n"
    "/paper <id> - show paper details\n"
    "/stats - dataset stats\n"
    "/piperlangs - list Piper TTS languages\n"
    "/piper <lang> <text> - generate voice with Piper\n"
    "/help - this help\n\n"
    "Plain text messages go to Codex and return short Telegram replies."
)


@dataclass
class Config:
    telegram_token: str
    telegram_allowed_chat_ids: set[str]
    telegram_poll_timeout_sec: int
    telegram_retry_sleep_sec: int
    telegram_force_ipv4: bool
    search_limit: int

    api_base_url: str
    api_key: str
    semantic_default: bool

    piper_enabled: bool
    piper_bin: str
    piper_timeout_sec: int
    piper_max_chars: int
    piper_default_lang: str
    piper_models: dict[str, Path]

    codex_enabled: bool
    codex_bin: str
    codex_model: str
    codex_mode: str
    codex_thread_id: str
    codex_resume_fallback_exec: bool
    codex_timeout_sec: int
    codex_input_max_chars: int
    codex_reply_max_chars: int
    codex_workdir: str
    codex_system_prompt: str

    tmux_relay_enabled: bool
    tmux_target_pane: str
    tmux_relay_max_chars: int
    tmux_relay_prefix: str
    tmux_relay_ack: bool
    tmux_plain_text_to_tmux: bool
    plain_text_reply_mode: str
    plain_text_quick_reply: str
    codex_session_reply_timeout_sec: int
    skip_backlog_on_start: bool
    reply_api_enabled: bool
    reply_api_host: str
    reply_api_port: int
    reply_api_token: str
    reply_api_dedupe_sec: int


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
    codex_mode = (os.environ.get("TELEGRAM_CODEX_MODE") or "exec").strip().lower()
    if codex_mode not in {"exec", "resume"}:
        codex_mode = "exec"
    codex_system_prompt = (
        os.environ.get("TELEGRAM_CODEX_SYSTEM_PROMPT")
        or (
            "You are Codex assistant in Telegram. Keep replies short, clean, and practical. "
            "Use plain text only. Prefer 3-6 concise lines or a short numbered list when useful. "
            "No long preambles, no markdown tables."
        )
    ).strip()
    plain_text_reply_mode = (os.environ.get("TELEGRAM_PLAIN_TEXT_REPLY_MODE") or "quick").strip().lower()
    if plain_text_reply_mode not in {"silent", "quick", "codex", "quick_codex", "session", "quick_session"}:
        plain_text_reply_mode = "quick"
    plain_text_quick_reply = (
        os.environ.get("TELEGRAM_PLAIN_TEXT_QUICK_REPLY")
        or "Received."
    ).strip() or "Received."

    return Config(
        telegram_token=token,
        telegram_allowed_chat_ids=allowed_ids,
        telegram_poll_timeout_sec=max(10, _int_env("TELEGRAM_POLL_TIMEOUT_SEC", 50)),
        telegram_retry_sleep_sec=max(1, _int_env("TELEGRAM_RETRY_SLEEP_SEC", 3)),
        telegram_force_ipv4=_bool_env("TELEGRAM_FORCE_IPV4", True),
        search_limit=max(1, _int_env("TELEGRAM_SEARCH_LIMIT", 5)),
        api_base_url=(os.environ.get("PAPER_PARSER_API_BASE_URL") or "http://127.0.0.1:8080").rstrip("/"),
        api_key=(os.environ.get("PAPER_PARSER_API_KEY") or "").strip(),
        semantic_default=_bool_env("TELEGRAM_SEMANTIC_DEFAULT", False),
        piper_enabled=_bool_env("TELEGRAM_PIPER_ENABLED", True),
        piper_bin=(os.environ.get("TELEGRAM_PIPER_BIN") or "piper").strip(),
        piper_timeout_sec=max(5, _int_env("TELEGRAM_PIPER_TIMEOUT_SEC", 120)),
        piper_max_chars=max(20, _int_env("TELEGRAM_PIPER_MAX_CHARS", 600)),
        piper_default_lang=default_lang,
        piper_models=piper_models,
        codex_enabled=_bool_env("TELEGRAM_CODEX_ENABLED", True),
        codex_bin=(os.environ.get("TELEGRAM_CODEX_BIN") or "codex").strip(),
        codex_model=(os.environ.get("TELEGRAM_CODEX_MODEL") or "").strip(),
        codex_mode=codex_mode,
        codex_thread_id=(os.environ.get("TELEGRAM_CODEX_THREAD_ID") or "").strip(),
        codex_resume_fallback_exec=_bool_env("TELEGRAM_CODEX_RESUME_FALLBACK_EXEC", False),
        codex_timeout_sec=max(10, _int_env("TELEGRAM_CODEX_TIMEOUT_SEC", 120)),
        codex_input_max_chars=max(50, _int_env("TELEGRAM_CODEX_INPUT_MAX_CHARS", 2500)),
        codex_reply_max_chars=max(200, _int_env("TELEGRAM_CODEX_REPLY_MAX_CHARS", 1400)),
        codex_workdir=(os.environ.get("TELEGRAM_CODEX_WORKDIR") or str(Path(__file__).resolve().parent)).strip(),
        codex_system_prompt=codex_system_prompt,
        tmux_relay_enabled=_bool_env("TELEGRAM_TMUX_RELAY_ENABLED", False),
        tmux_target_pane=(os.environ.get("TELEGRAM_TMUX_TARGET_PANE") or "").strip(),
        tmux_relay_max_chars=max(20, _int_env("TELEGRAM_TMUX_RELAY_MAX_CHARS", 2000)),
        tmux_relay_prefix=(os.environ.get("TELEGRAM_TMUX_RELAY_PREFIX") or "").strip(),
        tmux_relay_ack=_bool_env("TELEGRAM_TMUX_RELAY_ACK", True),
        tmux_plain_text_to_tmux=_bool_env("TELEGRAM_TMUX_PLAIN_TEXT_TO_TMUX", True),
        plain_text_reply_mode=plain_text_reply_mode,
        plain_text_quick_reply=plain_text_quick_reply,
        codex_session_reply_timeout_sec=max(15, _int_env("TELEGRAM_CODEX_SESSION_REPLY_TIMEOUT_SEC", 180)),
        skip_backlog_on_start=_bool_env("TELEGRAM_SKIP_BACKLOG_ON_START", False),
        reply_api_enabled=_bool_env("TELEGRAM_LOCAL_REPLY_API_ENABLED", True),
        reply_api_host=(os.environ.get("TELEGRAM_LOCAL_REPLY_API_HOST") or "127.0.0.1").strip(),
        reply_api_port=max(1, _int_env("TELEGRAM_LOCAL_REPLY_API_PORT", 8788)),
        reply_api_token=(os.environ.get("TELEGRAM_LOCAL_REPLY_API_TOKEN") or "").strip(),
        reply_api_dedupe_sec=max(0, _int_env("TELEGRAM_LOCAL_REPLY_API_DEDUPE_SEC", 10)),
    )


def _clean(value: str | None) -> str:
    text = (value or "").strip()
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_words(text: str, n: int) -> str:
    words = [w for w in text.split() if w]
    if len(words) <= n:
        return text
    return " ".join(words[:n]) + "..."


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


class BackendClient:
    def __init__(self, cfg: Config):
        self.base = cfg.api_base_url
        headers = {}
        if cfg.api_key:
            headers["x-api-key"] = cfg.api_key
        self.client = httpx.Client(timeout=35.0, headers=headers)

    def stats(self) -> dict:
        resp = self.client.get(f"{self.base}/stats")
        resp.raise_for_status()
        return resp.json()

    def search(self, query: str, limit: int, semantic: bool) -> list[dict]:
        params = {
            "q": query,
            "limit": str(limit),
            "semantic": "true" if semantic else "false",
            "fill_missing_abstracts": "true",
        }
        resp = self.client.get(f"{self.base}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def paper(self, paper_id: int) -> dict | None:
        resp = self.client.get(
            f"{self.base}/paper/{paper_id}",
            params={"fill_missing_abstracts": "true"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None


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


class TelegramClient:
    def __init__(self, token: str, timeout_sec: float = 70.0, force_ipv4: bool = False):
        transport = httpx.HTTPTransport(local_address="0.0.0.0") if force_ipv4 else None
        self.client = httpx.Client(timeout=timeout_sec, transport=transport)
        self.base = f"https://api.telegram.org/bot{token}"

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

    def send(self, chat_id: int, text: str) -> None:
        for chunk in _chunk_text(text):
            self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
            )

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


class LocalReplyAPI:
    def __init__(self, cfg: Config, state: LastChatState):
        self.enabled = cfg.reply_api_enabled
        self.host = cfg.reply_api_host
        self.port = cfg.reply_api_port
        self.token = cfg.reply_api_token
        self.dedupe_sec = cfg.reply_api_dedupe_sec
        self.state = state
        self.tg = TelegramClient(cfg.telegram_token, timeout_sec=20.0, force_ipv4=cfg.telegram_force_ipv4)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._sender_thread: threading.Thread | None = None
        self._send_q: queue.Queue[tuple[int, str]] = queue.Queue()
        self._dedupe_lock = threading.Lock()
        self._recent_sends: dict[tuple[int, str], float] = {}

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
                # Keep bot logs clean; important events are logged by the caller.
                return

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except BrokenPipeError:
                    return
                except ConnectionResetError:
                    return

            def _headers_lower(self) -> dict[str, str]:
                return {k.lower(): v for k, v in self.headers.items()}

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/health":
                    self._json(200, {"ok": True})
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
                    chat_id, _updated_at = outer.state.snapshot()
                if chat_id is None:
                    self._json(409, {"ok": False, "error": "no_recent_chat"})
                    return

                if outer._is_duplicate(chat_id, text):
                    LOG.info("reply-api duplicate suppressed chat_id=%s chars=%s", chat_id, len(text))
                    self._json(200, {"ok": True, "chat_id": int(chat_id), "chars": len(text), "duplicate": True})
                    return

                outer._send_q.put((int(chat_id), text))
                self._json(200, {"ok": True, "chat_id": int(chat_id), "chars": len(text), "queued": True})

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
        LOG.info(
            "local reply api enabled url=http://%s:%s/reply token=%s",
            self.host,
            self.port,
            "set" if self.token else "unset",
        )

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

    def _send_worker(self) -> None:
        while True:
            chat_id, text = self._send_q.get()
            try:
                t0 = time.time()
                LOG.info("reply-api send chat_id=%s chars=%s", chat_id, len(text))
                self.tg.send(int(chat_id), text)
                LOG.info("reply-api sent chat_id=%s chars=%s latency_ms=%s", chat_id, len(text), int((time.time() - t0) * 1000))
            except Exception as exc:
                LOG.exception("reply-api send failed chat_id=%s: %s", chat_id, exc)
            finally:
                self._send_q.task_done()


class CodexAssistant:
    def __init__(self, cfg: Config):
        self.enabled = cfg.codex_enabled
        self.bin = cfg.codex_bin
        self.model = cfg.codex_model
        self.mode = cfg.codex_mode
        self.thread_id = cfg.codex_thread_id
        self.resume_fallback_exec = cfg.codex_resume_fallback_exec
        self.timeout_sec = cfg.codex_timeout_sec
        self.input_max_chars = cfg.codex_input_max_chars
        self.reply_max_chars = cfg.codex_reply_max_chars
        self.workdir = cfg.codex_workdir
        self.system_prompt = cfg.codex_system_prompt

    def _resolve_bin(self) -> str | None:
        if Path(self.bin).exists():
            return str(Path(self.bin))
        return shutil.which(self.bin)

    def is_available(self) -> bool:
        return self.enabled and bool(self._resolve_bin())

    def _clean_input(self, text: str) -> str:
        cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
        if len(cleaned) > self.input_max_chars:
            cleaned = cleaned[: self.input_max_chars]
        return cleaned

    def _clean_reply(self, text: str) -> str:
        value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        value = re.sub(r"\n{3,}", "\n\n", value)
        if len(value) > self.reply_max_chars:
            value = value[: self.reply_max_chars].rstrip() + "..."
        return value or "No answer."

    def _ask_exec(self, codex_bin: str, prompt: str) -> str:
        fd, tmp_out = tempfile.mkstemp(prefix="codex-telegram-", suffix=".txt")
        os.close(fd)
        out_path = Path(tmp_out)

        cmd = [
            codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-C",
            self.workdir,
            "-o",
            str(out_path),
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.append(prompt)

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(f"Codex timeout after {self.timeout_sec}s") from exc

        reply_text = ""
        try:
            reply_text = out_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            reply_text = ""
        finally:
            out_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            err_text = proc.stderr.decode("utf-8", errors="ignore").strip()
            if not err_text:
                err_text = proc.stdout.decode("utf-8", errors="ignore").strip()
            raise RuntimeError((err_text or "Codex request failed")[-300:])

        if not reply_text.strip():
            fallback = proc.stdout.decode("utf-8", errors="ignore").strip()
            if fallback:
                reply_text = fallback.splitlines()[-1].strip()
        return reply_text

    def _ask_resume(self, codex_bin: str, prompt: str) -> str:
        if not self.thread_id:
            raise RuntimeError("TELEGRAM_CODEX_THREAD_ID is required for resume mode")

        cmd = [
            codex_bin,
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--json",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        cmd.extend([self.thread_id, prompt])

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_sec,
                check=False,
                cwd=self.workdir,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex timeout after {self.timeout_sec}s") from exc

        stdout = proc.stdout.decode("utf-8", errors="ignore")
        stderr = proc.stderr.decode("utf-8", errors="ignore")

        if proc.returncode != 0:
            err_text = (stderr or stdout or "Codex resume request failed").strip()
            raise RuntimeError(err_text[-300:])

        last_message = ""
        for line in stdout.splitlines():
            raw = line.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "item.completed":
                continue
            item = obj.get("item") or {}
            if item.get("type") == "agent_message":
                text = (item.get("text") or "").strip()
                if text:
                    last_message = text

        if last_message:
            return last_message

        for line in reversed(stdout.splitlines()):
            if line.strip():
                return line.strip()
        raise RuntimeError("Codex returned no assistant message")

    def ask(self, user_text: str) -> str:
        if not self.enabled:
            raise RuntimeError("Codex assistant is disabled")
        codex_bin = self._resolve_bin()
        if not codex_bin:
            raise RuntimeError(f"Codex binary not found: {self.bin}")

        cleaned = self._clean_input(user_text)
        if not cleaned:
            raise RuntimeError("Message is empty")

        prompt = (
            f"{self.system_prompt}\n\n"
            "User message:\n"
            f"{cleaned}\n\n"
            "Answer now:"
        )

        if self.mode == "resume":
            try:
                return self._clean_reply(self._ask_resume(codex_bin, prompt))
            except Exception:
                if self.resume_fallback_exec:
                    return self._clean_reply(self._ask_exec(codex_bin, prompt))
                raise
        return self._clean_reply(self._ask_exec(codex_bin, prompt))


class AsyncCodexReplier:
    def __init__(self, codex_ai: CodexAssistant, telegram_token: str, telegram_force_ipv4: bool):
        self.codex_ai = codex_ai
        # Keep a dedicated Telegram client in this worker thread to avoid sharing
        # one httpx client across polling thread and async-reply thread.
        self.tg = TelegramClient(telegram_token, timeout_sec=35.0, force_ipv4=telegram_force_ipv4)
        self.q: queue.Queue[tuple[int, str]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="codex-telegram-reply-worker")
        self.thread.start()

    def enqueue(self, chat_id: int, text: str) -> None:
        self.q.put((chat_id, text))

    def _run(self) -> None:
        while True:
            chat_id, text = self.q.get()
            try:
                reply = self.codex_ai.ask(text)
            except Exception as exc:
                reply = f"codex error: {exc}"
            try:
                self.tg.send(chat_id, reply)
            except Exception as exc:
                LOG.exception("Failed to send async codex reply to chat_id=%s: %s", chat_id, exc)
            finally:
                self.q.task_done()


class AsyncCodexSessionReplier:
    def __init__(self, cfg: Config):
        self.thread_id = (cfg.codex_thread_id or "").strip()
        self.timeout_sec = cfg.codex_session_reply_timeout_sec
        self.sessions_root = Path(
            (os.environ.get("TELEGRAM_CODEX_SESSIONS_ROOT") or str(Path.home() / ".codex/sessions")).strip()
        )
        # Dedicated Telegram client in this worker thread.
        self.tg = TelegramClient(cfg.telegram_token, timeout_sec=35.0, force_ipv4=cfg.telegram_force_ipv4)
        self.q: queue.Queue[tuple[int, float, Path, int]] = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True, name="codex-session-reply-worker")
        self.thread.start()

    def _resolve_session_file(self) -> Path | None:
        if not self.thread_id:
            return None
        if not self.sessions_root.exists():
            return None
        matches = list(self.sessions_root.rglob(f"*{self.thread_id}.jsonl"))
        if not matches:
            return None
        return max(matches, key=lambda p: p.stat().st_mtime)

    def enqueue(self, chat_id: int) -> None:
        session_file = self._resolve_session_file()
        if not session_file:
            raise RuntimeError("Codex session file not found for TELEGRAM_CODEX_THREAD_ID")
        start_offset = session_file.stat().st_size
        enqueued_at = time.time()
        LOG.info(
            "session-reply enqueue chat_id=%s file=%s start_offset=%s",
            chat_id,
            session_file,
            start_offset,
        )
        self.q.put((chat_id, enqueued_at, session_file, start_offset))

    @staticmethod
    def _extract_assistant_text(line: str, min_epoch: float) -> str:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if obj.get("type") != "response_item":
            return ""
        payload = obj.get("payload") or {}
        if payload.get("type") != "message":
            return ""
        if payload.get("role") != "assistant":
            return ""
        phase = (payload.get("phase") or "").strip().lower()
        if phase not in {"commentary", "final_answer"}:
            return ""

        ts_raw = (obj.get("timestamp") or "").strip()
        if not ts_raw:
            return ""
        try:
            ts_epoch = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return ""
        if ts_epoch + 1e-6 < min_epoch:
            return ""

        pieces: list[str] = []
        for item in payload.get("content") or []:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            if item.get("type") in {"output_text", "output_markdown", "text"}:
                pieces.append(text)
        return "\n\n".join(pieces).strip()

    def _wait_for_reply(self, enqueued_at: float, session_file: Path, start_offset: int) -> str:
        deadline = time.time() + self.timeout_sec
        cursor = start_offset
        current_file = session_file
        carry = ""

        while time.time() < deadline:
            if not current_file.exists():
                newer = self._resolve_session_file()
                if newer:
                    current_file = newer
                    cursor = 0
                    carry = ""

            with current_file.open("rb") as fh:
                fh.seek(cursor)
                chunk_bytes = fh.read()
                cursor = fh.tell()

            if chunk_bytes:
                text = carry + chunk_bytes.decode("utf-8", errors="ignore")
                lines = text.splitlines()
                if text and not text.endswith("\n"):
                    carry = lines.pop() if lines else text
                else:
                    carry = ""
                for line in lines:
                    text = self._extract_assistant_text(line, enqueued_at)
                    if text:
                        return text
            time.sleep(1.0)
        if carry:
            text = self._extract_assistant_text(carry, enqueued_at)
            if text:
                return text
        return ""

    def _run(self) -> None:
        while True:
            chat_id, enqueued_at, session_file, start_offset = self.q.get()
            try:
                LOG.info("session-reply wait start chat_id=%s", chat_id)
                reply = self._wait_for_reply(enqueued_at, session_file, start_offset)
                if reply:
                    LOG.info("session-reply found chat_id=%s chars=%s", chat_id, len(reply))
                    self.tg.send(chat_id, reply)
                else:
                    LOG.warning("session-reply timeout chat_id=%s", chat_id)
                    self.tg.send(chat_id, "No assistant reply from this Codex chat yet.")
            except Exception as exc:
                LOG.exception("session-reply error chat_id=%s: %s", chat_id, exc)
                try:
                    self.tg.send(chat_id, f"session reply error: {exc}")
                except Exception:
                    LOG.exception("Failed to send session reply error to chat_id=%s", chat_id)
            finally:
                self.q.task_done()


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

        # Clear current input line so relay text doesn't concatenate with draft text.
        subprocess.run(
            [tmux_bin, "send-keys", "-t", self.target_pane, "C-u"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=4,
            check=True,
        )

        # Paste payload as one chunk.
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

        # Nudge cursor (Left/Right) so Codex flushes any paste-burst state.
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


def _cmd_and_arg(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if not raw.startswith("/"):
        return "/search", raw

    parts = raw.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def _render_stats(stats: dict) -> str:
    total = int(stats.get("total_papers", 0))
    venues = stats.get("by_venue") or {}
    years = stats.get("by_year") or {}

    lines = [f"Indexed papers: {total}", "", "Top venues:"]
    for venue, count in sorted(venues.items(), key=lambda x: int(x[1]), reverse=True)[:8]:
        lines.append(f"- {venue}: {count}")

    if years:
        lines.append("")
        lines.append("By year:")
        for year, count in sorted(years.items(), key=lambda x: int(x[0]), reverse=True):
            lines.append(f"- {year}: {count}")
    return "\n".join(lines)


def _render_search(results: list[dict], query: str) -> str:
    if not results:
        return f"No results for: {query}"

    lines = [f"Results for: {query}", ""]
    for idx, item in enumerate(results, 1):
        pid = item.get("id")
        title = _clean(item.get("title")) or "Untitled"
        venue = _clean(item.get("venue"))
        year = item.get("year")
        tier = _clean(item.get("tier"))
        abstract = _first_words(_clean(item.get("abstract") or ""), 24)
        link = _clean(item.get("url") or item.get("pdf_url") or "")

        lines.append(f"{idx}. [{pid}] {title}" if pid is not None else f"{idx}. {title}")
        meta = " | ".join([x for x in [f"{venue} {year}".strip(), tier] if x])
        if meta:
            lines.append(f"   {meta}")
        if abstract:
            lines.append(f"   {abstract}")
        if link:
            lines.append(f"   {link}")
        lines.append("")

    lines.append("Use /paper <id> for full paper details.")
    return "\n".join(lines).strip()


def _render_paper(paper_id: int, paper: dict | None) -> str:
    if not paper:
        return f"Paper id {paper_id} not found."

    title = _clean(paper.get("title"))
    venue = _clean(paper.get("venue"))
    year = paper.get("year")
    tier = _clean(paper.get("tier"))
    authors = _clean(paper.get("authors"))
    abstract = _first_words(_clean(paper.get("abstract") or ""), 80)
    url = _clean(paper.get("url"))
    pdf = _clean(paper.get("pdf_url"))

    lines = [f"[{paper_id}] {title}", f"{venue} {year} {tier}".strip()]
    if authors:
        lines.append(authors)
    if abstract:
        lines.extend(["", abstract])
    if url:
        lines.extend(["", f"Source: {url}"])
    if pdf:
        lines.append(f"PDF: {pdf}")
    return "\n".join(lines)


def _render_piper_langs(tts: PiperTTS) -> str:
    if not tts.enabled:
        return "Piper is disabled in config."
    langs = tts.available_langs()
    if not langs:
        return (
            "No Piper languages configured. Set TELEGRAM_PIPER_MODELS in .env,\n"
            "example: TELEGRAM_PIPER_MODELS=en=/path/en_US.onnx,ru=/path/ru_RU.onnx"
        )
    return "Available Piper languages: " + ", ".join(langs)


def _parse_piper_args(arg: str, known_langs: set[str]) -> tuple[str, str, str]:
    raw = (arg or "").strip()
    if not raw:
        return "", "", "Usage: /piper <lang> <text>"

    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return "", "", "Usage: /piper <lang> <text>"

    lang = _norm_lang(parts[0])
    text = parts[1].strip()

    if lang not in known_langs:
        return "", "", f"Unknown language '{parts[0]}'. Use /piperlangs"
    if not text:
        return "", "", "Usage: /piper <lang> <text>"

    return lang, text, ""


def _render_tmux_status(relay: TmuxRelay) -> str:
    state = "enabled" if relay.enabled else "disabled"
    available = "yes" if relay.is_available() else "no"
    target = relay.target_pane or "(unset)"
    plain = "yes" if relay.plain_text_to_tmux else "no"
    return (
        f"tmux relay: {state}\n"
        f"available: {available}\n"
        f"target pane: {target}\n"
        f"plain text -> tmux: {plain}"
    )


def _relay_ack_message(text: str) -> str:
    snippet = _first_words(_clean(text), 10)
    if snippet:
        return f"✅ Sent to this live Codex session.\n{snippet}"
    return "✅ Sent to this live Codex session."


def _render_codex_status(codex_ai: CodexAssistant) -> str:
    state = "enabled" if codex_ai.enabled else "disabled"
    available = "yes" if codex_ai.is_available() else "no"
    return (
        f"codex assistant: {state}\n"
        f"available: {available}\n"
        f"mode: {codex_ai.mode}\n"
        f"thread: {codex_ai.thread_id or '(unset)'}\n"
        f"model: {codex_ai.model or '(default)'}\n"
        f"timeout: {codex_ai.timeout_sec}s"
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("TELEGRAM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Avoid logging Telegram URLs that include bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cfg = load_config()
    tg = TelegramClient(
        cfg.telegram_token,
        timeout_sec=float(max(70, cfg.telegram_poll_timeout_sec + 15)),
        force_ipv4=cfg.telegram_force_ipv4,
    )
    backend = BackendClient(cfg)
    tts = PiperTTS(cfg)
    codex_ai = CodexAssistant(cfg)
    codex_replier = (
        AsyncCodexReplier(codex_ai, cfg.telegram_token, cfg.telegram_force_ipv4) if codex_ai.enabled else None
    )
    session_replier = AsyncCodexSessionReplier(cfg) if cfg.codex_thread_id else None
    relay = TmuxRelay(cfg)
    last_chat_state = LastChatState()
    reply_api = LocalReplyAPI(cfg, last_chat_state)

    me = tg.get_me()
    username = me.get("username") or "unknown"
    LOG.info("Connected as @%s", username)
    LOG.info("telegram transport force_ipv4=%s", cfg.telegram_force_ipv4)

    if cfg.piper_enabled:
        if not tts._resolve_bin():
            LOG.warning("Piper binary not found: %s", cfg.piper_bin)
        LOG.info("Piper configured languages: %s", ", ".join(tts.available_langs()) or "none")
    if codex_ai.enabled:
        LOG.info(
            "codex assistant enabled available=%s mode=%s model=%s",
            codex_ai.is_available(),
            codex_ai.mode,
            codex_ai.model or "(default)",
        )
    if relay.enabled:
        LOG.info(
            "tmux relay enabled target=%s available=%s plain_text_to_tmux=%s",
            relay.target_pane or "(unset)",
            relay.is_available(),
            relay.plain_text_to_tmux,
        )
    LOG.info(
        "plain text reply mode=%s",
        cfg.plain_text_reply_mode,
    )
    if reply_api.enabled:
        try:
            reply_api.start()
        except Exception as exc:
            LOG.exception("failed to start local reply api: %s", exc)

    # Optional backlog skip on startup.
    offset = 0
    if cfg.skip_backlog_on_start:
        try:
            pending = tg.get_updates_nowait(offset=0)
            if pending:
                offset = int(max(int(u.get("update_id", 0)) for u in pending)) + 1
                LOG.info("Skipped %s stale update(s) on startup; next offset=%s", len(pending), offset)
        except Exception as exc:
            LOG.warning("Failed to preflight updates backlog skip: %s", exc)

    while True:
        try:
            updates = tg.get_updates(offset=offset, timeout_sec=cfg.telegram_poll_timeout_sec)
            for upd in updates:
                update_id = int(upd.get("update_id", 0))
                if update_id >= offset:
                    offset = update_id + 1

                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text")
                if chat_id is None or not text:
                    continue

                chat_id_key = str(chat_id)
                if cfg.telegram_allowed_chat_ids and chat_id_key not in cfg.telegram_allowed_chat_ids:
                    LOG.warning("Ignored unauthorized chat_id=%s", chat_id_key)
                    continue
                last_chat_state.set(int(chat_id))

                plain_text_message = not text.strip().startswith("/")
                if plain_text_message:
                    LOG.info("chat_id=%s plain_text_len=%s", chat_id_key, len(text))
                    if relay.enabled and relay.plain_text_to_tmux:
                        try:
                            if cfg.plain_text_reply_mode in {"session", "quick_session"} and session_replier:
                                session_replier.enqueue(int(chat_id))
                            relay.forward(text)
                            if cfg.plain_text_reply_mode in {"quick", "quick_codex", "quick_session"}:
                                tg.send(int(chat_id), cfg.plain_text_quick_reply)
                            if cfg.plain_text_reply_mode in {"codex", "quick_codex"} and codex_ai.enabled:
                                if codex_replier:
                                    codex_replier.enqueue(int(chat_id), text)
                                else:
                                    tg.send(int(chat_id), codex_ai.ask(text))
                            if cfg.plain_text_reply_mode in {"session", "quick_session"} and not session_replier:
                                tg.send(int(chat_id), "session reply is not configured (set TELEGRAM_CODEX_THREAD_ID)")
                        except Exception as exc:
                            tg.send(int(chat_id), f"tmux relay error: {exc}")
                        continue
                    if cfg.plain_text_reply_mode in {"codex", "quick_codex"} and codex_ai.enabled:
                        try:
                            tg.send(int(chat_id), codex_ai.ask(text))
                        except Exception as exc:
                            tg.send(int(chat_id), f"codex error: {exc}")
                        continue
                    if cfg.plain_text_reply_mode in {"quick", "quick_session"}:
                        tg.send(int(chat_id), cfg.plain_text_quick_reply)
                        continue

                cmd, arg = _cmd_and_arg(text)
                LOG.info("chat_id=%s cmd=%s", chat_id_key, cmd)

                if cmd == "/start":
                    tg.send(int(chat_id), START_TEXT)
                elif cmd == "/id":
                    tg.send(int(chat_id), f"Your chat_id is: {chat_id}")
                elif cmd == "/help":
                    tg.send(int(chat_id), HELP_TEXT)
                elif cmd == "/aistatus":
                    tg.send(int(chat_id), _render_codex_status(codex_ai))
                elif cmd == "/ping":
                    tg.send(int(chat_id), f"pong {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
                elif cmd == "/ask" or cmd == "/toai":
                    if not arg:
                        tg.send(int(chat_id), "Usage: /ask <text>")
                    else:
                        if codex_ai.enabled:
                            try:
                                tg.send(int(chat_id), codex_ai.ask(arg))
                            except Exception as exc:
                                tg.send(int(chat_id), f"codex error: {exc}")
                        elif relay.enabled:
                            try:
                                relay.forward(arg)
                                tg.send(int(chat_id), _relay_ack_message(arg))
                            except Exception as exc:
                                tg.send(int(chat_id), f"tmux relay error: {exc}")
                        else:
                            tg.send(int(chat_id), "No relay is enabled.")
                elif cmd == "/tmuxstatus":
                    tg.send(int(chat_id), _render_tmux_status(relay))
                elif cmd == "/totmux":
                    if not arg:
                        tg.send(int(chat_id), "Usage: /totmux <text>")
                    else:
                        try:
                            relay.forward(arg)
                            tg.send(int(chat_id), _relay_ack_message(arg))
                        except Exception as exc:
                            tg.send(int(chat_id), f"tmux relay error: {exc}")
                elif cmd == "/stats":
                    tg.send(int(chat_id), _render_stats(backend.stats()))
                elif cmd == "/paper":
                    if not arg.isdigit():
                        tg.send(int(chat_id), "Usage: /paper <id>")
                    else:
                        tg.send(int(chat_id), _render_paper(int(arg), backend.paper(int(arg))))
                elif cmd == "/search":
                    if not arg:
                        tg.send(int(chat_id), "Usage: /search <query>")
                    else:
                        results = backend.search(arg, limit=cfg.search_limit, semantic=cfg.semantic_default)
                        tg.send(int(chat_id), _render_search(results, arg))
                elif cmd == "/piperlangs":
                    tg.send(int(chat_id), _render_piper_langs(tts))
                elif cmd == "/piper":
                    lang, piper_text, err = _parse_piper_args(
                        arg=arg,
                        known_langs=set(tts.available_langs()),
                    )
                    if err:
                        tg.send(int(chat_id), err)
                        continue
                    audio_path = None
                    try:
                        audio_path = tts.synthesize(lang=lang, text=piper_text)
                        tg.send_voice(
                            int(chat_id),
                            audio_path,
                            caption=f"Piper TTS [{lang}]",
                        )
                    except Exception as exc:
                        tg.send(int(chat_id), f"Piper error: {exc}")
                    finally:
                        try:
                            if audio_path:
                                audio_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                else:
                    tg.send(int(chat_id), HELP_TEXT)
        except Exception as exc:
            LOG.exception("Polling loop error: %s", exc)
            time.sleep(cfg.telegram_retry_sleep_sec)


if __name__ == "__main__":
    raise SystemExit(main())

"""Keep secrets out of logs.

Third-party libraries log things langclaw doesn't control: httpx logs request
URLs (the Telegram Bot API puts the bot token *in* the URL), and exception
messages can echo credentials back (``InvalidToken: The token `...` was
rejected``). Logs end up on hosting dashboards and on disk, so every sink
langclaw installs runs its output through a :class:`SecretRedactor`.

Secrets are gathered from the resolved config (channel tokens, which may come
from ``config.json`` rather than env) and from environment variables whose
names look secret (``*KEY*``, ``*TOKEN*``, ``*SECRET*``, ``*PASSWORD*``,
``*DSN*``) — which covers provider keys such as ``OPENROUTER_API_KEY`` that
LangChain reads straight from the environment.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langclaw.config.schema import LangclawConfig

REDACTED = "[REDACTED]"

# Env var name fragments that mark a value as secret.
_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DSN")

# Shorter values are skipped: replacing e.g. "true" or "abc" everywhere would
# mangle unrelated log text without protecting anything real.
_MIN_SECRET_LENGTH = 8


def collect_secrets(
    config: LangclawConfig,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the secret values to redact, longest first.

    Args:
        config: Resolved langclaw config (channel credentials are read from it).
        environ: Environment to scan; defaults to ``os.environ``.

    Returns:
        Distinct secret strings, longest first so a secret that contains
        another is replaced whole.
    """
    env = os.environ if environ is None else environ
    candidates: list[str] = [
        value
        for name, value in env.items()
        if any(marker in name.upper() for marker in _SECRET_NAME_MARKERS)
    ]
    ch = config.channels
    candidates += [
        ch.telegram.token,
        ch.discord.token,
        ch.slack.bot_token,
        ch.slack.app_token,
        ch.matrix.access_token,
        ch.api.token,
    ]
    secrets = {value for value in candidates if value and len(value) >= _MIN_SECRET_LENGTH}
    return sorted(secrets, key=len, reverse=True)


class SecretRedactor:
    """Callable that replaces every known secret in a string with ``[REDACTED]``."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text


class RedactingFormatter(logging.Formatter):
    """stdlib formatter that redacts the fully formatted record, traceback included."""

    def __init__(self, redactor: SecretRedactor, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redact(super().format(record))


class RedactingStream:
    """Text stream proxy that redacts everything written through it.

    Installed over ``sys.stdout`` / ``sys.stderr`` by the gateway so output that
    bypasses logging entirely — crash tracebacks printed by the CLI, stray
    ``print()`` calls — is redacted too. Every other attribute delegates to the
    wrapped stream.
    """

    def __init__(self, stream: Any, redactor: SecretRedactor) -> None:
        self._stream = stream
        self._redact = redactor

    def write(self, text: str) -> int:
        return self._stream.write(self._redact(text))

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def install_redacting_streams(redactor: SecretRedactor) -> None:
    """Wrap ``sys.stdout`` and ``sys.stderr`` in :class:`RedactingStream` (idempotent)."""
    import sys

    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout, redactor)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr, redactor)


def make_redacting_sink(
    redactor: SecretRedactor,
    write: Callable[[str], Any],
) -> Callable[[Any], None]:
    """Wrap *write* as a loguru sink that redacts each message before writing.

    Loguru passes function sinks the fully formatted message, with any
    exception traceback already appended, so redacting it covers both.
    """

    def sink(message: Any) -> None:
        write(redactor(str(message)))

    return sink


def make_daily_file_writer(log_dir: Path, retention_days: int = 30) -> Callable[[str], None]:
    """Return a writer appending to ``<log_dir>/YYYY-MM-DD.log`` (local date).

    A plain function (rather than loguru's own file sink) so output can pass
    through :func:`make_redacting_sink`. Logs older than *retention_days* are
    pruned when the writer is created.

    Args:
        log_dir: Directory for the daily log files (created if missing).
        retention_days: Delete ``*.log`` files older than this many days.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - retention_days * 86400
    for old in log_dir.glob("*.log"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)

    def write(text: str) -> None:
        path = log_dir / f"{datetime.now():%Y-%m-%d}.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    return write

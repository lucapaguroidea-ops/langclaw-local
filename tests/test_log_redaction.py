"""Secrets never reach the logs.

Regression for three real leaks: httpx logging the Telegram URL (which embeds
the bot token), an ``InvalidToken`` exception message echoing the token, and
loguru's ``diagnose`` tracebacks dumping local variable values.
"""

from __future__ import annotations

import logging

from loguru import logger

from langclaw.config.schema import LangclawConfig
from langclaw.log_redaction import (
    REDACTED,
    RedactingFormatter,
    SecretRedactor,
    collect_secrets,
    make_redacting_sink,
)

TOKEN = "1234567890:AAHsecretsecretsecretsecret"
API_KEY = "sk-or-v1-abcdef0123456789abcdef"


def test_collect_secrets_from_env_and_config(monkeypatch) -> None:
    monkeypatch.setenv("LANGCLAW__CHANNELS__TELEGRAM__TOKEN", TOKEN)
    cfg = LangclawConfig()
    environ = {"OPENROUTER_API_KEY": API_KEY, "HOME": "/root", "SHORT_TOKEN": "abc"}

    secrets = collect_secrets(cfg, environ=environ)

    assert TOKEN in secrets
    assert API_KEY in secrets
    assert "/root" not in secrets  # not a secret-looking name
    assert "abc" not in secrets  # too short to redact safely


def test_redactor_replaces_every_occurrence() -> None:
    redact = SecretRedactor([TOKEN])
    text = f"POST https://api.telegram.org/bot{TOKEN}/getMe and again {TOKEN}"
    assert TOKEN not in redact(text)
    assert redact(text).count(REDACTED) == 2


def test_stdlib_formatter_redacts_message_and_traceback() -> None:
    formatter = RedactingFormatter(SecretRedactor([TOKEN]), "%(message)s")
    try:
        raise ValueError(f"The token `{TOKEN}` was rejected by the server.")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1, "POST /bot%s/getMe", (TOKEN,), sys.exc_info()
        )

    output = formatter.format(record)

    assert TOKEN not in output
    assert "getMe" in output and "rejected by the server" in output


def test_loguru_sink_redacts_message_and_exception() -> None:
    written: list[str] = []
    sink = make_redacting_sink(SecretRedactor([TOKEN]), written.append)
    handler_id = logger.add(sink, level="INFO", diagnose=False)
    try:
        token = TOKEN  # noqa: F841 — a local that diagnose=True would have dumped
        try:
            raise RuntimeError(f"Gateway task failed: {TOKEN}")
        except RuntimeError:
            logger.exception(f"Error handling message with {TOKEN}")
    finally:
        logger.remove(handler_id)

    output = "".join(written)
    assert TOKEN not in output
    assert "Gateway task failed" in output


def test_daily_file_writer_appends_and_prunes_old_logs(tmp_path) -> None:
    import os
    import time

    from langclaw.log_redaction import make_daily_file_writer

    stale = tmp_path / "2000-01-01.log"
    stale.write_text("old\n")
    old_time = time.time() - 40 * 86400
    os.utime(stale, (old_time, old_time))

    write = make_daily_file_writer(tmp_path, retention_days=30)
    write("one\n")
    write("two\n")

    assert not stale.exists()
    (today,) = tmp_path.glob("*.log")
    assert today.read_text() == "one\ntwo\n"


def test_redacting_stream_covers_direct_writes() -> None:
    """Crash tracebacks (e.g. Typer's) bypass log sinks and hit stderr directly."""
    import io

    from langclaw.log_redaction import RedactingStream

    target = io.StringIO()
    stream = RedactingStream(target, SecretRedactor([TOKEN]))
    stream.write(f"InvalidToken: The token `{TOKEN}` was rejected by the server.\n")
    stream.flush()

    assert TOKEN not in target.getvalue()
    assert "rejected by the server" in target.getvalue()
    assert stream.getvalue() == target.getvalue()  # other attributes delegate

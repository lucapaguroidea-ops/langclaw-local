"""A channel enabled in config with missing credentials fails loudly at startup.

Previously the gateway dropped it silently (``is_enabled()`` is False), while the
startup log still listed it — so a bot with an empty token just never answered.
"""

from __future__ import annotations

import pytest

from langclaw.app import Langclaw
from langclaw.config.schema import LangclawConfig


def _config(monkeypatch: pytest.MonkeyPatch, **env: str) -> LangclawConfig:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return LangclawConfig()


def test_enabled_telegram_without_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("telegram")
    cfg = _config(monkeypatch, LANGCLAW__CHANNELS__TELEGRAM__ENABLED="true")

    with pytest.raises(ValueError, match="LANGCLAW__CHANNELS__TELEGRAM__TOKEN"):
        Langclaw(config=cfg)._build_all_channels()


def test_enabled_slack_names_every_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("slack_bolt")
    cfg = _config(
        monkeypatch,
        LANGCLAW__CHANNELS__SLACK__ENABLED="true",
        LANGCLAW__CHANNELS__SLACK__BOT_TOKEN="xoxb-present",
    )

    with pytest.raises(ValueError, match="LANGCLAW__CHANNELS__SLACK__APP_TOKEN") as exc:
        Langclaw(config=cfg)._build_all_channels()
    assert "BOT_TOKEN" not in str(exc.value)


def test_enabled_telegram_with_token_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("telegram")
    cfg = _config(
        monkeypatch,
        LANGCLAW__CHANNELS__TELEGRAM__ENABLED="true",
        LANGCLAW__CHANNELS__TELEGRAM__TOKEN="123:abc",
    )

    channels = Langclaw(config=cfg)._build_all_channels()
    assert [ch.name for ch in channels] == ["telegram"]

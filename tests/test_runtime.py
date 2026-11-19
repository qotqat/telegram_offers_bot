"""Offline worker cleanup, alerts, log redaction and CLI safety checks."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import TelegramError

import broadcaster as broadcaster_cli
from app import listener, runtime
from app.config import Config, ConfigError
from app.logging_setup import RedactingFormatter, process_log_path


@pytest.fixture(autouse=True)
def clear_alert_cooldowns():
    runtime._ALERT_TIMES.clear()
    yield
    runtime._ALERT_TIMES.clear()


@pytest.fixture()
def config(tmp_path):
    return Config(
        bot_token="123456:offline-test-token-not-real",
        owner_id=42,
        target_channels=["@test"],
        tg_api_id=123,
        tg_api_hash="offline-hash",
        tg_session_path=tmp_path / "session",
        source_channels=["@source"],
        require_amazon_link=False,
        db_path=tmp_path / "offers.db",
    )


@pytest.mark.parametrize("owner", [None, 0, -1, "@Mahmoud_Qotqat", True])
def test_alert_never_uses_usernames_or_nonpositive_owner_ids(config, owner):
    config.owner_id = owner
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(runtime.notify_error(config, "listener", RuntimeError("secret"), bot=bot))
    bot.send_message.assert_not_called()


def test_alerts_are_class_only_plain_text_and_throttled(config):
    bot = SimpleNamespace(send_message=AsyncMock())

    async def check():
        await runtime.notify_error(config, "listener", RuntimeError("private-token"), bot=bot)
        await runtime.notify_error(config, "listener", RuntimeError("other-secret"), bot=bot)
        await runtime.notify_error(config, "broadcaster", RuntimeError("secret"), bot=bot)

    asyncio.run(check())
    assert bot.send_message.await_count == 2
    for call in bot.send_message.call_args_list:
        assert call.kwargs["chat_id"] == 42
        assert call.kwargs["parse_mode"] is None
        assert "RuntimeError" in call.kwargs["text"]
        assert "secret" not in call.kwargs["text"]
        assert "private-token" not in call.kwargs["text"]


def test_failed_alert_is_best_effort_and_also_throttled(config):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("DM closed")))

    async def check():
        await runtime.notify_error(config, "listener", ValueError(), bot=bot)
        await runtime.notify_error(config, "listener", ValueError(), bot=bot)

    asyncio.run(check())
    assert bot.send_message.await_count == 1


def test_alert_uses_and_closes_its_own_bot(config, monkeypatch):
    bot = SimpleNamespace(send_message=AsyncMock())
    manager = Mock()
    manager.__aenter__ = AsyncMock(return_value=bot)
    manager.__aexit__ = AsyncMock(return_value=False)
    factory = Mock(return_value=manager)
    monkeypatch.setattr(runtime, "Bot", factory)
    asyncio.run(runtime.notify_error(config, "listener", RuntimeError("secret")))
    factory.assert_called_once_with(config.bot_token)
    manager.__aexit__.assert_awaited_once()
    bot.send_message.assert_awaited_once()


def test_alert_timeout_is_bounded(config, monkeypatch):
    monkeypatch.setattr(runtime, "ALERT_TIMEOUT_SECONDS", 0.001)
    cancelled = []

    async def blocked(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    asyncio.run(runtime.notify_error(
        config, "listener", RuntimeError(), bot=SimpleNamespace(send_message=blocked)
    ))
    assert cancelled == [True]


def test_worker_alerts_before_reraising(config, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(runtime, "notify_error", notify)
    error = RuntimeError("offline")

    async def fail():
        raise error

    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(runtime.run_worker(config, "listener", fail))
    notify.assert_awaited_once_with(config, "listener", error)


def test_offline_worker_failure_does_not_alert(config, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(runtime, "notify_error", notify)

    async def fail():
        raise RuntimeError("offline")

    with pytest.raises(RuntimeError):
        asyncio.run(runtime.run_worker(config, "preview", fail, alerts=False))
    notify.assert_not_called()


def test_sigterm_cancels_worker_runs_cleanup_and_restores_handler(config, monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)
    notify = AsyncMock()
    monkeypatch.setattr(runtime, "notify_error", notify)
    cleanup = []

    async def work():
        try:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
            await asyncio.Event().wait()
        finally:
            cleanup.append(True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.run_worker(config, "listener", work))
    assert cleanup == [True]
    assert signal.getsignal(signal.SIGTERM) == previous
    notify.assert_not_called()


def test_redaction_covers_messages_urls_and_tracebacks():
    token = "123456:abcdefghijklmnopqrstuvwxyz0123456789"
    formatter = RedactingFormatter(("private-api-hash", "credential-secret"))
    try:
        raise ValueError("credential-secret private-api-hash " + token)
    except ValueError:
        import sys
        info = sys.exc_info()
    record = logging.LogRecord(
        "httpx", logging.ERROR, "test", 1,
        "https://api.telegram.org/bot%s/sendMessage", (token,), info,
    )
    text = formatter.format(record)
    assert token not in text
    assert "private-api-hash" not in text
    assert "credential-secret" not in text
    assert "ValueError" in text


def test_log_files_are_process_specific_and_explicit_names_are_preserved():
    assert process_log_path("data/bot.log", "listener") == Path("data/bot-listener.log")
    assert process_log_path("data/bot.log", "admin") == Path("data/bot-admin.log")
    assert process_log_path("data/logs/listener.log", "listener") == Path("data/logs/listener.log")
    assert process_log_path("data/custom-admin.log", "admin") == Path("data/custom-admin.log")


def test_listener_noninteractive_requires_authorized_session_and_always_cleans_up(config, monkeypatch):
    conn = Mock()
    client = SimpleNamespace(
        connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
        start=AsyncMock(), disconnect=AsyncMock(),
    )
    monkeypatch.setattr(listener.db, "init_db", Mock(return_value=conn))
    monkeypatch.setattr(listener, "TelegramClient", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="not authorized"):
        asyncio.run(listener.run(config, non_interactive=True))
    client.start.assert_not_called()
    client.disconnect.assert_awaited_once()
    conn.close.assert_called_once()


def test_listener_noninteractive_reuses_authorized_session_without_prompt(config, monkeypatch):
    conn = Mock()
    client = SimpleNamespace(
        connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
        start=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=42)),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(listener.db, "init_db", Mock(return_value=conn))
    monkeypatch.setattr(listener, "TelegramClient", Mock(return_value=client))
    monkeypatch.setattr(listener, "resolve_sources", AsyncMock(return_value=[
        listener.Source("@source", object())
    ]))
    asyncio.run(listener.run(config, non_interactive=True, catchup=False, once=True))
    client.start.assert_not_called()
    client.disconnect.assert_awaited_once()
    conn.close.assert_called_once()


def test_listener_closes_db_if_client_construction_fails(config, monkeypatch):
    conn = Mock()
    monkeypatch.setattr(listener.db, "init_db", Mock(return_value=conn))
    monkeypatch.setattr(listener, "TelegramClient", Mock(side_effect=RuntimeError("constructor")))
    with pytest.raises(RuntimeError, match="constructor"):
        asyncio.run(listener.run(config))
    conn.close.assert_called_once()


def test_listener_closes_db_even_if_disconnect_fails(config, monkeypatch):
    conn = Mock()
    client = SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError("startup")),
        disconnect=AsyncMock(side_effect=RuntimeError("disconnect")),
    )
    monkeypatch.setattr(listener.db, "init_db", Mock(return_value=conn))
    monkeypatch.setattr(listener, "TelegramClient", Mock(return_value=client))
    with pytest.raises(RuntimeError, match="disconnect"):
        asyncio.run(listener.run(config))
    conn.close.assert_called_once()


def test_broadcaster_dry_run_never_constructs_bot(config, monkeypatch):
    bot = Mock(side_effect=AssertionError("network must not initialize"))
    monkeypatch.setattr(broadcaster_cli, "Bot", bot)
    args = broadcaster_cli.parse_args(["--dry-run"])
    assert asyncio.run(broadcaster_cli._run(config, args)) == 0
    bot.assert_not_called()


def test_broadcaster_closes_db_on_bot_startup_failure(config, monkeypatch):
    conn = Mock()
    monkeypatch.setattr(broadcaster_cli.db, "init_db", Mock(return_value=conn))
    manager = Mock()
    manager.__aenter__ = AsyncMock(side_effect=TelegramError("startup"))
    manager.__aexit__ = AsyncMock()
    monkeypatch.setattr(broadcaster_cli, "Bot", Mock(return_value=manager))
    with pytest.raises(TelegramError):
        asyncio.run(broadcaster_cli._run(config, broadcaster_cli.parse_args(["--once"])))
    conn.close.assert_called_once()


def test_unknown_affiliate_tag_blocks_before_bot_initialization(config, monkeypatch):
    config.require_amazon_link = True
    config.amazon_link_mode = "affiliate"
    monkeypatch.setattr(broadcaster_cli.Config, "load", Mock(return_value=config))
    bot = Mock()
    monkeypatch.setattr(broadcaster_cli, "Bot", bot)
    assert broadcaster_cli.main(["--once"]) == 1
    bot.assert_not_called()


def test_dry_run_failure_never_attempts_owner_alert(config, monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(runtime, "notify_error", notify)
    monkeypatch.setattr(broadcaster_cli.Config, "load", Mock(return_value=config))
    monkeypatch.setattr(broadcaster_cli, "setup_logging", Mock())
    monkeypatch.setattr(broadcaster_cli, "_run", AsyncMock(side_effect=OSError("offline DB failure")))
    assert broadcaster_cli.main(["--dry-run"]) == 1
    notify.assert_not_called()


def test_known_but_unconfirmed_tag_cannot_publish(config):
    config.amazon_link_mode = "affiliate"
    config.amazon_associate_tag = "sample-not-proven-owned-20"
    config.require_amazon_link = True
    with pytest.raises(ConfigError, match="AMAZON_TAG_CONFIRMED"):
        config.require_broadcaster()

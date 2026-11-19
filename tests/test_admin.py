"""Admin controls and CLI lifecycle tests; no credentials or network required."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, Updater
from telegram.request import HTTPXRequest

import admin_bot
from app import admin, broadcaster, db
from app.config import Config, ConfigError
from app.listener import process_message
from app.models import Offer, Stats


@pytest.fixture(autouse=True)
def block_telegram_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Admin tests must not access Telegram")

    monkeypatch.setattr(HTTPXRequest, "do_request", forbidden)


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    handler = AsyncMock(return_value=None)
    fatal = AsyncMock(return_value=None)
    monkeypatch.setattr(admin, "notify_error", handler)
    monkeypatch.setattr(admin_bot, "notify_error", fatal)
    return SimpleNamespace(handler=handler, fatal=fatal)


@pytest.fixture()
def config(tmp_path):
    return Config(
        bot_token="123456:offline-test-token",
        owner_id=42,
        target_channels=["@deals_test"],
        db_path=tmp_path / "offers.db",
        log_path=tmp_path / "admin.log",
        require_amazon_link=False,
    )


@pytest.fixture()
def conn(config):
    connection = db.init_db(config.db_path)
    yield connection
    connection.close()


def fake_update(user_id=42, chat_type="private", *, message=True) -> Any:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="same_username"),
        effective_chat=SimpleNamespace(id=42, type=chat_type),
        effective_message=SimpleNamespace(reply_text=AsyncMock()) if message else None,
    )


def reply_text(update):
    call = update.effective_message.reply_text.call_args
    assert call.kwargs == {"parse_mode": None, "disable_web_page_preview": True}
    text = call.args[0]
    assert len(text.encode("utf-16-le")) // 2 <= admin.MAX_MESSAGE_LENGTH
    return text


def invoke(callback, update, context=None):
    asyncio.run(callback(update, context or Mock()))


def insert_offer(conn, label, *, source: str | None = "@source", status="pending", today=True):
    offer_id = db.insert_offer(
        conn,
        Offer(
            title=f"Offer {label}",
            link=f"https://example.com/{label}",
            source_type="telegram",
            content_hash=label,
            source_channel=source,
        ),
    )
    assert offer_id is not None
    with conn:
        conn.execute(
            "UPDATE offers SET status = ?, created_at = ? WHERE id = ?",
            (status, "2026-11-19T10:00:00Z" if today else "2026-11-18T23:59:59Z", offer_id),
        )
    return offer_id


@pytest.mark.parametrize("command", ["start", "pause", "resume", "stats"])
@pytest.mark.parametrize(
    "case",
    [
        "other_user", "same_username", "owner_group", "owner_supergroup",
        "owner_channel", "missing_user", "missing_user_id", "missing_chat",
        "missing_message", "string_user_id", "boolean_user_id",
    ],
)
def test_commands_deny_without_database_access(config, conn, monkeypatch, command, case):
    update = fake_update()
    if case in {"other_user", "same_username"}:
        update.effective_user.id = 7
    elif case.startswith("owner_"):
        update.effective_chat.type = case.removeprefix("owner_")
    elif case == "missing_user":
        update.effective_user = None
    elif case == "missing_user_id":
        update.effective_user = SimpleNamespace(username="same_username")
    elif case == "missing_chat":
        update.effective_chat = None
    elif case == "missing_message":
        update.effective_message = None
    elif case == "string_user_id":
        update.effective_user.id = "42"
    elif case == "boolean_user_id":
        config.owner_id = 1
        update.effective_user.id = True
    spies = {}
    for name in ("get_stats", "set_paused", "is_paused"):
        spies[name] = Mock(side_effect=AssertionError("Unauthorized DB access"))
        monkeypatch.setattr(db, name, spies[name])
    changes = conn.total_changes

    asyncio.run(getattr(admin.AdminHandlers(config, conn), command)(update, None))

    for spy in spies.values():
        spy.assert_not_called()
    assert conn.total_changes == changes
    if update.effective_message is not None:
        update.effective_message.reply_text.assert_awaited_once()
        assert reply_text(update) == admin.ACCESS_DENIED


@pytest.mark.parametrize("owner_id", [None, 0, -42, "42", "@owner", True, 42.0])
@pytest.mark.parametrize("command", ["start", "pause", "resume", "stats"])
def test_invalid_owner_fails_closed_in_callbacks(config, conn, owner_id, command):
    config.owner_id = owner_id
    update = fake_update(user_id=owner_id)
    changes = conn.total_changes

    asyncio.run(getattr(admin.AdminHandlers(config, conn), command)(update, None))

    assert reply_text(update) == admin.ACCESS_DENIED
    assert conn.total_changes == changes
    assert not db.is_paused(conn)


def test_start_lists_commands_without_reading_stats(config, conn, monkeypatch):
    stats = Mock(side_effect=AssertionError("Help must not read stats"))
    monkeypatch.setattr(db, "get_stats", stats)
    update = fake_update()

    invoke(admin.AdminHandlers(config, conn).start, update)

    text = reply_text(update)
    assert "Bosla Deals" in text
    for command in ("/start", "/pause", "/resume", "/stats"):
        assert command in text
    assert "UTC" in text
    assert "ingestion continues" in text
    stats.assert_not_called()


@pytest.mark.parametrize("paused,command", [(True, "pause"), (False, "resume")])
def test_pause_resume_idempotent_shared_and_persistent(config, conn, paused, command):
    reader = db.connect(config.db_path)
    try:
        db.set_paused(conn, not paused)
        handlers = admin.AdminHandlers(config, conn)
        for _ in range(2):
            update = fake_update()
            asyncio.run(getattr(handlers, command)(update, None))
            assert db.is_paused(reader) is paused
            assert db.get_state(reader, "paused") == ("1" if paused else "0")
            expected = "Publication paused" if paused else "Publication resumed"
            assert expected in reply_text(update)
        assert reader.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    finally:
        reader.close()

    reopened = db.init_db(config.db_path)
    try:
        assert db.is_paused(reopened) is paused
    finally:
        reopened.close()


def test_paused_ingestion_continues_and_broadcaster_waits(config, conn):
    worker = db.connect(config.db_path)
    handlers = admin.AdminHandlers(config, conn)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))
    message = SimpleNamespace(
        id=100,
        message="Air Fryer 5.8QT\nPrice: $49.99 was $79.99\nhttps://shop.example.com/p/1",
        get_entities_text=list,
    )
    try:
        invoke(handlers.pause, fake_update())
        offer_id = asyncio.run(
            process_message(Mock(), message, "@source", conn=worker, config=config)
        )
        assert offer_id is not None
        stored = db.get_offer(conn, offer_id)
        assert stored is not None and stored.status == "pending"

        result = asyncio.run(broadcaster.broadcast_once(worker, config, bot))
        assert result.sent == 0
        bot.send_message.assert_not_awaited()
        assert conn.execute("SELECT COUNT(*) FROM post_deliveries").fetchone()[0] == 0

        update = fake_update()
        invoke(handlers.stats, update)
        assert "Pending offers (all time): 1" in reply_text(update)
        assert "Publication paused: yes" in reply_text(update)

        invoke(handlers.resume, fake_update())
        result = asyncio.run(broadcaster.broadcast_once(worker, config, bot))
        assert result.sent == 1
        bot.send_message.assert_awaited_once()
        stored = db.get_offer(conn, offer_id)
        assert stored is not None and stored.status == "posted"
    finally:
        worker.close()


def test_stats_utc_delivery_counts_all_time_queue_and_shared_db(config, conn, monkeypatch):
    monkeypatch.setattr(db, "utc_day_start_iso", lambda: "2026-11-19T00:00:00Z")
    writer = db.connect(config.db_path)
    try:
        posted = insert_offer(writer, "posted", status="posted")
        insert_offer(writer, "rejected", source="@other", status="rejected")
        pending = insert_offer(writer, "pending", source=None)
        failed = insert_offer(writer, "failed")
        insert_offer(writer, "old-pending", today=False)
        old = insert_offer(writer, "old-posted", status="posted", today=False)
        insert_offer(writer, "old-rejected", status="rejected", today=False)
        for channel in ("@a", "@b"):
            delivery = db.ensure_delivery(writer, posted, channel)
            db.mark_delivery_sent(writer, delivery, 1)
            with writer:
                writer.execute(
                    "UPDATE post_deliveries SET sent_at = ? WHERE id = ?",
                    ("2026-11-19T00:00:00Z", delivery),
                )
        delivery = db.ensure_delivery(writer, old, "@a")
        db.mark_delivery_sent(writer, delivery, 2)
        with writer:
            writer.execute(
                "UPDATE post_deliveries SET sent_at = ? WHERE id = ?",
                ("2026-11-18T23:59:59Z", delivery),
            )
        db.mark_delivery_failed(writer, db.ensure_delivery(writer, failed, "@a"), "offline")
        db.mark_delivery_skipped(writer, db.ensure_delivery(writer, pending, "@a"), "route")
        db.set_paused(writer, True)
        changes = conn.total_changes
        update = fake_update()

        invoke(admin.AdminHandlers(config, conn).stats, update)

        assert reply_text(update) == (
            "Stats - today (UTC)\n"
            "Collected: 4\nRejected: 1\nPosted deliveries: 2\n"
            "Pending offers (all time): 3\nPublication paused: yes\n\n"
            "Per source (collected today, UTC):\n"
            "@source: 2\n(unknown): 1\n@other: 1"
        )
        assert conn.total_changes == changes
    finally:
        writer.close()


def test_empty_stats(config, conn):
    update = fake_update()
    invoke(admin.AdminHandlers(config, conn).stats, update)
    text = reply_text(update)
    assert "Collected: 0" in text
    assert "Posted deliveries: 0" in text
    assert "Publication paused: no" in text
    assert text.endswith("(none)")


def test_stats_caps_sources_and_handles_emoji_newlines_and_plain_text(config, conn, monkeypatch):
    sources = {f"<b>🔥_[source]{index:03d}</b>\n" + "🔥" * 200: 1 for index in range(100)}
    monkeypatch.setattr(db, "get_stats", lambda connection: Stats(per_source=sources))
    update = fake_update()

    invoke(admin.AdminHandlers(config, conn).stats, update)

    text = reply_text(update)
    source_lines = text.splitlines()[8:-1]
    assert len(source_lines) == admin.MAX_STATS_SOURCES
    for line in source_lines:
        label, count = line.rsplit(": ", 1)
        assert len(label.encode("utf-16-le")) // 2 <= admin.MAX_SOURCE_LENGTH
        assert label.endswith("…")
        assert count == "1"
    assert "<b>🔥_[source]000</b>" in text
    assert "source]020" not in text
    assert text.endswith("80 more source(s) omitted.")


@pytest.mark.parametrize(
    "text,expected",
    [("a" * 4096, "a" * 4096), ("🔥" * 2048, "🔥" * 2048),
     ("🔥" * 3000, "🔥" * 2047 + "…"), ("x" * 5000, "x" * 4095 + "…")],
    ids=["ascii-boundary", "emoji-boundary", "emoji-overflow", "ascii-overflow"],
)
def test_clip_text_utf16_boundary(text, expected):
    assert admin.clip_text(text) == expected
    assert len(admin.clip_text(text).encode("utf-16-le")) // 2 <= 4096


@pytest.mark.parametrize("owner_id", [None, 0, -1, "42", "@owner", True, 42.0])
def test_build_rejects_missing_or_invalid_owner(config, conn, owner_id):
    config.owner_id = owner_id
    with pytest.raises(ConfigError, match="OWNER_ID.*positive numeric"):
        admin.build_application(config, conn)
    assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["bot_token", "target_channels"])
def test_build_uses_require_bot(config, conn, field):
    setattr(config, field, None if field == "bot_token" else [])
    with pytest.raises(ConfigError, match="BOT_TOKEN" if field == "bot_token" else "TARGET_CHANNELS"):
        admin.build_application(config, conn)


def test_build_application_offline_registers_guarded_handlers(config, conn):
    application = admin.build_application(config, conn)
    assert isinstance(application, Application)
    registered = application.handlers[0]
    assert len(registered) == 4
    commands = set()
    for handler in registered:
        assert isinstance(handler, CommandHandler)
        commands.update(handler.commands)
        assert isinstance(handler.callback, MethodType)
        assert isinstance(handler.callback.__self__, admin.AdminHandlers)
    assert commands == {"start", "pause", "resume", "stats"}
    assert len(application.error_handlers) == 1
    on_error = next(iter(application.error_handlers))
    assert isinstance(on_error, MethodType)
    first_callback = registered[0].callback
    assert isinstance(first_callback, MethodType)
    assert on_error.__self__ is first_callback.__self__
    assert on_error.__func__ is admin.AdminHandlers.on_error
    assert application.error_handlers[on_error]
    assert application.update_processor.max_concurrent_updates == 1
    assert not application.running
    assert conn.execute("SELECT 1").fetchone()[0] == 1


@pytest.mark.parametrize(
    "origin", ["owner", "unauthorized", "nonprivate", "polling"]
)
def test_error_handler_alerts_owner_without_exposing_update_or_secrets(
    config, conn, alerts, caplog, origin
):
    update = None if origin == "polling" else fake_update()
    if update is not None:
        update.secret_payload = "private-update-text"
        if origin == "unauthorized":
            update.effective_user.id = 7
        elif origin == "nonprivate":
            update.effective_chat.type = "group"
    error = TelegramError(f"offline failure {config.bot_token}")
    bot = Mock()
    context = SimpleNamespace(error=error, bot=bot)
    changes = conn.total_changes

    with caplog.at_level(logging.ERROR, logger="app.admin"):
        invoke(admin.AdminHandlers(config, conn).on_error, update, context)

    alerts.handler.assert_awaited_once_with(config, "admin", error, bot=bot)
    alerts.fatal.assert_not_awaited()
    assert "Admin bot update/polling failed (TelegramError)" in caplog.text
    assert "offline failure" not in caplog.text
    assert config.bot_token not in caplog.text
    assert "private-update-text" not in caplog.text
    assert caplog.records[-1].exc_info is None
    assert conn.total_changes == changes
    if update is not None:
        update.effective_message.reply_text.assert_not_awaited()


def test_error_handler_without_error_does_not_alert(config, conn, alerts, caplog):
    update = fake_update()
    invoke(
        admin.AdminHandlers(config, conn).on_error,
        update,
        SimpleNamespace(error=None, bot=Mock()),
    )
    alerts.handler.assert_not_awaited()
    assert not caplog.records
    update.effective_message.reply_text.assert_not_awaited()


def test_application_dispatches_errors_to_owner_alert(config, conn, alerts):
    application = admin.build_application(config, conn)
    update = fake_update(user_id=7, chat_type="group")
    error = TelegramError("offline dispatch error")

    assert asyncio.run(application.process_error(update, error)) is False

    alerts.handler.assert_awaited_once_with(config, "admin", error, bot=application.bot)
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, RuntimeError("poll failed"), TelegramError("offline"), KeyboardInterrupt(), SystemExit(0)])
def test_run_closes_database_and_loop_after_polling(config, monkeypatch, failure):
    connection = db.connect(config.db_path)
    monkeypatch.setattr(admin_bot.db, "connect", Mock(return_value=connection))
    loops = []

    def poll(**kwargs):
        assert kwargs == {"allowed_updates": ["message"], "bootstrap_retries": 0, "close_loop": False}
        # No stop_signals override: PTB retains its platform signal defaults.
        loop = asyncio.get_event_loop()
        assert not loop.is_closed()
        loops.append(loop)
        assert db.schema_version(connection) == db.SCHEMA_VERSION
        if failure is not None:
            raise failure

    application = SimpleNamespace(run_polling=Mock(side_effect=poll))
    build = Mock(return_value=application)
    monkeypatch.setattr(admin_bot.admin, "build_application", build)

    if failure is None:
        admin_bot.run(config)
    else:
        with pytest.raises(type(failure)):
            admin_bot.run(config)

    build.assert_called_once_with(config, connection)
    assert len(loops) == 1 and loops[0].is_closed()
    with pytest.raises(RuntimeError, match="no current event loop"):
        asyncio.get_event_loop()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("stage", ["initialize", "start_polling"])
def test_real_ptb_failure_shuts_down_and_closes_database(config, monkeypatch, stage):
    connection = db.connect(config.db_path)
    monkeypatch.setattr(admin_bot.db, "connect", Mock(return_value=connection))
    failure = TelegramError("offline PTB startup failure")
    initialize = AsyncMock(
        return_value=None, side_effect=failure if stage == "initialize" else None
    )
    start_polling = AsyncMock(side_effect=failure)
    shutdown = AsyncMock()
    monkeypatch.setattr(Application, "initialize", initialize)
    monkeypatch.setattr(Application, "shutdown", shutdown)
    monkeypatch.setattr(Updater, "start_polling", start_polling)

    with pytest.raises(TelegramError, match="offline PTB startup failure"):
        admin_bot.run(config)

    initialize.assert_awaited_once()
    shutdown.assert_awaited_once()
    if stage == "initialize":
        start_polling.assert_not_awaited()
    else:
        start_polling.assert_awaited_once()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="no current event loop"):
        asyncio.get_event_loop()


@pytest.mark.parametrize("stage", ["schema", "build", "new_loop", "set_loop"])
def test_run_closes_database_on_setup_failure(config, monkeypatch, stage):
    connection = db.connect(config.db_path)
    monkeypatch.setattr(admin_bot.db, "connect", Mock(return_value=connection))
    application = SimpleNamespace(run_polling=Mock())
    monkeypatch.setattr(admin_bot.admin, "build_application", Mock(return_value=application))
    error = RuntimeError("setup failed")
    loop = None
    if stage == "schema":
        monkeypatch.setattr(admin_bot.db, "init_db", Mock(side_effect=error))
    elif stage == "build":
        monkeypatch.setattr(admin_bot.admin, "build_application", Mock(side_effect=error))
    elif stage == "new_loop":
        monkeypatch.setattr(admin_bot.asyncio, "new_event_loop", Mock(side_effect=error))
    else:
        loop = asyncio.new_event_loop()
        monkeypatch.setattr(admin_bot.asyncio, "new_event_loop", Mock(return_value=loop))
        monkeypatch.setattr(admin_bot.asyncio, "set_event_loop", Mock(side_effect=[error, None]))

    with pytest.raises(RuntimeError, match="setup failed"):
        admin_bot.run(config)

    application.run_polling.assert_not_called()
    if loop is not None:
        assert loop.is_closed()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_run_schema_failure_uses_owned_connection(config, monkeypatch):
    connection = db.connect(config.db_path)
    with connection:
        connection.executescript("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
        db.set_state(connection, "schema_version", "999")
    monkeypatch.setattr(admin_bot.db, "connect", Mock(return_value=connection))
    with pytest.raises(RuntimeError, match="newer than this code"):
        admin_bot.run(config)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("owner_id", [None, 0, -1, "@owner"])
def test_run_invalid_owner_never_opens_database(config, monkeypatch, owner_id):
    config.owner_id = owner_id
    connect = Mock()
    monkeypatch.setattr(admin_bot.db, "connect", connect)
    with pytest.raises(ConfigError, match="OWNER_ID"):
        admin_bot.run(config)
    connect.assert_not_called()


def test_cli_arguments():
    assert admin_bot.parse_args([]).env == ".env"
    assert admin_bot.parse_args(["--env", "offline-settings"]).env == "offline-settings"


@pytest.fixture()
def cli(config, monkeypatch):
    # Config.load is always mocked: never inspect the user's environment files.
    load = Mock(return_value=config)
    setup = Mock()
    run = Mock()
    log_path = config.log_path.with_name("isolated-admin.log")
    process_path = Mock(return_value=log_path)
    monkeypatch.setattr(admin_bot.Config, "load", load)
    monkeypatch.setattr(admin_bot, "setup_logging", setup)
    monkeypatch.setattr(admin_bot, "process_log_path", process_path)
    monkeypatch.setattr(admin_bot, "run", run)
    return SimpleNamespace(
        load=load, setup=setup, run=run, process_path=process_path, log_path=log_path
    )


def test_main_success_passes_config_and_explicit_env(config, cli, alerts):
    config.tg_api_hash = "offline-api-hash"
    config.amazon_credential_secret = "offline-amazon-secret"

    assert admin_bot.main(["--env", "offline-settings"]) == 0

    cli.load.assert_called_once_with("offline-settings")
    cli.process_path.assert_called_once_with(config.log_path, "admin")
    cli.setup.assert_called_once_with(
        cli.log_path,
        config.log_level,
        secrets=(config.bot_token, config.tg_api_hash, config.amazon_credential_secret),
    )
    cli.run.assert_called_once_with(config)
    alerts.fatal.assert_not_awaited()


@pytest.mark.parametrize("owner_id", [None, 0, -1, "@owner"])
def test_main_rejects_invalid_owner_before_logging_or_run(
    config, cli, owner_id, capsys, alerts
):
    config.owner_id = owner_id
    assert admin_bot.main([]) == 1
    assert "OWNER_ID must be a positive numeric Telegram user ID" in capsys.readouterr().err
    cli.process_path.assert_not_called()
    cli.setup.assert_not_called()
    cli.run.assert_not_called()
    alerts.fatal.assert_not_awaited()


def test_main_reports_config_load_error_without_raw_details(config, cli, capsys, alerts):
    cli.load.side_effect = ConfigError(f"invalid private setting {config.bot_token}")
    assert admin_bot.main([]) == 1
    output = capsys.readouterr()
    assert "Config error (ConfigError)" in output.err
    assert config.bot_token not in output.err + output.out
    assert "invalid private setting" not in output.err
    cli.process_path.assert_not_called()
    cli.setup.assert_not_called()
    cli.run.assert_not_called()
    alerts.fatal.assert_not_awaited()


@pytest.mark.parametrize("error", [TelegramError("offline failure"), RuntimeError("poll failure"), OSError("storage failure")])
def test_main_logs_operational_failure_class_and_alerts(config, cli, error, caplog, alerts):
    cli.run.side_effect = error
    with caplog.at_level(logging.ERROR):
        assert admin_bot.main([]) == 1
    assert "Admin bot stopped after a startup or polling failure" in caplog.text
    assert type(error).__name__ in caplog.text
    assert str(error) not in caplog.text
    assert caplog.records[-1].exc_info is None
    alerts.fatal.assert_awaited_once_with(config, "admin", error)


@pytest.mark.parametrize("stage", ["process_path", "setup"])
def test_main_logging_failure_does_not_run(config, cli, caplog, alerts, stage):
    error = OSError(f"log unavailable {config.bot_token}")
    getattr(cli, stage).side_effect = error
    with caplog.at_level(logging.ERROR):
        assert admin_bot.main([]) == 1
    assert "OSError" in caplog.text
    assert "log unavailable" not in caplog.text
    assert config.bot_token not in caplog.text
    cli.run.assert_not_called()
    alerts.fatal.assert_awaited_once_with(config, "admin", error)


def test_main_keyboard_interrupt_is_clean(cli, alerts):
    cli.run.side_effect = KeyboardInterrupt()
    assert admin_bot.main([]) == 0
    alerts.fatal.assert_not_awaited()


@pytest.mark.parametrize("stage", ["schema", "build", "poll"])
def test_main_fatal_alert_runs_after_database_and_loop_cleanup(
    config, monkeypatch, alerts, stage, caplog
):
    monkeypatch.setattr(admin_bot.Config, "load", Mock(return_value=config))
    monkeypatch.setattr(admin_bot, "setup_logging", Mock())
    monkeypatch.setattr(admin_bot, "process_log_path", Mock(return_value=config.log_path))
    connection = db.connect(config.db_path)
    monkeypatch.setattr(admin_bot.db, "connect", Mock(return_value=connection))
    error = RuntimeError(f"private startup detail {config.bot_token}")
    loops = []

    def poll(**kwargs):
        loops.append(asyncio.get_event_loop())
        raise error

    build = Mock(return_value=SimpleNamespace(run_polling=Mock(side_effect=poll)))
    monkeypatch.setattr(admin_bot.admin, "build_application", build)
    if stage == "schema":
        monkeypatch.setattr(admin_bot.db, "init_db", Mock(side_effect=error))
    elif stage == "build":
        build.side_effect = error

    async def check_cleanup(*args):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
        assert all(loop.is_closed() for loop in loops)
        assert asyncio.get_running_loop() not in loops

    alerts.fatal.side_effect = check_cleanup
    with caplog.at_level(logging.ERROR):
        assert admin_bot.main([]) == 1

    alerts.fatal.assert_awaited_once_with(config, "admin", error)
    assert config.bot_token not in caplog.text
    assert "private startup detail" not in caplog.text
    with pytest.raises(RuntimeError, match="no current event loop"):
        asyncio.get_event_loop()


def test_main_fatal_alert_uses_callers_running_loop(config, cli, alerts, monkeypatch):
    error = RuntimeError("offline failure inside caller loop")
    cli.run.side_effect = error

    async def scenario():
        loop = asyncio.get_running_loop()
        nested_run = Mock(side_effect=AssertionError("Do not nest asyncio.run"))
        monkeypatch.setattr(admin_bot.asyncio, "run", nested_run)
        assert admin_bot.main([]) == 1
        await asyncio.sleep(0)
        alerts.fatal.assert_awaited_once_with(config, "admin", error)
        nested_run.assert_not_called()
        assert asyncio.get_running_loop() is loop
        assert not loop.is_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("running_loop", [False, True])
def test_fatal_notification_scheduling_failure_is_best_effort(
    config, alerts, monkeypatch, caplog, running_loop
):
    error = RuntimeError("original offline failure")
    scheduling_error = RuntimeError(f"private alert detail {config.bot_token}")
    if running_loop:
        loop = SimpleNamespace(create_task=Mock(side_effect=scheduling_error))
        monkeypatch.setattr(admin_bot.asyncio, "get_running_loop", Mock(return_value=loop))
    else:
        monkeypatch.setattr(admin_bot.asyncio, "get_running_loop", Mock(side_effect=RuntimeError))
        monkeypatch.setattr(admin_bot.asyncio, "run", Mock(side_effect=scheduling_error))

    with caplog.at_level(logging.WARNING):
        admin_bot._notify_fatal(config, error)

    alerts.fatal.assert_called_once_with(config, "admin", error)
    alerts.fatal.assert_not_awaited()
    assert "Admin error notification unavailable (RuntimeError)" in caplog.text
    assert "private alert detail" not in caplog.text
    assert config.bot_token not in caplog.text


def test_fatal_notification_execution_failure_does_not_replace_exit_code(
    config, cli, alerts, caplog
):
    error = RuntimeError("original offline failure")
    cli.run.side_effect = error
    alerts.fatal.side_effect = OSError(f"private alert detail {config.bot_token}")

    with caplog.at_level(logging.WARNING):
        assert admin_bot.main([]) == 1

    alerts.fatal.assert_awaited_once_with(config, "admin", error)
    assert "Admin error notification unavailable (OSError)" in caplog.text
    assert config.bot_token not in caplog.text
    assert "private alert detail" not in caplog.text

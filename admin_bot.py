"""Run the owner-only Bosla Deals admin bot: python admin_bot.py [--env PATH]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app import admin, db
from app.config import Config, ConfigError
from app.logging_setup import process_log_path, setup_logging
from app.runtime import notify_error

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bosla Deals owner-only admin bot")
    parser.add_argument("--env", default=".env", help="path to .env file")
    return parser.parse_args(argv)


def run(config: Config) -> None:
    """Own the DB and event loop; PTB owns bot startup/shutdown and signals."""
    admin.require_admin_config(config)
    conn = db.connect(config.db_path)
    try:
        # Keep ownership even when schema initialization or PTB setup fails.
        db.init_db(conn)
        application = admin.build_application(config, conn)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            # Leave PTB's SIGINT/SIGTERM handling and shutdown defaults intact.
            application.run_polling(
                allowed_updates=["message"], bootstrap_retries=0, close_loop=False
            )
        finally:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)
    finally:
        conn.close()


def _notify_fatal(config: Config, exc: BaseException) -> None:
    """Use a fresh loop after PTB cleanup, or schedule on a caller's running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    notification = notify_error(config, "admin", exc)
    try:
        if loop is None:
            asyncio.run(notification)
        else:
            loop.create_task(notification)
    except Exception as alert_error:
        notification.close()
        log.warning("Admin error notification unavailable (%s)", type(alert_error).__name__)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = Config.load(args.env)
        admin.require_admin_config(config)
    except ConfigError as exc:
        print(
            f"Config error ({type(exc).__name__}). Check BOT_TOKEN, TARGET_CHANNELS "
            "and OWNER_ID; OWNER_ID must be a positive numeric Telegram user ID.",
            file=sys.stderr,
        )
        return 1

    try:
        setup_logging(
            process_log_path(config.log_path, "admin"),
            config.log_level,
            secrets=(config.bot_token, config.tg_api_hash, config.amazon_credential_secret),
        )
        run(config)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log.error(
            "Admin bot stopped after a startup or polling failure (%s)",
            type(exc).__name__,
        )
        _notify_fatal(config, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

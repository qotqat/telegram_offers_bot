"""Owner-only private-chat controls using the shared publication state.

``build_application`` borrows its SQLite connection; the caller owns cleanup.
No handlers stop ingestion or manage the other processes.
"""

from __future__ import annotations

import logging
import sqlite3

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes

from . import db
from .config import Config, ConfigError
from .models import Stats
from .runtime import notify_error

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MAX_STATS_SOURCES = 20
MAX_SOURCE_LENGTH = 120
ACCESS_DENIED = "Access denied. Admin commands require the owner in a private chat."
HELP_TEXT = (
    "Bosla Deals admin controls\n"
    "/pause - Pause publication; Telegram ingestion continues.\n"
    "/resume - Resume publication of queued offers.\n"
    "/stats - Today's UTC counts, pending queue, sources and pause state.\n"
    "/start - Show this help.\n\n"
    "Owner-only commands; use a private chat with this bot."
)


def require_admin_config(config: Config) -> None:
    """Bot requirements plus a numeric Telegram user ID, never a username."""
    config.require_bot()
    if type(config.owner_id) is not int or config.owner_id <= 0:
        raise ConfigError("OWNER_ID must be a positive numeric Telegram user ID, not a username")


def clip_text(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Bound UTF-16 code units without splitting a non-BMP character."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def format_stats(stats: Stats) -> str:
    lines = [
        "Stats - today (UTC)",
        f"Collected: {stats.collected_today}",
        f"Rejected: {stats.rejected_today}",
        f"Posted deliveries: {stats.posted_today}",
        f"Pending offers (all time): {stats.pending}",
        f"Publication paused: {'yes' if stats.paused else 'no'}",
        "",
        "Per source (collected today, UTC):",
    ]
    sources = sorted(stats.per_source.items(), key=lambda item: (-item[1], item[0]))
    for source, count in sources[:MAX_STATS_SOURCES]:
        label = clip_text(" ".join(source.split()), MAX_SOURCE_LENGTH)
        lines.append(f"{label or '(unknown)'}: {count}")
    if not sources:
        lines.append("(none)")
    omitted = len(sources) - MAX_STATS_SOURCES
    if omitted > 0:
        lines.append(f"… {omitted} more source(s) omitted.")
    return clip_text("\n".join(lines))


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            clip_text(text), parse_mode=None, disable_web_page_preview=True
        )


class AdminHandlers:
    """Testable callbacks; authorization precedes every DB access."""

    def __init__(self, config: Config, conn: sqlite3.Connection) -> None:
        self.config = config
        self.conn = conn

    async def _authorize(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        user_id = getattr(user, "id", None)
        owner_id = self.config.owner_id
        allowed = (
            type(owner_id) is int
            and owner_id > 0
            and type(user_id) is int
            and user_id == owner_id
            and chat is not None
            and chat.type == ChatType.PRIVATE
            and update.effective_message is not None
        )
        if not allowed:
            await _reply(update, ACCESS_DENIED)
        return allowed

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorize(update):
            await _reply(update, HELP_TEXT)

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorize(update):
            db.set_paused(self.conn, True)
            await _reply(update, "Publication paused. Telegram ingestion continues.")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorize(update):
            db.set_paused(self.conn, False)
            await _reply(update, "Publication resumed. Queued offers can be published.")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self._authorize(update):
            await _reply(update, format_stats(db.get_stats(self.conn)))

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Neither the update nor exception text is safe to send to an arbitrary chat.
        error = context.error
        if error is not None:
            log.error("Admin bot update/polling failed (%s)", type(error).__name__)
            await notify_error(self.config, "admin", error, bot=context.bot)


def build_application(config: Config, conn: sqlite3.Connection) -> Application:
    """Build offline; initialization and polling belong to the caller."""
    require_admin_config(config)
    assert config.bot_token is not None
    handlers = AdminHandlers(config, conn)
    application = (
        Application.builder()
        .token(config.bot_token)
        .concurrent_updates(False)
        .build()
    )
    for command in ("start", "pause", "resume", "stats"):
        application.add_handler(CommandHandler(command, getattr(handlers, command)))
    application.add_error_handler(handlers.on_error)
    return application

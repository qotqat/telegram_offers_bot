"""Bounded owner alerts and signal-driven cleanup for asynchronous workers."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import TypeVar

from telegram import Bot

from .config import Config

log = logging.getLogger(__name__)
T = TypeVar("T")
_ALERT_TIMES: dict[tuple[int, str, str], float] = {}
ALERT_COOLDOWN_SECONDS = 300
ALERT_TIMEOUT_SECONDS = 10


async def notify_error(
    config: Config, component: str, exc: BaseException, *, bot=None
) -> None:
    """Best effort, throttled DM; never send exception text or credentials."""
    if not config.bot_token or type(config.owner_id) is not int or config.owner_id <= 0:
        log.warning("Owner alert unavailable: set BOT_TOKEN and a positive numeric OWNER_ID")
        return
    key = (config.owner_id, component, type(exc).__name__)
    now = time.monotonic()
    previous = _ALERT_TIMES.get(key)
    if previous is not None and now - previous < ALERT_COOLDOWN_SECONDS:
        return
    if len(_ALERT_TIMES) > 256:
        _ALERT_TIMES.clear()
    # Throttle failures too, so an unavailable owner DM cannot delay every post.
    _ALERT_TIMES[key] = now
    text = (
        f"Bosla Deals: {component} error ({type(exc).__name__}).\n"
        "Check this process's local logs. The worker may have stopped or will retry."
    )
    try:
        async with asyncio.timeout(ALERT_TIMEOUT_SECONDS):
            if bot is not None:
                await bot.send_message(chat_id=config.owner_id, text=text, parse_mode=None)
            else:
                async with Bot(config.bot_token) as notifier:
                    await notifier.send_message(
                        chat_id=config.owner_id, text=text, parse_mode=None
                    )
    except Exception as alert_error:
        log.warning("Owner alert failed (%s)", type(alert_error).__name__)


@contextmanager
def termination_handler(callback: Callable[[], None]):
    """Map SIGTERM to orderly cleanup; restore the embedding process's handler."""
    previous = signal.getsignal(signal.SIGTERM)
    installed = False
    try:
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: callback())
            installed = True
        except (ValueError, OSError):
            # Signals can only be installed from the main interpreter thread.
            log.warning("SIGTERM handling unavailable outside the main thread")
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)


async def run_worker(
    config: Config, component: str, work: Callable[[], Awaitable[T]], *, alerts: bool = True
) -> T:
    """Cancel on SIGTERM so worker finally blocks run before asyncio exits."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    def cancel() -> None:
        loop.call_soon_threadsafe(task.cancel)

    with termination_handler(cancel):
        try:
            return await work()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("%s worker failed", component)
            if alerts:
                await notify_error(config, component, exc)
            raise

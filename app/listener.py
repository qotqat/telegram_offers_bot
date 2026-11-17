"""Telethon listener: ingest deals from source channels into SQLite.

Run via `python listener.py` (see that file for CLI flags). The first run asks
for the listener account's phone number and login code; the session file then
persists so later starts are non-interactive.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from pathlib import Path
from typing import Iterable, Optional

from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.utils import get_peer_id

from . import db
from .config import Config
from .ingest import BuildFailure, build_offer
from .models import Offer

log = logging.getLogger(__name__)

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def collect_urls(message) -> list[str]:
    """URLs from text entities, caption entities, inline buttons and raw text."""
    urls: list[str] = []

    try:
        entity_pairs = message.get_entities_text()
    except (AttributeError, TypeError):
        entity_pairs = []

    for entity, entity_text in entity_pairs:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            urls.append(entity.url)
        elif isinstance(entity, MessageEntityUrl):
            urls.append((entity_text or "").strip())

    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if not url:
                url = getattr(getattr(button, "type", None), "url", None)
            if url:
                urls.append(url)

    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    urls.extend(_URL_IN_TEXT_RE.findall(text))

    cleaned: list[str] = []
    for url in urls:
        url = (url or "").strip().rstrip(".,;:!?)]}\u00bb\u201d'\"")
        if url and url not in cleaned:
            cleaned.append(url)
    return cleaned


def offer_from_message(message, channel_label: str) -> Offer:
    """Build an Offer from a Telethon message; raises BuildFailure."""
    msg_id = getattr(message, "id", None)
    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    return build_offer(
        text,
        collect_urls(message),
        source_channel=channel_label,
        source_msg_id=int(msg_id) if msg_id is not None else None,
    )


async def download_photo(
    client: TelegramClient,
    message,
    media_dir: Path,
    max_bytes: int,
    content_hash: str,
) -> Optional[str]:
    """Download the first/largest photo, capped by max_bytes. Never raises."""
    file_info = getattr(message, "file", None)
    size = getattr(file_info, "size", None)
    if size and size > max_bytes:
        log.info("image skipped (%s bytes > limit %s)", size, max_bytes)
        return None

    media_dir.mkdir(parents=True, exist_ok=True)
    path = media_dir / f"{content_hash}.jpg"
    if path.exists():
        return str(path)

    try:
        result = await client.download_media(message, file=str(path))
    except Exception as exc:  # noqa: BLE001 - media is best-effort
        log.warning("image download failed: %s", exc)
        return None
    if not result:
        return None

    try:
        if path.stat().st_size > max_bytes:
            path.unlink(missing_ok=True)
            log.info("image skipped after download (larger than %s bytes)", max_bytes)
            return None
    except OSError:  # pragma: no cover - filesystem edge
        return None
    return str(path)


async def process_message(
    client: TelegramClient,
    message,
    channel_label: str,
    *,
    conn,
    config: Config,
) -> Optional[int]:
    """Normalize one post and persist it. Returns the offer id if new."""
    msg_id = getattr(message, "id", None)
    if msg_id is not None and db.offer_exists_by_source(conn, channel_label, int(msg_id)):
        log.debug("already seen %s/%s", channel_label, msg_id)
        return None

    try:
        offer = offer_from_message(message, channel_label)
    except BuildFailure as exc:
        log.info("skip %s/%s: %s", channel_label, msg_id, exc)
        return None
    except ValueError as exc:
        log.warning("invalid offer %s/%s: %s", channel_label, msg_id, exc)
        return None

    if getattr(message, "photo", None):
        image_path = await download_photo(
            client, message, config.media_dir, config.max_image_bytes, offer.content_hash
        )
        if image_path:
            offer.image_path = image_path

    offer_id = db.insert_offer(conn, offer)
    if offer_id is None:
        log.info("duplicate skipped: %s", offer.title)
        return None

    log.info(
        "stored #%d [%s] %s | %s %s | -%s%%",
        offer_id,
        channel_label,
        offer.title,
        offer.price if offer.price is not None else "?",
        offer.currency,
        offer.discount_pct if offer.discount_pct is not None else "?",
    )
    return offer_id


async def resolve_sources(
    client: TelegramClient, usernames: Iterable[str]
) -> list[tuple[str, object]]:
    resolved: list[tuple[str, object]] = []
    for name in usernames:
        try:
            entity = await client.get_entity(name)
        except Exception as exc:  # noqa: BLE001 - report and continue
            log.error(
                "source %s unresolved (%s) — join it with the listener account", name, exc
            )
            continue
        label = name if name.startswith("@") else f"@{name}"
        resolved.append((label, entity))
    return resolved


async def catch_up(
    client: TelegramClient,
    conn,
    config: Config,
    sources: list[tuple[str, object]],
    *,
    limit: Optional[int] = None,
) -> int:
    """Read the last N messages per source, oldest first."""
    cap = config.catchup_limit if limit is None else limit
    if cap <= 0:
        return 0

    total = 0
    for label, entity in sources:
        try:
            messages = await client.get_messages(entity, limit=cap)
        except Exception as exc:  # noqa: BLE001 - one source must not stop others
            log.error("catch-up failed for %s: %s", label, exc)
            continue

        stored = 0
        for message in reversed(messages):
            try:
                if await process_message(
                    client, message, label, conn=conn, config=config
                ):
                    stored += 1
            except Exception as exc:  # noqa: BLE001 - bad post cannot kill catch-up
                log.exception("failed to process %s/%s: %s", label, getattr(message, "id", "?"), exc)
        total += stored
        log.info("catch-up %s: %d message(s), %d new offer(s)", label, len(messages), stored)
        if config.catchup_delay_seconds:
            await asyncio.sleep(config.catchup_delay_seconds)
    return total


def _labels_by_chat(sources: list[tuple[str, object]]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for label, entity in sources:
        try:
            labels[get_peer_id(entity)] = label
        except Exception:  # noqa: BLE001 - label fallback handles this
            continue
    return labels


async def on_new_message(
    event,
    *,
    conn,
    config: Config,
    labels: dict[int, str],
) -> None:
    chat_id = getattr(event, "chat_id", None)
    label = labels.get(chat_id)
    if label is None:
        chat = getattr(event, "chat", None)
        username = getattr(chat, "username", None)
        label = f"@{username}" if username else str(chat_id)
    try:
        await process_message(
            event.client, event.message, label, conn=conn, config=config
        )
    except Exception as exc:  # noqa: BLE001 - keep listening after any bad post
        log.exception("live message %s/%s failed: %s", label, getattr(event.message, "id", "?"), exc)


async def run(
    config: Config,
    *,
    catchup: bool = True,
    once: bool = False,
    limit: Optional[int] = None,
) -> None:
    config.require_listener()
    session_path = Path(config.tg_session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    conn = db.init_db(config.db_path)
    client = TelegramClient(
        str(session_path),
        config.tg_api_id,
        config.tg_api_hash,
        connection_retries=None,
        retry_delay=5,
        auto_reconnect=True,
        request_retries=5,
    )

    try:
        await client.start()
        me = await client.get_me()
        log.info(
            "listener account: @%s (id=%s)",
            getattr(me, "username", None) or "unknown",
            getattr(me, "id", "?"),
        )

        sources = await resolve_sources(client, config.source_channels)
        if not sources:
            raise RuntimeError(
                "no source channels could be resolved — join them with the listener account"
            )
        log.info("resolved %d/%d source channels", len(sources), len(config.source_channels))

        if catchup:
            stored = await catch_up(client, conn, config, sources, limit=limit)
            log.info("catch-up finished: %d new offer(s)", stored)
        if once:
            log.info("--once requested; exiting after catch-up")
            return

        labels = _labels_by_chat(sources)
        handler = functools.partial(
            on_new_message, conn=conn, config=config, labels=labels
        )
        client.add_event_handler(
            handler, events.NewMessage(chats=[entity for _, entity in sources])
        )
        log.info("listening on %d channel(s) — Ctrl+C to stop", len(labels))
        await client.run_until_disconnected()
    finally:
        await client.disconnect()
        conn.close()
        log.info("listener stopped")

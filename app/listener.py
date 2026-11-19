"""Telethon listener: ingest deals from source channels into SQLite.

Run via `python listener.py` (see that file for CLI flags). The first run asks
for the listener account's phone number and login code; the session file then
persists so later starts are non-interactive.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from contextlib import nullcontext

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.utils import get_inner_text, get_peer_id

from . import db, filters
from .config import Config
from .ingest import BuildFailure, build_offer, pick_deal_url, replace_title
from .links import (
    affiliate_link, build_post_text, extract_asin, has_owned_amazon_links,
    has_plain_amazon_links, is_amazon_link, post_urls, resolve_foreign_amazon_links
)
from .products import ProductLookupError, fetch_product_title
from .runtime import notify_error

log = logging.getLogger(__name__)


Resolver = Callable[[str], Optional[str]]


@dataclass(slots=True)
class Source:
    """A resolved source channel plus the names used for brand replacement."""

    label: str
    entity: object
    names: tuple[str, ...] = ()
    usernames: tuple[str, ...] = ()


def collect_urls(message) -> list[str]:
    """URLs from text entities, caption entities, inline buttons and raw text."""
    urls: list[str] = []

    try:
        entity_pairs = list(message.get_entities_text())
    except (AttributeError, TypeError):
        entity_pairs = []

    # Captioned media keeps its entities in `media.caption_entities`, not in
    # `message.entities`; hidden links there are easy to miss.
    caption = getattr(message, "message", None) or ""
    caption_entities = getattr(getattr(message, "media", None), "caption_entities", None)
    if caption and caption_entities:
        try:
            entity_pairs.extend(
                zip(caption_entities, get_inner_text(caption, caption_entities))
            )
        except (AttributeError, TypeError):  # pragma: no cover - malformed entity
            pass

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
    urls.extend(post_urls(text))

    cleaned: list[str] = []
    for url in urls:
        url = (url or "").strip().rstrip(".,;:!?)]}\u00bb\u201d'\"")
        if url and url not in cleaned:
            cleaned.append(url)
    return cleaned


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
    source_names: Iterable[str] = (),
    source_usernames: Iterable[str] = (),
    resolver: Optional[Resolver] = None,
    title_fetcher: Optional[Callable[[str], str]] = None,
    lock: Optional[asyncio.Lock] = None,
) -> Optional[int]:
    """Serialize startup, polling and live ingestion before checking dedupe."""
    async with lock if lock is not None else nullcontext():
        return await _process_message(
            client, message, channel_label, conn=conn, config=config,
            source_names=source_names, source_usernames=source_usernames,
            resolver=resolver, title_fetcher=title_fetcher,
        )


async def _process_message(
    client: TelegramClient,
    message,
    channel_label: str,
    *,
    conn,
    config: Config,
    source_names: Iterable[str],
    source_usernames: Iterable[str],
    resolver: Optional[Resolver],
    title_fetcher: Optional[Callable[[str], str]],
) -> Optional[int]:
    """Normalize one post and persist it. Returns the offer id if new.

    With ``REQUIRE_AMAZON_LINK=1`` (the default) posts without an Amazon link
    are ignored. Every Amazon link in a copied post is replaced with our
    affiliate link, and source channel names/usernames are rebranded.
    """
    msg_id = getattr(message, "id", None)
    if msg_id is not None and db.offer_exists_by_source(conn, channel_label, int(msg_id)):
        log.debug("already seen %s/%s", channel_label, msg_id)
        return None

    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    urls = collect_urls(message)
    amazon_urls = [url for url in urls if is_amazon_link(url)]

    # A source may post a third-party redirect (for example 3rrood.com) that
    # lands on Amazon with the source's affiliate tag. Follow those links
    # before deciding the post has no Amazon link.
    foreign_links: dict[str, str] = {}
    if config.require_amazon_link and urls:
        foreign_links = await asyncio.to_thread(
            resolve_foreign_amazon_links,
            urls,
            resolver=resolver,
        )
        if not amazon_urls:
            amazon_urls = list(foreign_links.values())

    if config.require_amazon_link and not amazon_urls:
        log.info("skip %s/%s: no Amazon link", channel_label, msg_id)
        return None

    candidate_urls = amazon_urls if amazon_urls else urls
    product_title = None
    post_text = None
    if amazon_urls:
        config.require_amazon_links()
        partner_tag = config.amazon_associate_tag if config.amazon_link_mode == "affiliate" else ""
        rewritten = await asyncio.to_thread(
            affiliate_link, pick_deal_url(amazon_urls) or "",
            marketplace=config.amazon_marketplace, partner_tag=partner_tag or "",
            resolver=resolver,
        )
        if rewritten is None:
            log.warning("skip %s/%s: Amazon link could not be resolved/rebuilt", channel_label, msg_id)
            return None
        if not extract_asin(rewritten):
            log.info("skip %s/%s: Amazon URL is not an individual product (no ASIN)", channel_label, msg_id)
            return None
        product_url = f"https://{config.amazon_marketplace}/dp/{extract_asin(rewritten)}"
        try:
            product_title = await asyncio.to_thread(title_fetcher or fetch_product_title, product_url)
            if not isinstance(product_title, str) or not product_title.strip():
                raise ProductLookupError("Amazon returned an empty product title")
            product_title = " ".join(product_title.split())
        except ProductLookupError as exc:
            log.warning("skip %s/%s: Amazon title lookup unavailable: %s", channel_label, msg_id, exc)
            return None
        post = await asyncio.to_thread(
            build_post_text, text,
            primary_link=rewritten, source_names=source_names,
            source_usernames=source_usernames, marketplace=config.amazon_marketplace,
            partner_tag=partner_tag or "", brand_name=config.brand_name,
            brand_username=config.brand_username, brand_url=config.brand_url,
            resolver=resolver,
            foreign_links=foreign_links,
        )
        if post.failed:
            log.warning("skip %s/%s: %d Amazon link(s) could not be rewritten",
                        channel_label, msg_id, len(post.failed))
            return None
        post_text = replace_title(post.text, product_title, post_urls(post.text))
        if rewritten not in post_text:
            post_text = f"{post_text}\n\n{rewritten}"
        valid_links = (
            has_plain_amazon_links(post_text, rewritten, marketplace=config.amazon_marketplace)
            if config.amazon_link_mode == "plain" else has_owned_amazon_links(
                post_text, rewritten, marketplace=config.amazon_marketplace,
                partner_tag=partner_tag or "",
            )
        )
        if not valid_links:
            log.warning("skip %s/%s: unsafe secondary Amazon link", channel_label, msg_id)
            return None
        candidate_urls = [rewritten]

    try:
        offer = build_offer(
            text, candidate_urls, source_channel=channel_label,
            source_msg_id=int(msg_id) if msg_id is not None else None,
            title=product_title,
        )
    except BuildFailure as exc:
        log.info("skip %s/%s: %s", channel_label, msg_id, exc)
        return None
    except ValueError as exc:
        log.warning("invalid offer %s/%s: %s", channel_label, msg_id, exc)
        return None
    offer.post_text = post_text

    decision = filters.evaluate_offer(conn, offer, config)

    # Only accepted offers need media; rejected rows are stored text-only.
    if decision.accepted and getattr(message, "photo", None):
        image_path = await download_photo(
            client, message, config.media_dir, config.max_image_bytes, offer.content_hash
        )
        if image_path:
            offer.image_path = image_path

    offer_id = filters.save_offer(conn, offer, decision)
    if offer_id is None:
        log.info("duplicate skipped: %s", offer.title)
        return None

    if not decision.accepted:
        log.info(
            "rejected #%d [%s] %s: %s",
            offer_id,
            channel_label,
            offer.title,
            decision.detail,
        )
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
) -> list[Source]:
    resolved: list[Source] = []
    for name in usernames:
        try:
            entity = await client.get_entity(name)
        except Exception as exc:  # noqa: BLE001 - report and continue
            log.error(
                "source %s unresolved (%s) — join it with the listener account", name, exc
            )
            continue
        label = name if name.startswith("@") else f"@{name}"
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
        username = (getattr(entity, "username", None) or "").lstrip("@")
        configured = label.lstrip("@")
        names = tuple(dict.fromkeys(item.strip() for item in (title,) if item and item.strip()))
        usernames_seen = tuple(
            dict.fromkeys(
                item
                for item in (username, configured)
                if item
            )
        )
        resolved.append(
            Source(
                label=label,
                entity=entity,
                names=names,
                usernames=usernames_seen,
            )
        )
    return resolved


async def catch_up(
    client: TelegramClient,
    conn,
    config: Config,
    sources: list[Source],
    *,
    limit: Optional[int] = None,
    lock: Optional[asyncio.Lock] = None,
) -> int:
    """Check each source's newest message; backfill is explicitly opt-in."""
    cap = config.catchup_limit if limit is None else limit
    if cap <= 0:
        return 0
    if config.listener_latest_only:
        cap = 1

    total = 0
    for source in sources:
        try:
            messages = await client.get_messages(source.entity, limit=cap)
        except Exception as exc:  # noqa: BLE001 - one source must not stop others
            log.error("catch-up failed for %s: %s", source.label, exc)
            continue

        stored = 0
        for message in reversed(messages):
            try:
                if await process_message(
                    client,
                    message,
                    source.label,
                    conn=conn,
                    config=config,
                    source_names=source.names,
                    source_usernames=source.usernames,
                    lock=lock,
                ):
                    stored += 1
            except Exception as exc:  # noqa: BLE001 - bad post cannot kill catch-up
                log.exception(
                    "failed to process %s/%s: %s",
                    source.label,
                    getattr(message, "id", "?"),
                    exc,
                )
                await notify_error(config, "listener", exc)
        total += stored
        log.info(
            "catch-up %s: %d message(s), %d new offer(s)",
            source.label,
            len(messages),
            stored,
        )
        if config.catchup_delay_seconds:
            await asyncio.sleep(config.catchup_delay_seconds)
    return total


def _sources_by_chat(sources: list[Source]) -> dict[int, Source]:
    by_chat: dict[int, Source] = {}
    for source in sources:
        try:
            by_chat[get_peer_id(source.entity)] = source
        except Exception:  # noqa: BLE001 - fallback handles this
            continue
    return by_chat


async def on_new_message(
    event,
    *,
    conn,
    config: Config,
    sources: dict[int, Source],
    lock: Optional[asyncio.Lock] = None,
) -> None:
    chat_id = getattr(event, "chat_id", None)
    source = sources.get(chat_id)
    if source is None:
        chat = getattr(event, "chat", None)
        username = (getattr(chat, "username", None) or "").lstrip("@")
        label = f"@{username}" if username else str(chat_id)
        title = getattr(chat, "title", None)
        source = Source(
            label=label,
            entity=chat,
            names=(title,) if title else (),
            usernames=(username,) if username else (),
        )
    try:
        await process_message(
            event.client,
            event.message,
            source.label,
            conn=conn,
            config=config,
            source_names=source.names,
            source_usernames=source.usernames,
            lock=lock,
        )
    except Exception as exc:  # noqa: BLE001 - keep listening after any bad post
        log.exception(
            "live message %s/%s failed: %s",
            source.label,
            getattr(event.message, "id", "?"),
            exc,
        )
        await notify_error(config, "listener", exc)


async def poll_latest(client, conn, config: Config, sources: list[Source], *, lock) -> None:
    """Recover missed updates and retry unavailable titles without scanning history."""
    while True:
        await asyncio.sleep(config.source_poll_interval_seconds)
        await catch_up(client, conn, config, sources, limit=1, lock=lock)


async def run(
    config: Config,
    *,
    catchup: bool = True,
    once: bool = False,
    limit: Optional[int] = None,
    non_interactive: bool = False,
) -> None:
    config.require_listener()
    session_path = Path(config.tg_session_path)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    assert config.tg_api_id is not None and config.tg_api_hash is not None
    conn = db.init_db(config.db_path)
    client = None
    poller = None
    try:
        client = TelegramClient(
            str(session_path),
            config.tg_api_id,
            config.tg_api_hash,
            connection_retries=None,
            retry_delay=5,
            auto_reconnect=True,
            request_retries=5,
        )
        if non_interactive:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "listener session is not authorized; run listener.py --once "
                    "interactively on your trusted machine first"
                )
        else:
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

        lock = asyncio.Lock()
        if not once:
            by_chat = _sources_by_chat(sources)
            handler = functools.partial(
                on_new_message, conn=conn, config=config, sources=by_chat, lock=lock,
            )
            # Register before startup reads so posts arriving during lookup aren't lost.
            client.add_event_handler(
                handler, events.NewMessage(chats=[source.entity for source in sources])
            )
        if catchup:
            stored = await catch_up(client, conn, config, sources, limit=limit, lock=lock)
            log.info("catch-up finished: %d new offer(s)", stored)
        if once:
            log.info("--once requested; exiting after catch-up")
            return

        poller = asyncio.create_task(poll_latest(client, conn, config, sources, lock=lock))
        log.info("listening on %d channel(s), checking newest posts every %ss",
                 len(sources), config.source_poll_interval_seconds)
        await client.run_until_disconnected()
    finally:
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        try:
            if client is not None:
                await client.disconnect()
        finally:
            conn.close()
            log.info("listener stopped")

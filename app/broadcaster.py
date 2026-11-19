"""Phase 5 broadcaster: post accepted offers to the target channel(s).

The listener stores accepted offers as ``pending``; this module turns them
into Telegram messages, records one delivery per (offer, channel), and honours
the per-channel hourly cap plus ``POST_INTERVAL_SECONDS`` between cycles.

Text comes from ``offers.post_text`` (copied source post with our affiliate
link and brand), or from a generated fallback for Amazon-poller rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

from telegram.error import RetryAfter, TelegramError

from . import db
from .config import CHANNEL_MODE_ROUTE, Config
from .links import fallback_text, has_owned_amazon_links, has_plain_amazon_links, truncate_post_text
from .models import StoredOffer
from .runtime import notify_error
from .utils import contains_arabic, hours_ago_iso, utf16_length

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024


@dataclass(slots=True)
class BroadcastResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    channels: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------- text
def text_for_offer(offer: StoredOffer) -> str:
    """Copied/branded text when present, otherwise a generated title/price."""
    text = (offer.post_text or fallback_text(offer)).strip()
    return text or offer.title


def clip_text(
    text: str,
    *,
    link: str = "",
    limit: int = MAX_MESSAGE_LENGTH,
) -> str:
    """Trim to Telegram's limit while keeping our affiliate link at the end."""
    text = (text or "").strip()
    if utf16_length(text) <= limit:
        return text
    footer = f"…\n{link}" if link else "…"
    if utf16_length(footer) > limit:
        footer = "…"
    head = truncate_post_text(text, limit - utf16_length(footer)).rstrip()
    return head + footer


def rule_matches(offer: StoredOffer, rule: str) -> bool:
    """Match a ROUTE_RULES value: ``*``, ``ar``, ``en`` or a category."""
    rule = (rule or "").strip().lower()
    if not rule:
        return False
    if rule == "*":
        return True

    body = text_for_offer(offer)
    if rule == "ar":
        return contains_arabic(body)
    if rule == "en":
        return not contains_arabic(body)

    haystack = " ".join(
        part
        for part in (offer.category or "", offer.title, body)
        if part
    ).lower()
    return rule in haystack


def route_targets(offer: StoredOffer, config: Config) -> list[str]:
    """Channels this offer may post to, in configured order."""
    if config.channel_mode != CHANNEL_MODE_ROUTE:
        return list(config.target_channels)

    matched: list[str] = []
    for channel, rule in config.route_rules:
        if channel in config.target_channels and channel not in matched:
            if rule_matches(offer, rule):
                matched.append(channel)
    if not matched and config.target_channels:
        matched.append(config.target_channels[0])
    return matched


# ------------------------------------------------------------------- sending
async def send_offer(bot, channel: str, offer: StoredOffer) -> Optional[int]:
    """Send one offer and return the Telegram message id.

    Photos are best-effort: if the image cannot be sent, the text still goes
    out as a normal message. Text longer than a caption is sent separately.
    """
    text = clip_text(text_for_offer(offer), link=offer.link)
    caption = text if utf16_length(text) <= MAX_CAPTION_LENGTH else None
    photo_sent = False

    if offer.image_path or offer.image_url:
        try:
            if offer.image_path and Path(offer.image_path).is_file():
                with open(offer.image_path, "rb") as handle:
                    message = await bot.send_photo(
                        chat_id=channel, photo=handle, caption=caption
                    )
            elif offer.image_url:
                message = await bot.send_photo(
                    chat_id=channel, photo=offer.image_url, caption=caption
                )
            else:
                message = None
            if message is not None:
                photo_sent = True
                if caption is not None:
                    return int(message.message_id)
        except RetryAfter:
            # A flood wait is not an image failure: do not immediately send text.
            raise
        except (TelegramError, OSError) as exc:
            log.warning("photo failed for offer #%d on %s: %s", offer.id, channel, exc)

    message = await bot.send_message(chat_id=channel, text=text)
    if not photo_sent:
        return int(message.message_id)
    # Photo without caption already carries the media; this text is the deal.
    return int(message.message_id)


async def deliver_offer(
    conn, bot, channel: str, offer: StoredOffer, *, config: Optional[Config] = None
) -> bool:
    """Send and record one delivery. Returns True on success."""
    delivery_id = db.ensure_delivery(conn, offer.id, channel)
    try:
        message_id = await send_offer(bot, channel, offer)
    except RetryAfter as exc:
        wait = exc.retry_after
        seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
        db.set_state(conn, "retry_after:" + channel, str(time.time() + max(1, seconds)))
        db.mark_delivery_failed(conn, delivery_id, "Telegram flood wait; retry scheduled")
        log.warning("%s flood wait: retry in %.0f seconds", channel, seconds)
        return False
    except TelegramError as exc:
        db.mark_delivery_failed(conn, delivery_id, type(exc).__name__)
        log.error(
            "post failed for offer #%d on %s: %s", offer.id, channel, exc
        )
        if config is not None:
            await notify_error(config, "broadcaster", exc, bot=bot)
        return False
    db.mark_delivery_sent(conn, delivery_id, message_id)
    log.info(
        "posted offer #%d to %s (message %s): %s",
        offer.id,
        channel,
        message_id,
        offer.title,
    )
    return True


# --------------------------------------------------------------------- cycle
def _safe_links(body: str, primary_link: str, config: Config) -> bool:
    if config.amazon_link_mode == "plain":
        return has_plain_amazon_links(body, primary_link, marketplace=config.amazon_marketplace)
    return has_owned_amazon_links(
        body, primary_link, marketplace=config.amazon_marketplace,
        partner_tag=config.amazon_associate_tag or "",
    )


async def broadcast_once(
    conn,
    config: Config,
    bot,
    *,
    limit_per_channel: int = 1,
    dry_run: bool = False,
) -> BroadcastResult:
    """One broadcast pass: at most ``limit_per_channel`` posts per channel."""
    result = BroadcastResult()
    if config.require_amazon_link and not dry_run:
        config.require_amazon_links()
    if db.is_paused(conn):
        log.info("broadcast skipped: bot is paused")
        return result

    for channel in config.target_channels:
        sent_here = 0
        cooldown = db.get_state(conn, "retry_after:" + channel)
        if cooldown and not dry_run:
            try:
                if float(cooldown) > time.time():
                    result.channels[channel] = 0
                    continue
            except ValueError:
                log.warning("invalid retry cooldown for %s; retrying", channel)
        for _ in range(max(1, limit_per_channel)):
            # /pause may arrive while another channel is awaiting Telegram.
            if db.is_paused(conn):
                break
            if not dry_run and db.count_sent_since(
                conn, hours_ago_iso(1), channel=channel
            ) >= config.max_posts_per_hour:
                log.info(
                    "%s reached MAX_POSTS_PER_HOUR (%d)",
                    channel,
                    config.max_posts_per_hour,
                )
                break

            offer = db.next_pending_for_channel(conn, channel)
            if offer is None:
                break

            targets = route_targets(offer, config)
            if channel not in targets:
                if not dry_run:
                    delivery_id = db.ensure_delivery(conn, offer.id, channel)
                    db.mark_delivery_skipped(conn, delivery_id, "route mismatch")
                    if db.all_channels_delivered(
                        conn, offer.id, config.target_channels
                    ):
                        db.mark_offer_posted(conn, offer.id)
                result.skipped += 1
                continue

            if dry_run:
                print(f"[DRY] {channel} <- offer #{offer.id}")
                print(text_for_offer(offer))
                print("-" * 60)
                sent_here += 1
                result.sent += 1
                break  # do not loop on the same pending offer

            queued_text = text_for_offer(offer)
            if config.require_amazon_link and not all(
                _safe_links(body, offer.link, config)
                for body in (queued_text, clip_text(queued_text, link=offer.link))
            ):
                delivery_id = db.ensure_delivery(conn, offer.id, channel)
                reason = "affiliate safety check failed" if config.amazon_link_mode == "affiliate" else "plain-link safety check failed"
                db.mark_delivery_failed(conn, delivery_id, reason)
                log.error(
                    "Blocked offer #%d on %s: queued links do not match the configured "
                    "Amazon link mode/marketplace. Review the queue before retrying.",
                    offer.id, channel,
                )
                result.failed += 1
                break

            if await deliver_offer(conn, bot, channel, offer, config=config):
                sent_here += 1
                result.sent += 1
                if db.all_channels_delivered(
                    conn, offer.id, config.target_channels
                ):
                    db.mark_offer_posted(conn, offer.id)
            else:
                result.failed += 1
                # Leave this and newer offers for the next pass; a broken
                # channel must not spin or block the other channels.
                break

        result.channels[channel] = sent_here
    return result


async def run_forever(config: Config, conn, bot) -> None:
    """Broadcast continuously, sleeping ``POST_INTERVAL_SECONDS`` per cycle."""
    delay = max(5, config.post_interval_seconds)
    while True:
        try:
            result = await broadcast_once(conn, config, bot)
            if result.sent or result.failed:
                log.info(
                    "broadcast pass: %d sent, %d skipped, %d failed",
                    result.sent,
                    result.skipped,
                    result.failed,
                )
        except Exception as exc:  # noqa: BLE001 - keep the broadcaster alive
            log.exception("broadcast pass failed")
            await notify_error(config, "broadcaster", exc, bot=bot)
        await asyncio.sleep(delay)


__all__ = [
    "BroadcastResult",
    "broadcast_once",
    "clip_text",
    "deliver_offer",
    "route_targets",
    "rule_matches",
    "run_forever",
    "send_offer",
    "text_for_offer",
]

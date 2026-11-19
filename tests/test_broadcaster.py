"""Broadcaster tests: routing, text clipping, sending and delivery state."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from app import broadcaster, db
from app.config import CHANNEL_MODE_ROUTE, Config
from app.models import SOURCE_TELEGRAM, Offer


class FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, str]] = []
        self.photos: list[tuple[str, str | None]] = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise TelegramError("send failed")
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.messages))

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        if self.fail:
            raise TelegramError("photo failed")
        if hasattr(photo, "read"):
            photo.read()
        self.photos.append((chat_id, caption))
        return SimpleNamespace(message_id=100 + len(self.photos))


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offers.db")
    yield connection
    connection.close()


@pytest.fixture()
def config() -> Config:
    return Config(
        target_channels=["@deals_test"],
        channel_mode="all",
        post_interval_seconds=30,
        max_posts_per_hour=10,
        amazon_link_mode="affiliate",
        amazon_associate_tag="ours-21",
        amazon_tag_confirmed=True,
        amazon_marketplace="www.amazon.eg",
    )


def make_offer(**overrides) -> Offer:
    data = {
        "title": "Sony WH-1000XM5 Headphones",
        "link": "https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21",
        "source_type": SOURCE_TELEGRAM,
        "content_hash": "hash-1",
        "price": 299.0,
        "currency": "USD",
        "source_channel": "@deals_me",
        "source_msg_id": 10,
        "post_text": "Sony XM5\nPrice: $299\nhttps://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21",
    }
    data.update(overrides)
    return Offer(**data)


def test_clip_text_keeps_the_link_visible() -> None:
    link = "https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21"
    text = "x" * 5000

    clipped = broadcaster.clip_text(text, link=link)

    assert len(clipped) <= broadcaster.MAX_MESSAGE_LENGTH
    assert clipped.endswith(link)


def test_clip_text_short_text_is_unchanged() -> None:
    assert broadcaster.clip_text("hello") == "hello"


def test_route_targets_all_mode(config) -> None:
    offer = make_offer()
    config.target_channels = ["@a", "@b"]
    assert broadcaster.route_targets(offer, config) == ["@a", "@b"]


def test_route_targets_matches_arabic_english_and_category(config) -> None:
    config.channel_mode = CHANNEL_MODE_ROUTE
    config.target_channels = ["@ar", "@en", "@tech", "@all"]
    config.route_rules = [
        ("@ar", "ar"),
        ("@en", "en"),
        ("@tech", "headphones"),
        ("@all", "*"),
    ]

    english = make_offer()
    assert broadcaster.route_targets(english, config) == ["@en", "@tech", "@all"]

    arabic = make_offer(
        title="سماعات سوني",
        post_text="خصم على سماعات سوني",
        category=None,
    )
    assert broadcaster.route_targets(arabic, config) == ["@ar", "@all"]


def test_route_targets_falls_back_to_first_channel(config) -> None:
    config.channel_mode = CHANNEL_MODE_ROUTE
    config.target_channels = ["@first", "@second"]
    config.route_rules = [("@second", "tablets")]

    assert broadcaster.route_targets(make_offer(), config) == ["@first"]


def test_broadcast_once_sends_and_marks_posted(conn, config) -> None:
    offer_id = db.insert_offer(conn, make_offer())
    bot = FakeBot()

    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))

    assert result.sent == 1
    assert result.failed == 0
    assert len(bot.messages) == 1
    assert "tag=ours-21" in bot.messages[0][1]
    stored = db.get_offer(conn, offer_id)
    assert stored.status == "posted"
    row = conn.execute("SELECT status, message_id FROM post_deliveries").fetchone()
    assert row["status"] == "sent"
    assert row["message_id"] == 1


def test_broadcast_once_uses_photo_caption(conn, config, tmp_path) -> None:
    image = tmp_path / "deal.jpg"
    image.write_bytes(b"jpeg")
    offer_id = db.insert_offer(conn, make_offer(image_path=str(image)))
    bot = FakeBot()

    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))

    assert result.sent == 1
    assert bot.photos == [("@deals_test", make_offer().post_text)]
    assert bot.messages == []
    assert db.get_offer(conn, offer_id).status == "posted"


def test_broadcast_once_sends_long_text_separately(conn, config, tmp_path) -> None:
    image = tmp_path / "deal.jpg"
    image.write_bytes(b"jpeg")
    long_text = "x" * (broadcaster.MAX_CAPTION_LENGTH + 50)
    db.insert_offer(conn, make_offer(post_text=long_text, image_path=str(image)))
    bot = FakeBot()

    asyncio.run(broadcaster.broadcast_once(conn, config, bot))

    assert bot.photos == [("@deals_test", None)]
    assert len(bot.messages) == 1
    assert bot.messages[0][1] == long_text


def test_broadcast_once_marks_failed_delivery(conn, config) -> None:
    offer_id = db.insert_offer(conn, make_offer())
    bot = FakeBot(fail=True)

    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))

    assert result.sent == 0
    assert result.failed == 1
    assert db.get_offer(conn, offer_id).status == "pending"
    row = conn.execute("SELECT status, error FROM post_deliveries").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "TelegramError"


def test_broadcast_once_respects_hourly_cap(conn, config) -> None:
    config.max_posts_per_hour = 1
    sent_offer = db.insert_offer(
        conn, make_offer(content_hash="sent-hash", source_msg_id=1)
    )
    delivery = db.ensure_delivery(conn, sent_offer, "@deals_test")
    db.mark_delivery_sent(conn, delivery, 1)

    pending_id = db.insert_offer(
        conn, make_offer(content_hash="pending-hash", source_msg_id=2)
    )
    result = asyncio.run(broadcaster.broadcast_once(conn, config, FakeBot()))

    assert result.sent == 0
    assert db.get_offer(conn, pending_id).status == "pending"


def test_broadcast_once_skips_non_matching_route(conn, config) -> None:
    config.channel_mode = CHANNEL_MODE_ROUTE
    config.target_channels = ["@first", "@second"]
    config.route_rules = [("@second", "*")]
    offer_id = db.insert_offer(conn, make_offer())
    bot = FakeBot()

    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))

    assert result.sent == 1
    assert len(bot.messages) == 1
    assert bot.messages[0][0] == "@second"
    statuses = {
        row["channel"]: row["status"]
        for row in conn.execute("SELECT channel, status FROM post_deliveries")
    }
    assert statuses == {"@first": "skipped", "@second": "sent"}
    assert db.get_offer(conn, offer_id).status == "posted"


def test_broadcast_once_does_nothing_when_paused(conn, config) -> None:
    db.insert_offer(conn, make_offer())
    db.set_paused(conn, True)

    result = asyncio.run(broadcaster.broadcast_once(conn, config, FakeBot()))

    assert result.sent == 0
    assert conn.execute("SELECT COUNT(*) FROM post_deliveries").fetchone()[0] == 0


def test_dry_run_does_not_write(conn, config, capsys) -> None:
    db.insert_offer(conn, make_offer())

    result = asyncio.run(
        broadcaster.broadcast_once(conn, config, FakeBot(), dry_run=True)
    )

    assert result.sent == 1
    assert "tag=ours-21" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM post_deliveries").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM offers").fetchone()["status"] == "pending"


def test_all_channels_delivered_with_expected_channels(conn) -> None:
    offer_id = db.insert_offer(conn, make_offer())
    delivery = db.ensure_delivery(conn, offer_id, "@a")
    db.mark_delivery_sent(conn, delivery, 1)

    assert db.all_channels_delivered(conn, offer_id, ["@a"]) is True
    assert db.all_channels_delivered(conn, offer_id, ["@a", "@b"]) is False

    other = db.ensure_delivery(conn, offer_id, "@b")
    db.mark_delivery_skipped(conn, other, "route mismatch")
    assert db.all_channels_delivered(conn, offer_id, ["@a", "@b"]) is True


def test_sqlite_rows_are_typed(conn) -> None:
    """Guard against a stale schema fixture slipping into these tests."""
    assert isinstance(conn, sqlite3.Connection)
    assert db.schema_version(conn) == db.SCHEMA_VERSION


@pytest.mark.parametrize("overrides", [
    {"link": "https://www.amazon.eg/dp/B09XS7JWHH?tag=someone-else-21"},
    {"link": "https://www.amazon.com/dp/B09XS7JWHH?tag=ours-21"},
    {"link": "https://amzn.to/unresolved"},
    {"post_text": "Deal https://www.amazon.eg/dp/B09XS7JWHH?tag=other-21"},
    {"post_text": "Deal https://amzn.to/old-link"},
    {"post_text": "Deal www.amazon.eg/dp/B09XS7JWHH?tag=other-21"},
    {"link": "https://www.amazon.eg:443@attacker.invalid/dp/B09XS7JWHH?tag=ours-21"},
])
def test_old_or_unrewritten_queue_entries_never_publish(conn, config, overrides):
    offer_id = db.insert_offer(conn, make_offer(**overrides))
    bot = FakeBot()
    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert result.failed == 1
    assert bot.messages == []
    assert bot.photos == []
    assert db.get_offer(conn, offer_id).status == "pending"
    assert conn.execute("SELECT error FROM post_deliveries").fetchone()[0] == "affiliate safety check failed"


def test_flood_wait_is_persistent_does_not_block_other_channel_and_retries_later(conn, config, monkeypatch):
    from telegram.error import RetryAfter

    clock = [10000.0]
    monkeypatch.setattr(broadcaster.time, "time", lambda: clock[0])
    config.target_channels = ["@slow", "@healthy"]
    offer_id = db.insert_offer(conn, make_offer())

    class FloodBot(FakeBot):
        attempts = 0

        async def send_message(self, chat_id, text, **kwargs):
            if chat_id == "@slow":
                self.attempts += 1
                if self.attempts == 1:
                    raise RetryAfter(60)
            return await super().send_message(chat_id, text, **kwargs)

    bot = FloodBot()
    first = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert (first.sent, first.failed) == (1, 1)
    assert db.get_offer(conn, offer_id).status == "pending"
    assert float(db.get_state(conn, "retry_after:@slow")) == 10060
    asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert bot.attempts == 1
    clock[0] += 61
    third = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert third.sent == 1
    assert bot.attempts == 2
    assert db.get_offer(conn, offer_id).status == "posted"
    assert len(bot.messages) == 2


def test_photo_flood_wait_does_not_fall_back_to_text(conn, config):
    from telegram.error import RetryAfter

    class PhotoFloodBot(FakeBot):
        async def send_photo(self, **kwargs):
            raise RetryAfter(60)

    bot = PhotoFloodBot()
    db.insert_offer(conn, make_offer(image_url="https://example.com/photo.jpg"))
    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert result.failed == 1
    assert bot.messages == []
    assert db.get_state(conn, "retry_after:@deals_test")


def test_pause_is_rechecked_between_posts_and_channels(conn, config):
    config.target_channels = ["@first", "@second"]
    db.insert_offer(conn, make_offer())
    db.insert_offer(conn, make_offer(content_hash="second"))

    class PauseBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            result = await super().send_message(chat_id, text, **kwargs)
            db.set_paused(conn, True)
            return result

    bot = PauseBot()
    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot, limit_per_channel=3))
    assert result.sent == 1
    assert len(bot.messages) == 1
    assert bot.messages[0][0] == "@first"


def test_clipping_counts_emoji_utf16_units_and_keeps_link():
    from app.utils import utf16_length

    link = "https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21"
    clipped = broadcaster.clip_text("🔥" * 3000, link=link)
    assert utf16_length(clipped) <= broadcaster.MAX_MESSAGE_LENGTH
    assert clipped.endswith(link)
    assert "�" not in clipped


def test_long_multilink_payload_never_loses_an_affiliate_query(conn, config):
    from app.links import has_owned_amazon_links

    primary = make_offer().link
    other = "https://www.amazon.eg/dp/B000000002?utm_source=" + "x" * 350 + "&tag=ours-21"
    text = "Deal " * 770 + other + "\n" + primary
    assert len(text) > broadcaster.MAX_MESSAGE_LENGTH
    db.insert_offer(conn, make_offer(post_text=text))
    bot = FakeBot()
    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert result.sent == 1
    sent_text = bot.messages[0][1]
    assert "B000000002" not in sent_text
    assert has_owned_amazon_links(sent_text, primary, marketplace="www.amazon.eg", partner_tag="ours-21")


def test_caption_limit_counts_emoji_as_two_units(conn, config):
    bot = FakeBot()
    text = "🔥" * 600
    db.insert_offer(conn, make_offer(post_text=text, image_url="https://example.com/photo.jpg"))
    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert result.sent == 1
    assert bot.photos == [("@deals_test", None)]
    assert bot.messages == [("@deals_test", text)]

"""Listener helper tests (no network, no real Telegram session)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from app import db, listener
from app.config import Config
from app.ingest import pick_deal_url
from app.listener import collect_urls, process_message


def fake_message(
    *,
    msg_id: int = 1,
    text: str = "",
    entities: tuple = (),
    markup=None,
    photo=None,
    file_size=None,
    media=None,
):
    return SimpleNamespace(
        id=msg_id,
        message=text,
        reply_markup=markup,
        photo=photo,
        media=media,
        file=SimpleNamespace(size=file_size) if file_size is not None else None,
        get_entities_text=lambda: list(entities),
    )


class FakeClient:
    def __init__(self, payload: bytes = b"jpeg-bytes") -> None:
        self.payload = payload
        self.downloads = 0

    async def download_media(self, message, file):
        self.downloads += 1
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return str(path)


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config(
        media_dir=tmp_path / "media",
        max_image_bytes=1024 * 1024,
        require_amazon_link=False,
    )


@pytest.fixture()
def amazon_config(tmp_path, monkeypatch) -> Config:
    monkeypatch.setattr(listener, "fetch_product_title", lambda url: "Amazon product title")
    return Config(
        media_dir=tmp_path / "media",
        max_image_bytes=1024 * 1024,
        require_amazon_link=True,
        amazon_link_mode="affiliate",
        amazon_associate_tag="ours-21",
        amazon_tag_confirmed=True,
        amazon_marketplace="www.amazon.eg",
        brand_name="Bosla Deals",
        brand_username="@deals_test",
        brand_url="https://t.me/deals_test",
    )


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offers.db")
    yield connection
    connection.close()


def test_ingests_and_rewrites_bare_amazon_link(conn, amazon_config):
    message = fake_message(text="Sony headphones\nwww.amazon.eg/dp/B0CH8DKDKN?tag=other-21")
    offer_id = asyncio.run(process_message(
        FakeClient(), message, "@deals_me", conn=conn, config=amazon_config,
    ))
    assert offer_id is not None
    stored = db.get_offer(conn, offer_id)
    assert stored is not None
    assert "tag=ours-21" in stored.post_text
    assert "other-21" not in stored.post_text


def test_collect_urls_from_entities_text_and_buttons() -> None:
    markup = SimpleNamespace(
        rows=[
            SimpleNamespace(
                buttons=[SimpleNamespace(type=SimpleNamespace(url="https://button.example/p/9"))]
            )
        ]
    )
    message = fake_message(
        text="Buy: https://raw.example/x then https://t.me/+invite",
        entities=(
            (MessageEntityUrl(offset=5, length=22), "https://raw.example/x"),
            (MessageEntityTextUrl(offset=0, length=3, url="https://hidden.example/y"), "Buy"),
        ),
        markup=markup,
    )

    urls = collect_urls(message)
    assert "https://raw.example/x" in urls
    assert "https://hidden.example/y" in urls
    assert "https://button.example/p/9" in urls
    assert urls.count("https://raw.example/x") == 1
    assert pick_deal_url(urls) != "https://t.me/+invite"


def test_collect_urls_from_caption_entities() -> None:
    message = fake_message(
        text="Buy now",
        media=SimpleNamespace(
            caption_entities=[
                MessageEntityTextUrl(
                    offset=0, length=3, url="https://www.amazon.eg/dp/B0CH8DKDKN"
                )
            ]
        ),
    )

    assert collect_urls(message) == ["https://www.amazon.eg/dp/B0CH8DKDKN"]


def test_process_message_stores_offer_and_photo(conn, config) -> None:
    message = fake_message(
        msg_id=101,
        text="🔥 Air Fryer 5.8QT\nPrice: $49.99 was $79.99\nhttps://shop.example.com/p/1",
        photo=True,
        file_size=2048,
    )
    client = FakeClient()

    offer_id = asyncio.run(
        process_message(client, message, "@chan", conn=conn, config=config)
    )

    assert offer_id is not None
    stored = db.get_offer(conn, offer_id)
    assert stored is not None
    assert stored.title == "Air Fryer 5.8QT"
    assert stored.price == 49.99
    assert stored.old_price == 79.99
    assert stored.image_path is not None
    assert Path(stored.image_path).is_file()
    assert client.downloads == 1


def test_process_message_skips_seen_and_duplicate_posts(conn, config) -> None:
    client = FakeClient()
    message = fake_message(
        msg_id=7,
        text="Sony XM5\nPrice: $299 was $399\nhttps://shop.example.com/sony",
    )

    first = asyncio.run(
        process_message(client, message, "@chan", conn=conn, config=config)
    )
    again = asyncio.run(
        process_message(client, message, "@chan", conn=conn, config=config)
    )
    # Same deal re-posted under a new message id: exact hash dedupe applies.
    repost = fake_message(
        msg_id=8,
        text="Sony XM5\nPrice: $299 was $399\nhttps://shop.example.com/sony",
    )
    duplicate = asyncio.run(
        process_message(client, repost, "@chan", conn=conn, config=config)
    )

    assert first is not None
    assert again is None
    assert duplicate is None
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1


def test_process_message_skips_oversized_photo(conn, config) -> None:
    message = fake_message(
        msg_id=3,
        text="Deal\nPrice: $10\nhttps://shop.example.com/x",
        photo=True,
        file_size=config.max_image_bytes + 1,
    )
    client = FakeClient()

    offer_id = asyncio.run(
        process_message(client, message, "@chan", conn=conn, config=config)
    )

    assert offer_id is not None
    assert client.downloads == 0
    assert db.get_offer(conn, offer_id).image_path is None


def test_process_message_without_link_is_skipped(conn, config) -> None:
    message = fake_message(msg_id=4, text="Just a text post with no links")
    assert (
        asyncio.run(
            process_message(FakeClient(), message, "@chan", conn=conn, config=config)
        )
        is None
    )


def test_process_message_rejects_low_discount_without_downloading(
    conn, config
) -> None:
    message = fake_message(
        msg_id=201,
        text=(
            "Weak deal\n"
            "Price: $100 was $105\n"
            "https://shop.example.com/weak"
        ),
        photo=True,
        file_size=1024,
    )
    client = FakeClient()

    offer_id = asyncio.run(
        process_message(client, message, "@chan", conn=conn, config=config)
    )

    assert offer_id is None
    assert client.downloads == 0
    row = conn.execute("SELECT status FROM offers").fetchone()
    assert row["status"] == "rejected"


def test_process_message_rejects_near_duplicate(conn, config) -> None:
    client = FakeClient()
    first = fake_message(
        msg_id=301,
        text=(
            "Sony WH-1000XM5 Headphones\n"
            "Price: $299 was $399\n"
            "https://shop.example.com/a"
        ),
    )
    repost = fake_message(
        msg_id=302,
        text=(
            "Sony WH-1000XM5 Headphones!\n"
            "Price: $305\n"
            "https://other.example.com/b"
        ),
    )

    first_id = asyncio.run(
        process_message(client, first, "@chan", conn=conn, config=config)
    )
    duplicate_id = asyncio.run(
        process_message(client, repost, "@chan", conn=conn, config=config)
    )

    assert first_id is not None
    assert duplicate_id is None
    statuses = [
        row["status"]
        for row in conn.execute("SELECT status FROM offers ORDER BY id")
    ]
    assert statuses == ["pending", "rejected"]


def test_amazon_post_is_copied_with_our_link_and_brand(conn, amazon_config) -> None:
    message = fake_message(
        msg_id=401,
        text=(
            "🔥 Sony WH-1000XM5 Headphones\n"
            "Price: $299 (was $399)\n"
            "https://www.amazon.com/dp/B09XS7JWHH?tag=other-20\n"
            "More deals: https://t.me/deals_me or @deals_me\n"
            "By Deals Me"
        ),
    )

    offer_id = asyncio.run(
        process_message(
            FakeClient(),
            message,
            "@deals_me",
            conn=conn,
            config=amazon_config,
            source_names=("Deals Me",),
            source_usernames=("deals_me",),
        )
    )

    assert offer_id is not None
    stored = db.get_offer(conn, offer_id)
    assert stored is not None
    assert stored.link == "https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21"
    assert stored.post_text is not None
    assert "tag=ours-21" in stored.post_text
    assert "other-20" not in stored.post_text
    assert "Bosla Deals" in stored.post_text
    assert "@deals_test" in stored.post_text
    assert "https://t.me/deals_test" in stored.post_text
    assert "@deals_me" not in stored.post_text
    assert "Amazon.com" not in stored.post_text


def test_non_amazon_post_is_ignored_when_amazon_required(conn, amazon_config) -> None:
    message = fake_message(
        msg_id=402,
        text="Air Fryer\nPrice: $49.99\nhttps://shop.example.com/p/1",
    )

    offer_id = asyncio.run(
        process_message(FakeClient(), message, "@chan", conn=conn, config=amazon_config)
    )

    assert offer_id is None
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_short_amazon_link_is_resolved_and_rewritten(conn, amazon_config) -> None:
    resolved = []

    def resolver(url: str) -> str:
        resolved.append(url)
        return (
            "https://www.amazon.eg/dp/B0CH8DKDKN?"
            "tag=their-21&linkCode=sl2&ascsubtag=abc"
        )

    message = fake_message(
        msg_id=403,
        text="Wireless Earbuds\nPrice: 999 EGP\nhttps://link.amazon/B0dpfzV4h",
    )

    offer_id = asyncio.run(
        process_message(
            FakeClient(),
            message,
            "@chan",
            conn=conn,
            config=amazon_config,
            resolver=resolver,
        )
    )

    assert resolved
    assert set(resolved) == {"https://link.amazon/B0dpfzV4h"}
    stored = db.get_offer(conn, offer_id)
    assert stored.link == "https://www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21"
    assert "https://link.amazon/B0dpfzV4h" not in stored.post_text
    assert "tag=ours-21" in stored.post_text


def test_amazon_button_link_is_appended_to_copied_text(conn, amazon_config) -> None:
    markup = SimpleNamespace(
        rows=[
            SimpleNamespace(
                buttons=[
                    SimpleNamespace(
                        type=SimpleNamespace(
                            url="https://www.amazon.com/dp/B000000001?tag=their-20"
                        )
                    )
                ]
            )
        ]
    )
    message = fake_message(msg_id=404, text="Air Fryer 5.8QT\n$49.99", markup=markup)

    offer_id = asyncio.run(
        process_message(FakeClient(), message, "@chan", conn=conn, config=amazon_config)
    )

    stored = db.get_offer(conn, offer_id)
    assert stored.post_text is not None
    assert "https://www.amazon.eg/dp/B000000001?tag=ours-21" in stored.post_text
    assert "their-20" not in stored.post_text


def test_unresolvable_short_link_is_skipped(conn, amazon_config) -> None:
    message = fake_message(
        msg_id=405,
        text="Deal\n$10\nhttps://link.amazon/B0dpfzV4h",
    )

    offer_id = asyncio.run(
        process_message(
            FakeClient(),
            message,
            "@chan",
            conn=conn,
            config=amazon_config,
            resolver=lambda url: None,
        )
    )

    assert offer_id is None
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_every_amazon_link_is_rewritten(conn, amazon_config) -> None:
    message = fake_message(
        msg_id=406,
        text=(
            "Two products\n"
            "First: https://www.amazon.com/dp/B000000002?tag=their-20\n"
            "Second: https://www.amazon.com/dp/B000000003?tag=their-21"
        ),
    )

    offer_id = asyncio.run(
        process_message(FakeClient(), message, "@chan", conn=conn, config=amazon_config)
    )

    stored = db.get_offer(conn, offer_id)
    assert "their-20" not in stored.post_text
    assert "their-21" not in stored.post_text
    assert stored.post_text.count("tag=ours-21") == 2

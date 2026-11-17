"""Listener helper tests (no network, no real Telegram session)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from app import db
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
):
    return SimpleNamespace(
        id=msg_id,
        message=text,
        reply_markup=markup,
        photo=photo,
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
    return Config(media_dir=tmp_path / "media", max_image_bytes=1024 * 1024)


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offers.db")
    yield connection
    connection.close()


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

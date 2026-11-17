"""Database layer tests: schema, dedupe, deliveries, state, stats."""

from __future__ import annotations

import pytest

from app import db
from app.models import (
    STATUS_PENDING,
    STATUS_REJECTED,
    Offer,
    SOURCE_TELEGRAM,
)
from app.utils import hours_ago_iso


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offers.db")
    yield connection
    connection.close()


def make_offer(**overrides) -> Offer:
    data = {
        "title": "Sony WH-1000XM5 Headphones",
        "link": "https://example.com/deal/1",
        "source_type": SOURCE_TELEGRAM,
        "content_hash": "hash-1",
        "price": 299.0,
        "old_price": 399.0,
        "discount_pct": 25.0,
        "source_channel": "@chan1",
        "source_msg_id": 10,
    }
    data.update(overrides)
    return Offer(**data)


def test_init_creates_schema_and_version(conn) -> None:
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert db.table_names(conn) == ["offers", "post_deliveries", "state"]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_is_idempotent(tmp_path) -> None:
    path = tmp_path / "db.sqlite"
    first = db.init_db(path)
    first.close()
    second = db.init_db(path)
    try:
        assert db.schema_version(second) == db.SCHEMA_VERSION
        assert db.get_state(second, "paused") == "0"
    finally:
        second.close()


def test_insert_and_exact_hash_dedupe(conn) -> None:
    offer_id = db.insert_offer(conn, make_offer())
    assert offer_id is not None

    duplicate = db.insert_offer(conn, make_offer(link="https://example.com/other"))
    assert duplicate is None
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1

    stored = db.get_offer(conn, offer_id)
    assert stored is not None
    assert stored.title == "Sony WH-1000XM5 Headphones"
    assert stored.price == 299.0
    assert stored.discount_pct == 25.0
    assert stored.status == STATUS_PENDING
    assert stored.created_at is not None


def test_recent_offers_newest_first(conn) -> None:
    for index in range(3):
        db.insert_offer(
            conn,
            make_offer(
                title=f"Deal {index}",
                content_hash=f"hash-{index}",
                source_msg_id=index,
            ),
        )

    recent = db.recent_offers(conn, limit=2)
    assert [offer.title for offer in recent] == ["Deal 2", "Deal 1"]


def test_recent_offers_can_exclude_rejected(conn) -> None:
    accepted = db.insert_offer(conn, make_offer())
    rejected = db.insert_offer(
        conn, make_offer(content_hash="hash-2", source_msg_id=2)
    )
    db.mark_offer_rejected(conn, rejected)

    assert [offer.id for offer in db.recent_offers(conn)] == [rejected, accepted]
    assert [
        offer.id for offer in db.recent_offers(conn, include_rejected=False)
    ] == [accepted]


def test_mark_posted_and_rejected(conn) -> None:
    offer_id = db.insert_offer(conn, make_offer())
    db.mark_offer_posted(conn, offer_id)
    stored = db.get_offer(conn, offer_id)
    assert stored.status == "posted"
    assert stored.posted_at is not None

    other_id = db.insert_offer(conn, make_offer(content_hash="hash-2"))
    db.mark_offer_rejected(conn, other_id)
    assert db.get_offer(conn, other_id).status == STATUS_REJECTED


def test_count_offers_since_and_per_source(conn) -> None:
    db.insert_offer(conn, make_offer())
    db.insert_offer(conn, make_offer(content_hash="hash-2", source_channel="@chan2"))
    db.insert_offer(conn, make_offer(content_hash="hash-3", source_channel="@chan2"))
    rejected = db.insert_offer(conn, make_offer(content_hash="hash-4"))
    db.mark_offer_rejected(conn, rejected)

    assert db.count_offers_since(conn, hours_ago_iso(1)) == 3
    assert db.count_offers_since(conn, hours_ago_iso(1), source_channel="@chan2") == 2
    assert db.count_offers_since(conn, hours_ago_iso(-1)) == 0


def test_delivery_lifecycle(conn) -> None:
    offer_id = db.insert_offer(conn, make_offer())

    first = db.ensure_delivery(conn, offer_id, "@deals_en")
    second = db.ensure_delivery(conn, offer_id, "@deals_en")
    assert first == second
    assert db.ensure_delivery(conn, offer_id, "@deals_ar") != first

    pending = db.next_pending_for_channel(conn, "@deals_en")
    assert pending is not None and pending.id == offer_id

    db.mark_delivery_sent(conn, first, message_id=555)
    assert db.all_channels_delivered(conn, offer_id) is False
    assert db.next_pending_for_channel(conn, "@deals_en") is None
    assert db.count_sent_since(conn, hours_ago_iso(1)) == 1
    assert db.count_sent_since(conn, hours_ago_iso(1), channel="@deals_ar") == 0

    ar_delivery = db.ensure_delivery(conn, offer_id, "@deals_ar")
    db.mark_delivery_failed(conn, ar_delivery, "boom")
    assert db.all_channels_delivered(conn, offer_id) is False
    # Failed deliveries stay eligible for a retry.
    assert db.next_pending_for_channel(conn, "@deals_ar").id == offer_id

    db.mark_delivery_sent(conn, ar_delivery, message_id=556)
    assert db.all_channels_delivered(conn, offer_id) is True


def test_pause_state_roundtrip(conn) -> None:
    assert db.is_paused(conn) is False
    db.set_paused(conn, True)
    assert db.is_paused(conn) is True
    db.set_paused(conn, False)
    assert db.is_paused(conn) is False


def test_stats(conn) -> None:
    offer_id = db.insert_offer(conn, make_offer())
    db.insert_offer(conn, make_offer(content_hash="hash-2", source_channel="@chan2"))
    db.insert_offer(conn, make_offer(content_hash="hash-3", source_channel="@chan2"))
    rejected = db.insert_offer(conn, make_offer(content_hash="hash-4"))
    db.mark_offer_rejected(conn, rejected)

    delivery = db.ensure_delivery(conn, offer_id, "@deals_en")
    db.mark_delivery_sent(conn, delivery, None)

    stats = db.get_stats(conn)
    assert stats.collected_today == 4
    assert stats.rejected_today == 1
    assert stats.posted_today == 1
    assert stats.pending == 3
    assert stats.per_source == {"@chan1": 2, "@chan2": 2}
    assert stats.paused is False


def test_offer_validation() -> None:
    with pytest.raises(ValueError, match="title"):
        make_offer(title="   ")
    with pytest.raises(ValueError, match="link"):
        make_offer(link="")
    with pytest.raises(ValueError, match="source_type"):
        make_offer(source_type="twitter")


def test_image_path_roundtrip_and_source_lookup(conn) -> None:
    offer_id = db.insert_offer(
        conn, make_offer(image_path="data/media/hash-1.jpg")
    )
    stored = db.get_offer(conn, offer_id)
    assert stored.image_path == "data/media/hash-1.jpg"
    assert db.offer_exists_by_source(conn, "@chan1", 10) is True
    assert db.offer_exists_by_source(conn, "@chan1", 11) is False


def test_migrates_v1_database(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL, old_price REAL, discount_pct REAL,
            currency TEXT DEFAULT 'USD',
            link TEXT NOT NULL,
            image_url TEXT,
            category TEXT,
            source_type TEXT NOT NULL,
            source_channel TEXT, source_msg_id INTEGER,
            content_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            posted_at TEXT
        );
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO state VALUES ('schema_version', '1');
        INSERT INTO offers (title, link, source_type, content_hash, created_at)
        VALUES ('Old offer', 'https://example.com/old', 'telegram', 'old-hash',
                '2026-01-01T00:00:00Z');
        """
    )
    raw.commit()
    raw.close()

    migrated = db.init_db(path)
    try:
        assert db.schema_version(migrated) == db.SCHEMA_VERSION
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(offers)")
        }
        assert "image_path" in columns
        assert migrated.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
    finally:
        migrated.close()


def test_refuses_newer_schema(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "newer.db"
    raw = sqlite3.connect(path)
    raw.executescript("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);")
    raw.execute("INSERT INTO state VALUES ('schema_version', '999')")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="newer than this code"):
        db.init_db(path)

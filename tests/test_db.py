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

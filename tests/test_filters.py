"""Filter pipeline tests: keywords, discount, fuzzy dedupe, source cap."""

from __future__ import annotations

import pytest

from app import db, filters
from app.config import Config
from app.filters import (
    REASON_EXCLUDED_KEYWORD,
    REASON_LOW_DISCOUNT,
    REASON_NEAR_DUPLICATE,
    REASON_SOURCE_CAP,
    FilterDecision,
    evaluate_offer,
    find_duplicate,
    normalize_title,
    same_price_bucket,
    save_offer,
)
from app.models import (
    SOURCE_AMAZON,
    SOURCE_TELEGRAM,
    STATUS_PENDING,
    STATUS_REJECTED,
    Offer,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "offers.db")
    yield connection
    connection.close()


def make_config(**overrides) -> Config:
    values = dict(
        min_discount_pct=20.0,
        exclude_keywords=["fake", "scam"],
        max_per_source_per_hour=3,
        fuzzy_threshold=88.0,
    )
    values.update(overrides)
    return Config(**values)


def make_offer(**overrides) -> Offer:
    data = dict(
        title="Sony WH-1000XM5 Headphones",
        link="https://shop.example.com/sony-xm5",
        source_type=SOURCE_TELEGRAM,
        content_hash="hash-base",
        price=299.0,
        old_price=399.0,
        discount_pct=25.1,
        source_channel="@chan1",
        source_msg_id=1,
    )
    data.update(overrides)
    return Offer(**data)


def store(conn, **overrides) -> int:
    offer = make_offer(**overrides)
    offer_id = db.insert_offer(conn, offer)
    assert offer_id is not None
    return offer_id


# --------------------------------------------------------------- normalization
def test_normalize_title_ignores_case_punctuation_and_arabic_noise() -> None:
    assert (
        normalize_title("  Sony WH-1000XM5 — Headphones!  ")
        == "sony wh 1000xm5 headphones"
    )
    assert normalize_title("خصم ٣٠٪ سماعات سوني") == "خصم 30 سماعات سوني"
    assert normalize_title("سُونَيّ") == "سوني"
    assert normalize_title("SALE__Sony   XM5") == "sale sony xm5"


def test_same_price_bucket_is_symmetric_and_handles_unknowns() -> None:
    assert same_price_bucket(100.0, 110.0) is True
    assert same_price_bucket(110.0, 100.0) is True
    assert same_price_bucket(100.0, 112.0) is False
    assert same_price_bucket(None, None) is True
    assert same_price_bucket(None, 10.0) is False
    assert same_price_bucket(10.0, None) is False
    assert same_price_bucket(0.0, 0.0) is False


# -------------------------------------------------------------------- filters
def test_accepts_telegram_offer_without_discount(conn) -> None:
    offer = make_offer(discount_pct=None, old_price=None, price=None)
    decision = evaluate_offer(conn, offer, make_config())

    assert decision.accepted is True
    assert decision.reason == filters.REASON_ACCEPTED


def test_discount_at_threshold_passes_and_below_rejects(conn) -> None:
    config = make_config(min_discount_pct=20)

    at_threshold = make_offer(discount_pct=20.0, content_hash="hash-at")
    assert evaluate_offer(conn, at_threshold, config).accepted is True

    below = make_offer(discount_pct=12.5, content_hash="hash-below")
    decision = evaluate_offer(conn, below, config)
    assert decision.accepted is False
    assert decision.reason == REASON_LOW_DISCOUNT
    assert "12.5%" in decision.detail


def test_excluded_keyword_is_case_insensitive(conn) -> None:
    offer = make_offer(title="SCAM Sony Headphones", content_hash="hash-scam")
    decision = evaluate_offer(conn, offer, make_config())

    assert decision.accepted is False
    assert decision.reason == REASON_EXCLUDED_KEYWORD
    assert "scam" in decision.detail


def test_excluded_keyword_matches_arabic_variants(conn) -> None:
    config = make_config(exclude_keywords=["مقلد"])
    offer = make_offer(title="سماعات مقلدة", content_hash="hash-arabic")
    decision = evaluate_offer(conn, offer, config)

    assert decision.accepted is False
    assert decision.reason == REASON_EXCLUDED_KEYWORD


def test_near_duplicate_title_in_price_bucket_is_rejected(conn) -> None:
    original_id = store(
        conn,
        title="Sony WH-1000XM5 Wireless Headphones",
        link="https://shop.example.com/a",
        price=299.0,
        content_hash="hash-a",
    )
    duplicate = make_offer(
        title="Sony WH-1000XM5 Wireless Headphones!",
        link="https://other.example.com/b",
        price=305.0,
        content_hash="hash-b",
        source_msg_id=2,
    )

    decision = evaluate_offer(conn, duplicate, make_config())

    assert decision.accepted is False
    assert decision.reason == REASON_NEAR_DUPLICATE
    assert decision.duplicate_of == original_id
    assert decision.score is not None and decision.score >= 88


def test_arabic_titles_are_fuzzy_matched(conn) -> None:
    original_id = store(
        conn,
        title="سماعات سوني الجديدة",
        link="https://shop.example.com/ar-a",
        price=1299.0,
        currency="SAR",
        content_hash="hash-ar-a",
    )
    duplicate = make_offer(
        title="سماعات سوني الجديده!",
        link="https://other.example.com/ar-b",
        price=1300.0,
        currency="SAR",
        content_hash="hash-ar-b",
        source_msg_id=2,
    )

    decision = evaluate_offer(conn, duplicate, make_config())

    assert decision.accepted is False
    assert decision.reason == REASON_NEAR_DUPLICATE
    assert decision.duplicate_of == original_id


def test_similar_title_outside_price_bucket_passes(conn) -> None:
    store(
        conn,
        title="Sony WH-1000XM5 Wireless Headphones",
        link="https://shop.example.com/a",
        price=299.0,
        content_hash="hash-a",
    )
    price_drop = make_offer(
        title="Sony WH-1000XM5 Wireless Headphones",
        link="https://other.example.com/b",
        price=399.0,
        content_hash="hash-b",
        source_msg_id=2,
    )

    assert evaluate_offer(conn, price_drop, make_config()).accepted is True


def test_same_canonical_link_is_rejected_even_with_another_title(conn) -> None:
    original_id = store(
        conn,
        title="Sony XM5",
        link="https://shop.example.com/p/1?utm_source=telegram",
        price=299.0,
        content_hash="hash-a",
    )
    repost = make_offer(
        title="Noise Cancelling Headphones",
        link="https://www.shop.example.com/p/1/?tag=aff-20",
        price=299.0,
        content_hash="hash-b",
        source_msg_id=2,
    )

    decision = evaluate_offer(conn, repost, make_config())

    assert decision.accepted is False
    assert decision.reason == REASON_NEAR_DUPLICATE
    assert decision.duplicate_of == original_id
    assert "link duplicate" in decision.detail


def test_large_price_drop_on_same_link_posts_as_new_deal(conn) -> None:
    store(
        conn,
        title="Sony XM5",
        link="https://shop.example.com/p/1",
        price=299.0,
        content_hash="hash-a",
    )
    price_drop = make_offer(
        title="Sony XM5",
        link="https://shop.example.com/p/1",
        price=199.0,
        content_hash="hash-b",
        source_msg_id=2,
    )

    assert evaluate_offer(conn, price_drop, make_config()).accepted is True


def test_rejected_offers_do_not_veto_later_offers(conn) -> None:
    rejected_id = store(
        conn,
        title="Sony WH-1000XM5 Wireless Headphones",
        link="https://shop.example.com/a",
        price=299.0,
        content_hash="hash-a",
    )
    db.mark_offer_rejected(conn, rejected_id)

    candidate = make_offer(
        title="Sony WH-1000XM5 Wireless Headphones",
        link="https://other.example.com/b",
        price=299.0,
        content_hash="hash-b",
        source_msg_id=2,
    )

    assert evaluate_offer(conn, candidate, make_config()).accepted is True


def test_different_currency_is_not_a_fuzzy_duplicate(conn) -> None:
    store(
        conn,
        title="Sony WH-1000XM5 Headphones",
        link="https://shop.example.com/a",
        price=299.0,
        content_hash="hash-a",
    )
    candidate = make_offer(
        title="Sony WH-1000XM5 Headphones",
        link="https://other.example.com/b",
        price=299.0,
        currency="EUR",
        content_hash="hash-b",
        source_msg_id=2,
    )

    assert evaluate_offer(conn, candidate, make_config()).accepted is True


def test_cross_source_duplicate_is_rejected(conn) -> None:
    amazon_id = store(
        conn,
        title="Sony WH-1000XM5 Headphones",
        link="https://www.amazon.com/dp/B000TEST01?tag=tag-20",
        price=299.0,
        source_type=SOURCE_AMAZON,
        source_channel="amazon",
        content_hash="hash-amazon",
    )
    telegram_post = make_offer(
        title="Sony WH-1000XM5 Headphones",
        link="https://www.amazon.com/dp/B000TEST01?tag=other-20",
        price=299.0,
        content_hash="hash-telegram",
        source_msg_id=2,
    )

    decision = evaluate_offer(conn, telegram_post, make_config())

    assert decision.accepted is False
    assert decision.reason == REASON_NEAR_DUPLICATE
    assert decision.duplicate_of == amazon_id


def test_source_cap_rejects_fourth_offer_from_same_channel(conn) -> None:
    config = make_config(max_per_source_per_hour=2)
    store(
        conn,
        title="Alpha Deal",
        link="https://shop.example.com/a",
        price=10.0,
        content_hash="hash-a",
    )
    store(
        conn,
        title="Beta Offer",
        link="https://shop.example.com/b",
        price=500.0,
        content_hash="hash-b",
    )

    third = make_offer(
        title="Gamma Bargain",
        link="https://shop.example.com/c",
        price=50.0,
        content_hash="hash-c",
        source_msg_id=3,
    )
    decision = evaluate_offer(conn, third, config)

    assert decision.accepted is False
    assert decision.reason == REASON_SOURCE_CAP
    assert "@chan1" in decision.detail

    other_source = make_offer(
        title="Delta Deal",
        link="https://shop.example.com/d",
        price=900.0,
        content_hash="hash-d",
        source_channel="@chan2",
        source_msg_id=4,
    )
    assert evaluate_offer(conn, other_source, config).accepted is True


def test_find_duplicate_returns_best_title_match(conn) -> None:
    store(
        conn,
        title="Sony WH-1000XM5 Headphones",
        link="https://shop.example.com/a",
        price=299.0,
        content_hash="hash-a",
    )
    store(
        conn,
        title="Sony WH-1000XM5 Headphones Black",
        link="https://shop.example.com/b",
        price=300.0,
        content_hash="hash-b",
    )
    offer = make_offer(
        title="Sony WH-1000XM5 Headphones",
        link="https://shop.example.com/c",
        price=301.0,
        content_hash="hash-c",
    )

    match = find_duplicate(
        offer,
        db.recent_offers(conn, include_rejected=False),
        threshold=88,
    )

    assert match is not None
    assert match.kind == "title"
    assert match.score == 100.0


# ----------------------------------------------------------------- persistence
def test_save_offer_marks_rejected_and_blocks_exact_rehash(conn) -> None:
    offer = make_offer()
    decision = FilterDecision(
        accepted=False,
        reason=REASON_LOW_DISCOUNT,
        detail="too weak",
    )

    offer_id = save_offer(conn, offer, decision)

    assert offer_id is not None
    stored = db.get_offer(conn, offer_id)
    assert stored is not None
    assert stored.status == STATUS_REJECTED
    assert save_offer(conn, offer, FilterDecision(accepted=True)) is None


def test_save_offer_leaves_accepted_offer_pending(conn) -> None:
    offer = make_offer(content_hash="hash-new")
    offer_id = save_offer(conn, offer, evaluate_offer(conn, offer, make_config()))

    assert offer_id is not None
    assert db.get_offer(conn, offer_id).status == STATUS_PENDING

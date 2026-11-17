"""Ingestion parsing tests using real-world deal post shapes."""

from __future__ import annotations

import pytest

from app.ingest import (
    BuildFailure,
    build_offer,
    canonicalize_url,
    clean_url,
    make_content_hash,
    parse_number,
    parse_pricing,
    pick_deal_url,
    extract_title,
)

AMAZON_URL = "https://www.amazon.com/dp/B09XS7JWHH?tag=deals-20&utm_source=telegram"


def test_build_offer_english_post() -> None:
    text = (
        "🔥 Sony WH-1000XM5 Wireless Headphones\n"
        "Price: $299.00 (was $399.00) — 25% off\n"
        f"{AMAZON_URL}"
    )
    offer = build_offer(
        text,
        [AMAZON_URL],
        source_channel="@deals",
        source_msg_id=42,
    )

    assert offer.title == "Sony WH-1000XM5 Wireless Headphones"
    assert offer.price == 299.0
    assert offer.old_price == 399.0
    assert offer.discount_pct == 25.1
    assert offer.currency == "USD"
    assert offer.link == AMAZON_URL
    assert offer.source_type == "telegram"
    assert offer.source_channel == "@deals"
    assert offer.source_msg_id == 42
    assert len(offer.content_hash) == 64


def test_build_offer_arabic_post() -> None:
    text = (
        "خصم 30% على سماعات سوني\n"
        "السعر: 1,299 ر.س بدلاً من 1,899 ر.س\n"
        "https://example.com/sony-xm5"
    )
    offer = build_offer(
        text, [], source_channel="@ar_deals", source_msg_id=7
    )

    assert offer.title == "خصم 30% على سماعات سوني"
    assert offer.price == 1299.0
    assert offer.old_price == 1899.0
    assert offer.currency == "SAR"
    assert offer.discount_pct == 31.6


def test_build_offer_requires_a_link() -> None:
    with pytest.raises(BuildFailure, match="link"):
        build_offer(
            "Sony XM5 headphones — $299", [], source_channel="@c", source_msg_id=1
        )


def test_channel_invite_is_not_a_deal_link() -> None:
    with pytest.raises(BuildFailure, match="link"):
        build_offer(
            "Join our other channel https://t.me/+abc123",
            ["https://t.me/+abc123"],
            source_channel="@c",
            source_msg_id=1,
        )


def test_build_offer_uses_url_from_text_when_entities_are_empty() -> None:
    offer = build_offer(
        "Air Fryer 5.8QT\nOnly $49.99\nhttps://shop.example.com/air-fryer",
        [],
        source_channel="@c",
        source_msg_id=2,
    )
    assert offer.link == "https://shop.example.com/air-fryer"
    assert offer.price == 49.99


def test_pick_deal_url_prefers_shops_over_social_and_telegram() -> None:
    urls = [
        "https://t.me/somechannel/123",
        "https://instagram.com/p/abc",
        "https://www.amazon.com/dp/B000?tag=x-20",
    ]
    assert pick_deal_url(urls) == "https://www.amazon.com/dp/B000?tag=x-20"


def test_pick_deal_url_skips_invites() -> None:
    assert pick_deal_url(["https://t.me/+secret"]) is None


def test_clean_url_adds_scheme_and_strips_punctuation() -> None:
    assert clean_url("www.example.com/deal).") == "https://www.example.com/deal"
    assert clean_url("not a url") is None


def test_canonicalize_url_strips_tracking_and_normalizes_scheme() -> None:
    canonical = canonicalize_url(
        "http://www.Amazon.com/dp/B09XS7JWHH/ref=sr_1_1?tag=deals-20&utm_source=tg&b=2&a=1#top"
    )
    assert canonical == "https://amazon.com/dp/B09XS7JWHH?a=1&b=2"


def test_content_hash_ignores_tracking_and_affiliate_tag() -> None:
    first = make_content_hash(
        "Sony XM5", "https://amazon.com/dp/B09?tag=a-20&utm_medium=tg", 299.0
    )
    second = make_content_hash(
        "sony   xm5", "http://www.amazon.com/dp/B09/ref=xyz?tag=b-20", 299.0
    )
    other_price = make_content_hash(
        "Sony XM5", "https://amazon.com/dp/B09", 279.0
    )

    assert first == second
    assert first != other_price


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("1,299.00", 1299.0),
        ("1.299,00", 1299.0),
        ("1299", 1299.0),
        ("19.99", 19.99),
        ("19,99", 19.99),
        ("1.299", 1299.0),
        ("1.234.567", 1234567.0),
    ],
)
def test_parse_number_formats(token: str, expected: float) -> None:
    assert parse_number(token) == expected


def test_parse_pricing_english_discount() -> None:
    info = parse_pricing("Sony XM5 — $299.00 (was $399.00) 25% off")
    assert (info.price, info.old_price, info.currency) == (299.0, 399.0, "USD")
    assert info.discount_pct == 25.1


def test_parse_pricing_arabic() -> None:
    info = parse_pricing("خصم 30% على سماعات سوني\nالسعر: 1,299 ر.س بدلاً من 1,899 ر.س")
    assert (info.price, info.old_price, info.currency) == (1299.0, 1899.0, "SAR")
    assert info.discount_pct == 31.6


def test_parse_pricing_arabic_indic_digits() -> None:
    info = parse_pricing("السعر: ٩٩٩ ر.س")
    assert info.price == 999.0
    assert info.currency == "SAR"


def test_parse_pricing_european_format() -> None:
    info = parse_pricing("Deal: 1.299,00 € statt 1.599,00 €")
    assert (info.price, info.old_price, info.currency) == (1299.0, 1599.0, "EUR")


def test_parse_pricing_single_price_with_stated_discount() -> None:
    info = parse_pricing("خصم 50% فقط 99 ريال")
    assert info.price == 99.0
    assert info.old_price == 198.0
    assert info.discount_pct == 50.0
    assert info.currency == "SAR"


def test_parse_pricing_ignores_installments() -> None:
    info = parse_pricing("Price: $299 or 4 payments of $74.75")
    assert info.price == 299.0
    assert info.old_price is None
    assert info.discount_pct is None


def test_parse_pricing_ignores_shipping_fee() -> None:
    info = parse_pricing("Price: $20 + $5 shipping")
    assert info.price == 20.0
    assert info.old_price is None


def test_parse_pricing_ignores_model_numbers() -> None:
    info = parse_pricing("iPhone 15 Pro Max 256GB — brand new")
    assert info.price is None
    assert info.discount_pct is None


def test_parse_pricing_ignores_non_discount_percentages() -> None:
    info = parse_pricing("100% authentic — free shipping")
    assert info.discount_pct is None


def test_parse_pricing_accepts_price_word_without_currency() -> None:
    info = parse_pricing("Only 49 today")
    assert info.price == 49.0
    assert info.currency == "USD"


def test_extract_title_skips_emoji_and_price_lines() -> None:
    text = "🔥🔥🔥\n$29.99\nAir Fryer 5.8QT\nhttps://shop.example/x"
    assert extract_title(text) == "Air Fryer 5.8QT"


def test_extract_title_truncates_long_titles() -> None:
    title = extract_title(" ".join(["Sony"] * 80))
    assert len(title) <= 180
    assert title.endswith("…")


def test_extract_title_requires_text() -> None:
    with pytest.raises(BuildFailure, match="title"):
        extract_title("🔥🔥\n$29.99\nhttps://shop.example/x")

"""Amazon-title copying and plain-link publication without real services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import broadcaster, db, listener
from app.config import Config, ConfigError
from app.ingest import build_offer, replace_title
from app.links import affiliate_link, has_plain_amazon_links
from app.products import ProductLookupError


PRIMARY = "https://www.amazon.eg/dp/B000000001"


@pytest.fixture()
def config(tmp_path):
    return Config(
        bot_token="offline-token", target_channels=["@deal_test"],
        amazon_marketplace="www.amazon.eg", media_dir=tmp_path / "media",
        # Deliberately configured but unconfirmed: plain mode must ignore it.
        amazon_associate_tag="unverified-21", amazon_tag_confirmed=False,
        brand_name="test", brand_username="@deal_test", brand_url="https://t.me/deal_test",
    )


@pytest.fixture()
def conn(tmp_path):
    connection = db.init_db(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def title_lookup(monkeypatch):
    lookup = Mock(return_value="Actual Amazon product title")
    monkeypatch.setattr(listener, "fetch_product_title", lookup)
    return lookup


def message(text=None, msg_id=1):
    return SimpleNamespace(
        id=msg_id, photo=None,
        message=text if text is not None else (
            "Source promotional headline\nPrice: 999 EGP\n"
            + PRIMARY + "?tag=someone-else-21&linkCode=abc"
        ),
        get_entities_text=lambda: [],
    )


def ingest(conn, config, post=None, **kwargs):
    return asyncio.run(listener.process_message(
        object(), post or message(), "@source", conn=conn, config=config, **kwargs,
    ))


def test_plain_is_default_and_needs_no_confirmed_tag(config):
    assert config.amazon_link_mode == "plain"
    config.require_amazon_links()
    config.require_broadcaster()
    config.amazon_link_mode = "affiliate"
    with pytest.raises(ConfigError, match="AMAZON_TAG_CONFIRMED"):
        config.require_broadcaster()


def test_plain_copy_stores_real_title_preserves_prices_and_removes_all_tracking(
    conn, config, title_lookup,
):
    title_lookup.return_value = "Actual Amazon 500 ml product 12 EGP"
    offer_id = ingest(conn, config)
    stored = db.get_offer(conn, offer_id)
    assert stored.title == title_lookup.return_value
    assert stored.post_text.splitlines()[0] == title_lookup.return_value
    assert "Source promotional headline" not in stored.post_text
    assert "Price: 999 EGP" in stored.post_text
    assert stored.price == 999
    assert stored.currency == "EGP"
    assert stored.link == PRIMARY
    assert "tag=" not in stored.post_text
    assert "linkCode=" not in stored.post_text
    assert "unverified" not in stored.post_text
    title_lookup.assert_called_once_with(PRIMARY)


def test_source_brand_replacement_does_not_change_authoritative_product_title(conn, config, title_lookup):
    title_lookup.return_value = "Amazon Basics Product"
    offer_id = ingest(conn, config, source_names=("Amazon",))
    stored = db.get_offer(conn, offer_id)
    assert stored.title == "Amazon Basics Product"
    assert stored.post_text.splitlines()[0] == "Amazon Basics Product"


@pytest.mark.parametrize("text", [PRIMARY, "\n\n" + PRIMARY, "Deal " + PRIMARY])
def test_product_title_and_link_survive_url_only_or_inline_source_headline(conn, config, text):
    offer_id = ingest(conn, config, message(text))
    stored = db.get_offer(conn, offer_id)
    assert stored.post_text.startswith("Actual Amazon product title")
    assert PRIMARY in stored.post_text


def test_rebrands_mentions_and_channel_links_in_remaining_body(conn, config):
    post = message("Source title\nMore @source https://t.me/source\n" + PRIMARY)
    offer_id = ingest(conn, config, post, source_usernames=("source",))
    stored = db.get_offer(conn, offer_id)
    assert "@deal_test" in stored.post_text
    assert "https://t.me/deal_test" in stored.post_text
    assert "@source" not in stored.post_text


def test_link_resolution_and_title_lookup_are_both_used(conn, config, title_lookup):
    resolver = Mock(return_value=PRIMARY + "?tag=someone-else-21")
    offer_id = ingest(conn, config, message("Old title\nhttps://amzn.to/short"), resolver=resolver)
    assert db.get_offer(conn, offer_id).link == PRIMARY
    title_lookup.assert_called_once_with(PRIMARY)


def test_missing_amazon_title_is_not_replaced_by_source_headline(conn, config, title_lookup):
    title_lookup.side_effect = ProductLookupError("Amazon CAPTCHA/access challenge")
    assert ingest(conn, config) is None
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    # A temporary lookup failure isn't recorded as processed; the next poll retries.
    title_lookup.side_effect = None
    assert ingest(conn, config) is not None


@pytest.mark.parametrize("title", [None, "", "   "])
def test_empty_amazon_title_never_enters_the_queue(conn, config, title_lookup, title):
    title_lookup.return_value = title
    assert ingest(conn, config) is None
    assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_non_amazon_announcement_does_not_request_a_product_title(conn, config, title_lookup):
    assert ingest(conn, config, message("Channel announcement")) is None
    title_lookup.assert_not_called()


def test_seen_source_message_skips_title_network_lookup(conn, config, title_lookup):
    assert ingest(conn, config) is not None
    assert ingest(conn, config) is None
    assert title_lookup.call_count == 1


def test_actual_title_deduplicates_different_source_headlines_for_same_deal(conn, config):
    assert ingest(conn, config) is not None
    assert ingest(conn, config, message("Completely different headline\nPrice: 999 EGP\n" + PRIMARY, 2)) is None
    assert conn.execute("SELECT COUNT(*) FROM offers WHERE status = 'pending'").fetchone()[0] == 1


def test_plain_rewriter_uses_no_empty_tag_query():
    assert affiliate_link(PRIMARY + "?tag=other-21", marketplace="www.amazon.eg", partner_tag="") == PRIMARY


@pytest.mark.parametrize("url", [
    PRIMARY + "?tag=other-21", PRIMARY + "?tag=", PRIMARY + "?ref=tracking",
    PRIMARY + "#tracking", "https://amzn.to/short",
    "https://www.amazon.com/dp/B000000001",
    "https://www.amazon.eg@evil.example/dp/B000000001",
])
def test_plain_send_guard_rejects_tracking_and_unresolved_urls(url):
    assert not has_plain_amazon_links("Title " + url, url, marketplace="www.amazon.eg")


def test_plain_send_guard_checks_secondary_amazon_links():
    assert has_plain_amazon_links("Title " + PRIMARY, PRIMARY, marketplace="www.amazon.eg")
    assert not has_plain_amazon_links(
        "Title " + PRIMARY + "\nwww.amazon.eg/dp/B000000002?tag=other",
        PRIMARY, marketplace="www.amazon.eg",
    )


def test_plain_broadcaster_delivers_and_never_resends(conn, config):
    offer_id = ingest(conn, config)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    first = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    second = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert first.sent == 1 and first.failed == 0
    assert second.sent == 0
    assert db.get_offer(conn, offer_id).status == "posted"
    bot.send_message.assert_awaited_once()


def test_plain_broadcaster_blocks_a_stale_affiliate_queue_entry(conn, config):
    offer_id = ingest(conn, config)
    with conn:
        conn.execute("UPDATE offers SET post_text = post_text || '?tag=old-21' WHERE id = ?", (offer_id,))
    bot = SimpleNamespace(send_message=AsyncMock())
    result = asyncio.run(broadcaster.broadcast_once(conn, config, bot))
    assert result.sent == 0 and result.failed == 1
    bot.send_message.assert_not_awaited()


def test_explicit_title_override_parses_prices_only_from_source_text():
    offer = build_offer("Source title\n999 EGP\n" + PRIMARY, [PRIMARY],
                        source_channel="@source", source_msg_id=1,
                        title="Amazon title with model 12 EGP")
    assert offer.title == "Amazon title with model 12 EGP"
    assert offer.price == 999


@pytest.mark.parametrize("headline", [
    "Source headphones - 999 EGP - coupon SAVE10",
    "Source headphones - 999 جنيه - بكود SAVE10",
])
def test_replacing_inline_headline_keeps_price_and_coupon(conn, config, headline):
    offer_id = ingest(conn, config, message(headline + "\n" + PRIMARY))
    stored = db.get_offer(conn, offer_id)
    assert stored.post_text.startswith("Actual Amazon product title\n")
    assert "Source headphones" not in stored.post_text
    assert "999 EGP" in stored.post_text
    assert "SAVE10" in stored.post_text
    assert stored.price == 999


def test_inline_reference_price_is_preserved_without_inventing_a_was_price():
    body = replace_title("Old headphones - was 1999 EGP, now 999 EGP", "Amazon title")
    assert "Price: 999 EGP (was 1999 EGP)" in body
    inferred = replace_title("Old headphones - 999 EGP - 50% off", "Amazon title")
    assert "Price: 999 EGP" in inferred
    assert "was" not in inferred
    assert "50%" in inferred


def test_conflicting_query_asin_never_looks_up_another_product(conn, config, title_lookup):
    assert ingest(conn, config, message("Source product\n" + PRIMARY + "?pd_rd_i=B000000002")) is None
    title_lookup.assert_not_called()


def test_replace_title_keeps_separate_price_body_and_url_lines():
    assert replace_title("\n999 EGP\nOld title\nBody\n" + PRIMARY, "Amazon title", [PRIMARY]) == (
        "Amazon title\n\n999 EGP\nBody\n" + PRIMARY
    )

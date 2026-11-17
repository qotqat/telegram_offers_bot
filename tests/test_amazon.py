"""Amazon Creators API client, mapping and poller tests (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import amazon, db
from app.amazon import (
    AmazonError,
    CreatorsClient,
    SkipItem,
    item_to_offer,
    poll,
)
from app.config import Config


# ----------------------------------------------------------------- fixtures
def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(
        amazon_enabled=True,
        amazon_credential_id="cred-id",
        amazon_credential_secret="cred-secret",
        amazon_credential_version="3.1",
        amazon_associate_tag="tag-20",
        amazon_marketplace="www.amazon.com",
        amazon_keywords=["headphones"],
        amazon_item_count=10,
        amazon_min_discount_pct=20.0,
        amazon_request_delay_seconds=0.0,
        db_path=tmp_path / "offers.db",
        media_dir=tmp_path / "media",
    )
    values.update(overrides)
    return Config(**values)


def token_ok(token: str = "token-1") -> tuple[int, bytes]:
    return 200, json.dumps(
        {"access_token": token, "expires_in": 3600, "token_type": "bearer"}
    ).encode()


def search_ok(items: list[dict]) -> tuple[int, bytes]:
    return 200, json.dumps({"searchResult": {"items": items}}).encode()


class FakeTransport:
    """Scripted HTTP transport that records every call."""

    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body):
        payload = json.loads(body.decode("utf-8")) if body else None
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "payload": payload,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
        return self.responses.pop(0)


def make_item(**overrides) -> dict:
    item = {
        "asin": "B000TEST01",
        "detailPageURL": (
            "https://www.amazon.com/dp/B000TEST01?tag=other-20&linkCode=ogi"
        ),
        "images": {
            "primary": {
                "medium": {"url": "https://img.example/medium.jpg"},
                "large": {"url": "https://img.example/large.jpg"},
            }
        },
        "itemInfo": {"title": {"displayValue": "Sony WH-1000XM5 Headphones"}},
        "offersV2": {
            "listings": [
                {
                    "isBuyBoxWinner": False,
                    "condition": {"value": "New"},
                    "availability": {"type": "IN_STOCK"},
                    "price": {
                        "money": {"amount": 349.99, "currency": "USD"},
                        "savingBasis": {"money": {"amount": 399.0}},
                        "savings": {"percentage": 12},
                    },
                },
                {
                    "isBuyBoxWinner": True,
                    "condition": {"value": "New"},
                    "availability": {"type": "IN_STOCK"},
                    "price": {
                        "money": {"amount": 299.99, "currency": "USD"},
                        "savingBasis": {"money": {"amount": 399.0}},
                        "savings": {"percentage": 25},
                    },
                },
            ]
        },
    }
    item.update(overrides)
    return item


@pytest.fixture()
def client() -> CreatorsClient:
    transport = FakeTransport([])
    return CreatorsClient(
        "cred-id",
        "cred-secret",
        "tag-20",
        transport=transport,
    )


# ------------------------------------------------------------------- client
def test_search_requests_token_then_catalog() -> None:
    transport = FakeTransport([token_ok(), search_ok([make_item()])])
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)

    items = client.search_items(
        "headphones", item_count=5, min_saving_percent=20
    )

    assert len(items) == 1
    token_call, search_call = transport.calls
    assert token_call["method"] == "POST"
    assert token_call["url"] == "https://api.amazon.com/auth/o2/token"
    assert token_call["payload"] == {
        "grant_type": "client_credentials",
        "client_id": "cred-id",
        "client_secret": "cred-secret",
        "scope": "creatorsapi::default",
    }

    assert search_call["url"] == (
        "https://creatorsapi.amazon/catalog/v1/searchItems"
    )
    assert search_call["headers"]["Authorization"] == "Bearer token-1"
    assert search_call["headers"]["x-marketplace"] == "www.amazon.com"
    payload = search_call["payload"]
    assert payload["keywords"] == "headphones"
    assert payload["partnerTag"] == "tag-20"
    assert payload["marketplace"] == "www.amazon.com"
    assert payload["itemCount"] == 5
    assert payload["minSavingPercent"] == 20
    assert "offersV2.listings.price" in payload["resources"]
    assert "itemInfo.title" in payload["resources"]


def test_token_is_cached_between_searches() -> None:
    transport = FakeTransport(
        [token_ok(), search_ok([]), search_ok([])]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)

    client.search_items("one")
    client.search_items("two")

    token_urls = [
        call["url"] for call in transport.calls if call["url"].endswith("/token")
    ]
    assert len(token_urls) == 1
    assert len(transport.calls) == 3


def test_401_refreshes_token_once() -> None:
    transport = FakeTransport(
        [
            token_ok("stale"),
            (401, b'{"message":"token expired"}'),
            token_ok("fresh"),
            search_ok([make_item()]),
        ]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)

    items = client.search_items("headphones")

    assert len(items) == 1
    assert [call["url"] for call in transport.calls] == [
        "https://api.amazon.com/auth/o2/token",
        "https://creatorsapi.amazon/catalog/v1/searchItems",
        "https://api.amazon.com/auth/o2/token",
        "https://creatorsapi.amazon/catalog/v1/searchItems",
    ]
    assert transport.calls[-1]["headers"]["Authorization"] == "Bearer fresh"


def test_token_failure_raises_amazon_error() -> None:
    transport = FakeTransport(
        [(400, b'{"error":"invalid_client","message":"bad credentials"}')]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)

    with pytest.raises(AmazonError, match="HTTP 400") as excinfo:
        client.search_items("headphones")
    assert "invalid_client" in str(excinfo.value)


def test_catalog_error_includes_response_snippet() -> None:
    transport = FakeTransport(
        [token_ok(), (429, b'{"message":"TooManyRequests"}')]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)

    with pytest.raises(AmazonError, match="HTTP 429") as excinfo:
        client.search_items("headphones")
    assert "TooManyRequests" in str(excinfo.value)


def test_get_items_uses_asin_payload() -> None:
    transport = FakeTransport(
        [
            token_ok(),
            (
                200,
                json.dumps({"itemResults": {"items": [make_item()]}}).encode(),
            ),
        ]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)

    items = client.get_items(["B000TEST01"])

    assert len(items) == 1
    call = transport.calls[-1]
    assert call["url"] == "https://creatorsapi.amazon/catalog/v1/getItems"
    assert call["payload"]["itemIds"] == ["B000TEST01"]
    assert call["payload"]["itemIdType"] == "ASIN"


# ------------------------------------------------------------------ mapping
def test_item_to_offer_picks_buy_box_and_maps_fields() -> None:
    offer = item_to_offer(
        make_item(),
        partner_tag="tag-20",
        marketplace="www.amazon.com",
        min_discount_pct=20,
        category="headphones",
    )

    assert offer.title == "Sony WH-1000XM5 Headphones"
    assert offer.price == 299.99
    assert offer.old_price == 399.0
    assert offer.discount_pct == 25.0
    assert offer.currency == "USD"
    assert offer.image_url == "https://img.example/large.jpg"
    assert offer.category == "headphones"
    assert offer.source_type == "amazon"
    assert offer.source_channel == "amazon"
    assert len(offer.content_hash) == 64
    assert "tag=tag-20" in offer.link
    assert "tag=other-20" not in offer.link


def test_item_to_offer_rejects_discount_below_threshold() -> None:
    item = make_item(
        offersV2={
            "listings": [
                {
                    "price": {
                        "money": {"amount": 380.0, "currency": "USD"},
                        "savingBasis": {"money": {"amount": 400.0}},
                        "savings": {"percentage": 5},
                    }
                }
            ]
        }
    )
    with pytest.raises(SkipItem, match="AMAZON_MIN_DISCOUNT_PCT"):
        item_to_offer(
            item,
            partner_tag="tag-20",
            marketplace="www.amazon.com",
            min_discount_pct=20,
        )


def test_item_to_offer_rejects_out_of_stock_and_missing_prices() -> None:
    out_of_stock = make_item(
        offersV2={
            "listings": [
                {
                    "availability": {"type": "OUT_OF_STOCK"},
                    "price": {"money": {"amount": 99.0, "currency": "USD"}},
                }
            ]
        }
    )
    with pytest.raises(SkipItem, match="no priced"):
        item_to_offer(
            out_of_stock, partner_tag="tag-20", marketplace="www.amazon.com"
        )

    no_price = make_item(
        offersV2={"listings": [{"condition": {"value": "New"}}]}
    )
    with pytest.raises(SkipItem, match="no priced"):
        item_to_offer(no_price, partner_tag="tag-20", marketplace="www.amazon.com")


def test_item_to_offer_builds_link_from_asin() -> None:
    item = make_item()
    del item["detailPageURL"]

    offer = item_to_offer(
        item, partner_tag="tag-20", marketplace="www.amazon.com"
    )

    assert offer.link == "https://www.amazon.com/dp/B000TEST01?tag=tag-20"


def test_item_to_offer_requires_a_title() -> None:
    item = make_item(itemInfo={"title": {"displayValue": "   "}})
    with pytest.raises(SkipItem, match="title"):
        item_to_offer(item, partner_tag="tag-20", marketplace="www.amazon.com")


# ------------------------------------------------------------------- poller
def test_poll_disabled_does_not_touch_network_or_db(tmp_path) -> None:
    config = make_config(
        tmp_path, amazon_enabled=False, amazon_keywords=[]
    )
    transport = FakeTransport([])
    client = CreatorsClient(
        "cred-id", "cred-secret", "tag-20", transport=transport
    )
    conn = db.init_db(tmp_path / "offers.db")
    try:
        result = poll(config, conn, client=client)
        assert result.enabled is False
        assert result.stored == 0
        assert transport.calls == []
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    finally:
        conn.close()


def test_poll_stores_then_dedupes(tmp_path) -> None:
    config = make_config(tmp_path)
    transport = FakeTransport(
        [
            token_ok(),
            search_ok([make_item()]),
            search_ok([make_item()]),
        ]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)
    conn = db.init_db(tmp_path / "offers.db")
    try:
        first = poll(config, conn, client=client)
        second = poll(config, conn, client=client)

        assert (first.searches, first.items, first.stored) == (1, 1, 1)
        assert (second.stored, second.duplicates) == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
        assert db.get_state(conn, "last_amazon_poll")
        stored = db.recent_offers(conn)[0]
        assert stored.source_type == "amazon"
        assert stored.category == "headphones"
    finally:
        conn.close()


def test_poll_continues_after_one_keyword_fails(tmp_path) -> None:
    config = make_config(
        tmp_path, amazon_keywords=["broken", "working"]
    )
    transport = FakeTransport(
        [
            token_ok(),
            (500, b'{"message":"internal error"}'),
            search_ok([make_item()]),
        ]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)
    conn = db.init_db(tmp_path / "offers.db")
    try:
        result = poll(config, conn, client=client, sleep=lambda _seconds: None)

        assert result.searches == 1
        assert result.stored == 1
        assert len(result.errors) == 1
        assert "broken" in result.errors[0]
    finally:
        conn.close()


def test_poll_counts_low_discount_items_as_skipped(tmp_path) -> None:
    config = make_config(tmp_path, amazon_min_discount_pct=30)
    transport = FakeTransport([token_ok(), search_ok([make_item()])])
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)
    conn = db.init_db(tmp_path / "offers.db")
    try:
        result = poll(config, conn, client=client)

        assert result.stored == 0
        assert result.skipped == 1
        assert conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    finally:
        conn.close()


def test_collect_offers_pauses_between_keywords(tmp_path) -> None:
    config = make_config(
        tmp_path,
        amazon_keywords=["one", "two", "three"],
        amazon_request_delay_seconds=1.5,
    )
    transport = FakeTransport(
        [token_ok(), search_ok([]), search_ok([]), search_ok([])]
    )
    client = CreatorsClient("cred-id", "cred-secret", "tag-20", transport=transport)
    sleeps: list[float] = []

    result = amazon.collect_offers(config, client, sleep=sleeps.append)

    assert result.searches == 3
    assert sleeps == [1.5, 1.5]

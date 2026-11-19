"""Amazon link detection, affiliate rewriting and copied-post cleanup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.links import (
    affiliate_link,
    build_post_text,
    extract_asin,
    fallback_text,
    has_owned_amazon_links,
    is_amazon_link,
    is_short_amazon_link,
    replace_source_names,
    resolve_foreign_amazon_links
    rewrite_amazon_links,
)

MARKETPLACE = "www.amazon.eg"
TAG = "ours-21"


@pytest.mark.parametrize("url", [
    "https://www.amazon.eg:443@attacker.invalid/dp/B0CH8DKDKN?tag=ours-21",
    "https://www.amazon.eg@attacker.invalid/dp/B0CH8DKDKN?tag=ours-21",
    "https://user:password@www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21",
])
def test_amazon_detection_rejects_url_userinfo_and_spoofed_authorities(url):
    assert not is_amazon_link(url)
    assert affiliate_link(url, marketplace=MARKETPLACE, partner_tag=TAG) is None
    assert not has_owned_amazon_links(url, url, marketplace=MARKETPLACE, partner_tag=TAG)


@pytest.mark.parametrize("url", [
    "www.amazon.eg/dp/B0CH8DKDKN?tag=someone-else-21",
    "amazon.eg/dp/B0CH8DKDKN?tag=someone-else-21",
    "smile.amazon.eg/dp/B0CH8DKDKN?tag=someone-else-21",
    "amzn.to/unknown",
])
def test_send_guard_rejects_bare_amazon_links_with_stale_or_unresolved_tags(url):
    primary = "https://www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21"
    assert not has_owned_amazon_links(
        "Copied post " + url, primary, marketplace=MARKETPLACE, partner_tag=TAG,
    )


def test_rewriter_normalizes_bare_amazon_urls():
    result = rewrite_amazon_links(
        "Deal www.amazon.eg/dp/B0CH8DKDKN?tag=someone-else-21",
        marketplace=MARKETPLACE, partner_tag=TAG,
    )
    assert result.failed == []
    assert "https://www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21" in result.text
    assert "someone-else" not in result.text


def test_is_amazon_link_accepts_storefronts_and_short_hosts() -> None:
    assert is_amazon_link("https://www.amazon.eg/dp/B0CH8DKDKN")
    assert is_amazon_link("https://amazon.com/dp/B000000001?tag=x")
    assert is_amazon_link("https://link.amazon/B0dpfzV4h")
    assert is_amazon_link("https://amzn.to/abc")
    assert not is_amazon_link("https://shop.example.com/p/1")
    assert not is_amazon_link("https://notamazon.com/dp/B000000001")


def test_is_short_amazon_link() -> None:
    assert is_short_amazon_link("https://link.amazon/B0dpfzV4h")
    assert is_short_amazon_link("https://amzn.to/abc")
    assert not is_short_amazon_link("https://www.amazon.eg/dp/B0CH8DKDKN")


def test_extract_asin_from_path_and_query() -> None:
    assert extract_asin("https://www.amazon.eg/dp/B0CH8DKDKN?tag=x") == "B0CH8DKDKN"
    assert (
        extract_asin("https://www.amazon.com/gp/product/B09XS7JWHH/ref=x")
        == "B09XS7JWHH"
    )
    assert (
        extract_asin("https://www.amazon.com/checkout?asin=B000000002")
        == "B000000002"
    )
    assert extract_asin("https://www.amazon.com/deals") is None


@pytest.mark.parametrize("query", [
    "pd_rd_i=B000000002", "asin=B000000002", "dib=B000000002",
    "asin=B000000001&asin=B000000002",
])
def test_conflicting_query_asin_cannot_override_product_path(query):
    assert extract_asin("https://www.amazon.eg/dp/B000000001?" + query) is None


def test_multiple_conflicting_query_only_asins_are_ambiguous():
    assert extract_asin("https://www.amazon.eg/?asin=B000000001&asin=B000000002") is None


def test_temporary_short_link_failure_is_retried(monkeypatch):
    import urllib.error
    from unittest.mock import MagicMock, Mock
    from app import links

    url = "https://amzn.to/temporary-regression"
    response = MagicMock()
    response.__enter__.return_value.geturl.return_value = "https://www.amazon.eg/dp/B000000001"
    request = Mock(side_effect=[urllib.error.URLError("temporary"), response])
    monkeypatch.setattr(links.urllib.request, "urlopen", request)
    monkeypatch.setattr(links, "_RESOLVE_CACHE", {})
    assert links.resolve_amazon_url(url) is None
    assert links.resolve_amazon_url(url) == "https://www.amazon.eg/dp/B000000001"
    assert request.call_count == 2


def test_affiliate_link_rebuilds_product_link_on_our_marketplace() -> None:
    link = affiliate_link(
        "https://www.amazon.com/dp/B09XS7JWHH?tag=their-20&utm_source=tg",
        marketplace=MARKETPLACE,
        partner_tag=TAG,
    )
    assert link == "https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21"


def test_affiliate_link_resolves_short_links() -> None:
    calls: list[str] = []

    def resolver(url: str) -> str:
        calls.append(url)
        return (
            "https://www.amazon.eg/dp/B0CH8DKDKN?"
            "tag=their-21&linkCode=sl2&ascsubtag=abc"
        )

    link = affiliate_link(
        "https://link.amazon/B0dpfzV4h",
        marketplace=MARKETPLACE,
        partner_tag=TAG,
        resolver=resolver,
    )

    assert calls == ["https://link.amazon/B0dpfzV4h"]
    assert link == "https://www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21"


def test_affiliate_link_rejects_non_amazon_and_failed_resolution() -> None:
    assert (
        affiliate_link(
            "https://shop.example.com/p/1",
            marketplace=MARKETPLACE,
            partner_tag=TAG,
        )
        is None
    )
    assert (
        affiliate_link(
            "https://link.amazon/B0dpfzV4h",
            marketplace=MARKETPLACE,
            partner_tag=TAG,
            resolver=lambda url: None,
        )
        is None
    )


def test_rewrite_amazon_links_leaves_other_links_alone() -> None:
    text = (
        "Deal https://www.amazon.com/dp/B09XS7JWHH?tag=their-20.\n"
        "Video https://youtu.be/abc"
    )
    result = rewrite_amazon_links(
        text, marketplace=MARKETPLACE, partner_tag=TAG
    )

    assert "tag=ours-21" in result.text
    assert "their-20" not in result.text
    assert "https://youtu.be/abc" in result.text
    assert result.links == ["https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21"]
    assert result.failed == []


def test_rewrite_amazon_links_reports_failures() -> None:
    result = rewrite_amazon_links(
        "https://link.amazon/B0dpfzV4h",
        marketplace=MARKETPLACE,
        partner_tag=TAG,
        resolver=lambda url: None,
    )
    assert result.failed == ["https://link.amazon/B0dpfzV4h"]
    assert result.links == []


def test_replace_source_names_rebrands_names_mentions_and_links() -> None:
    text = (
        "Join @deals_me or https://t.me/deals_me\n"
        "Private: https://t.me/+secret\n"
        "By Deals Me"
    )
    result = replace_source_names(
        text,
        source_names=("Deals Me",),
        source_usernames=("deals_me",),
        brand_name="Bosla Deals",
        brand_username="@deals_test",
        brand_url="https://t.me/deals_test",
    )

    assert result == (
        "Join @deals_test or https://t.me/deals_test\n"
        "Private: https://t.me/deals_test\n"
        "By Bosla Deals"
    )


def test_build_post_text_appends_primary_link_and_rebrands() -> None:
    text = "Sony XM5 from Deals Me\nPrice: $299"
    result = build_post_text(
        text,
        primary_link="https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21",
        source_names=("Deals Me",),
        source_usernames=("deals_me",),
        marketplace=MARKETPLACE,
        partner_tag=TAG,
        brand_name="Bosla Deals",
        brand_username="@deals_test",
        brand_url="https://t.me/deals_test",
    )

    assert result.text.startswith("Sony XM5 from Bosla Deals")
    assert result.text.endswith("https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21")


def test_fallback_text_has_title_price_and_link() -> None:
    offer = SimpleNamespace(
        title="Sony XM5",
        price=299.0,
        old_price=399.0,
        discount_pct=25.1,
        currency="USD",
        link="https://www.amazon.eg/dp/B09XS7JWHH?tag=ours-21",
    )
    text = fallback_text(offer)

    assert "Sony XM5" in text
    assert "299 USD" in text
    assert "-25.1%" in text
    assert "tag=ours-21" in text

def test_resolve_foreign_amazon_links_follows_third_party_redirect() -> None:
    calls: list[str] = []

    def resolver(url: str) -> str:
        calls.append(url)
        return (
            "https://www.amazon.eg/dp/B0CH8DKDKN?"
            "tag=their-21&linkCode=sl2"
        )

    result = resolve_foreign_amazon_links(
        ["https://t.me/deals_me", "https://3rrood.com/PHVVsLyN"],
        resolver=resolver,
    )

    assert calls == ["https://3rrood.com/PHVVsLyN"]
    assert result == {
        "https://3rrood.com/PHVVsLyN":
            "https://www.amazon.eg/dp/B0CH8DKDKN?tag=their-21&linkCode=sl2"
    }


def test_resolve_foreign_amazon_links_ignores_non_amazon_destination() -> None:
    calls: list[str] = []

    def resolver(url: str) -> str:
        calls.append(url)
        return "https://shop.example.com/landing"

    result = resolve_foreign_amazon_links(
        ["https://3rrood.com/not-amazon"],
        resolver=resolver,
    )

    assert calls == ["https://3rrood.com/not-amazon"]
    assert result == {}


def test_resolve_foreign_amazon_links_rejects_private_hosts() -> None:
    calls: list[str] = []

    def resolver(url: str) -> str:
        calls.append(url)
        return "https://www.amazon.eg/dp/B0CH8DKDKN"

    result = resolve_foreign_amazon_links(
        ["http://127.0.0.1/internal", "http://10.0.0.5/internal"],
        resolver=resolver,
    )

    assert calls == []
    assert result == {}


def test_rewrite_amazon_links_replaces_foreign_redirect() -> None:
    result = rewrite_amazon_links(
        "Deal https://3rrood.com/PHVVsLyN now",
        marketplace=MARKETPLACE,
        partner_tag=TAG,
        foreign_links={
            "https://3rrood.com/PHVVsLyN":
                "https://www.amazon.eg/dp/B0CH8DKDKN?tag=their-21"
        },
    )

    assert result.text == (
        "Deal https://www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21 now"
    )
    assert result.links == [
        "https://www.amazon.eg/dp/B0CH8DKDKN?tag=ours-21"
    ]
    assert result.failed == []


def test_resolve_url_follows_meta_refresh(monkeypatch) -> None:
    from unittest.mock import MagicMock, Mock
    from app import links

    response = MagicMock()
    response.__enter__.return_value.geturl.return_value = (
        "https://3rrood.com/PHVVsLyN"
    )
    response.__enter__.return_value.read.return_value = (
        b'<meta http-equiv="refresh" '
        b'content="0;url=https://www.amazon.eg/dp/B0CH8DKDKN?tag=their-21">'
    )

    request = Mock(return_value=response)
    monkeypatch.setattr(links.urllib.request, "urlopen", request)
    monkeypatch.setattr(links, "_RESOLVE_CACHE", {})

    assert links.resolve_amazon_url("https://3rrood.com/PHVVsLyN") == (
        "https://www.amazon.eg/dp/B0CH8DKDKN?tag=their-21"
    )
    assert request.call_count == 1
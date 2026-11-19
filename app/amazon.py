"""Amazon Creators API ingestion.

The bot ships Telegram-only by default (`AMAZON_ENABLED=0`). Once the
Associates account has Creators API access, set `AMAZON_ENABLED=1` and this
module polls `SearchItems` for the configured keywords and stores qualifying
deals exactly like Telegram posts.

API docs: https://affiliate-program.amazon.com/creatorsapi/docs

Only the OAuth 2.0 credential versions 3.x are supported (3.1 North America,
3.2 Europe, 3.3 Far East); older 2.x Cognito credentials need to be
regenerated in Associates Central.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db, filters
from .config import AMAZON_CREDENTIAL_VERSIONS, Config
from .ingest import make_content_hash
from .models import SOURCE_AMAZON, Offer
from .runtime import notify_error
from .utils import now_iso

log = logging.getLogger(__name__)

CATALOG_BASE_URL = "https://creatorsapi.amazon"
SEARCH_PATH = "/catalog/v1/searchItems"
GET_ITEMS_PATH = "/catalog/v1/getItems"

AMAZON_SOURCE_CHANNEL = "amazon"

# Only request what the broadcaster needs; extra resources waste quota.
_RESOURCES = [
    "images.primary.large",
    "images.primary.medium",
    "itemInfo.title",
    "offersV2.listings.price",
    "offersV2.listings.condition",
    "offersV2.listings.availability",
    "offersV2.listings.isBuyBoxWinner",
]

# (method, url, headers, body) -> (status, raw body)
Transport = Callable[
    [str, str, Mapping[str, str], Optional[bytes]], tuple[int, bytes]
]


class AmazonError(Exception):
    """Network, authentication or API failure."""


class SkipItem(Exception):
    """The item is not a postable deal (price, discount, stock, ...)."""


# ------------------------------------------------------------------ transport
def _snippet(payload: bytes, limit: int = 300) -> str:
    text = payload.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _urlopen(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise AmazonError(
            f"network error contacting {urlsplit(url).netloc}: {exc.reason}"
        ) from exc


def _parse_json(payload: bytes, context: str) -> dict:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AmazonError(
            f"{context}: invalid JSON response ({_snippet(payload)})"
        ) from exc
    if not isinstance(data, dict):
        raise AmazonError(f"{context}: unexpected response shape")
    return data


# -------------------------------------------------------------------- client
class CreatorsClient:
    """Small synchronous Creators API client with cached OAuth tokens."""

    def __init__(
        self,
        credential_id: str,
        credential_secret: str,
        partner_tag: str,
        *,
        marketplace: str = "www.amazon.com",
        credential_version: str = "3.1",
        timeout: float = 30.0,
        transport: Optional[Transport] = None,
    ) -> None:
        if credential_version not in AMAZON_CREDENTIAL_VERSIONS:
            raise AmazonError(
                f"unsupported credential version {credential_version!r}; "
                f"expected one of {', '.join(sorted(AMAZON_CREDENTIAL_VERSIONS))}"
            )
        self.credential_id = credential_id
        self.credential_secret = credential_secret
        self.partner_tag = partner_tag
        self.marketplace = marketplace
        self.credential_version = credential_version
        self.timeout = timeout
        self.token_url = AMAZON_CREDENTIAL_VERSIONS[credential_version]
        self._access_token: Optional[str] = None
        self._token_expires_at = 0.0

        if transport is not None:
            self._transport = transport
        else:

            def _transport(
                method: str, url: str, headers: Mapping[str, str], body: Optional[bytes]
            ) -> tuple[int, bytes]:
                return _urlopen(method, url, headers, body, timeout)

            self._transport = _transport

    # ------------------------------------------------------------- auth
    def _token(self, *, force: bool = False) -> str:
        if (
            not force
            and self._access_token
            and time.monotonic() < self._token_expires_at
        ):
            return self._access_token

        body = json.dumps(
            {
                "grant_type": "client_credentials",
                "client_id": self.credential_id,
                "client_secret": self.credential_secret,
                "scope": "creatorsapi::default",
            }
        ).encode("utf-8")
        status, payload = self._transport(
            "POST",
            self.token_url,
            {"Content-Type": "application/json", "Accept": "application/json"},
            body,
        )
        if status != 200:
            raise AmazonError(
                f"token request failed (HTTP {status}): {_snippet(payload)}"
            )

        data = _parse_json(payload, "token")
        token = data.get("access_token")
        if not token or not isinstance(token, str):
            raise AmazonError("token response has no access_token")
        try:
            expires_in = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600

        self._access_token = token
        # Refresh a minute early; never cache for less than a minute.
        self._token_expires_at = time.monotonic() + max(60, expires_in - 60)
        return token

    # ---------------------------------------------------------- catalog
    def _catalog(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        for attempt in (0, 1):
            token = self._token(force=attempt > 0)
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "x-marketplace": self.marketplace,
            }
            status, raw = self._transport(
                "POST", CATALOG_BASE_URL + path, headers, body
            )
            if status == 401 and attempt == 0:
                # Expired or revoked token: drop it and retry once.
                log.info("amazon token rejected; refreshing")
                continue
            if status != 200:
                raise AmazonError(
                    f"{path} failed (HTTP {status}): {_snippet(raw)}"
                )
            return _parse_json(raw, path)
        raise AmazonError(f"{path}: authentication failed after token refresh")

    def search_items(
        self,
        keywords: str,
        *,
        item_count: int = 10,
        min_saving_percent: Optional[float] = None,
        search_index: Optional[str] = None,
    ) -> list[dict]:
        """Return raw item dicts for one keyword search (max 10 per call)."""
        payload: dict[str, object] = {
            "keywords": keywords,
            "partnerTag": self.partner_tag,
            "marketplace": self.marketplace,
            "itemCount": max(1, min(10, int(item_count))),
            "resources": list(_RESOURCES),
        }
        if min_saving_percent is not None and min_saving_percent > 0:
            # The API requires a positive integer below 100.
            payload["minSavingPercent"] = max(
                1, min(99, int(min_saving_percent))
            )
        if search_index:
            payload["searchIndex"] = search_index

        data = self._catalog(SEARCH_PATH, payload)
        for error in data.get("errors") or []:
            log.warning("amazon search warning: %s", error)
        result = data.get("searchResult") or {}
        items = result.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def get_items(self, asins: list[str]) -> list[dict]:
        """Look up specific ASINs (max 10); used for manual checks."""
        payload = {
            "itemIds": [asin for asin in asins if asin][:10],
            "itemIdType": "ASIN",
            "partnerTag": self.partner_tag,
            "marketplace": self.marketplace,
            "resources": list(_RESOURCES),
        }
        data = self._catalog(GET_ITEMS_PATH, payload)
        for error in data.get("errors") or []:
            log.warning("amazon item warning: %s", error)
        result = data.get("itemResults") or {}
        items = result.get("items") or []
        return [item for item in items if isinstance(item, dict)]


def client_from_config(config: Config) -> CreatorsClient:
    config.require_amazon()
    return CreatorsClient(
        config.amazon_credential_id or "",
        config.amazon_credential_secret or "",
        config.amazon_associate_tag or "",
        marketplace=config.amazon_marketplace,
        credential_version=config.amazon_credential_version,
    )


# ------------------------------------------------------------------- mapping
def _to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _listing_price(listing: dict) -> tuple[Optional[float], str]:
    price = listing.get("price") or {}
    money = price.get("money") or {}
    amount = _to_float(money.get("amount"))
    currency = str(money.get("currency") or "USD").upper()
    if amount is None or amount <= 0:
        return None, currency
    return amount, currency


def _listing_score(listing: dict) -> tuple[int, int, int]:
    condition = str((listing.get("condition") or {}).get("value") or "")
    availability = str((listing.get("availability") or {}).get("type") or "")
    return (
        1 if listing.get("isBuyBoxWinner") is True else 0,
        1 if condition.lower() == "new" else 0,
        1 if availability.upper() in ("", "IN_STOCK") else 0,
    )


def _pick_listing(item: dict) -> dict:
    listings = (item.get("offersV2") or {}).get("listings") or []
    usable = []
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        amount, _currency = _listing_price(listing)
        if amount is None:
            continue
        availability = str(
            (listing.get("availability") or {}).get("type") or ""
        ).upper()
        if availability == "OUT_OF_STOCK":
            continue
        usable.append(listing)
    if not usable:
        raise SkipItem("no priced, in-stock listing")
    return max(usable, key=_listing_score)


def _best_image(item: dict) -> Optional[str]:
    primary = (item.get("images") or {}).get("primary") or {}
    for size in ("large", "medium", "small"):
        url = (primary.get(size) or {}).get("url")
        if url:
            return str(url)
    return None


def _with_partner_tag(url: str, partner_tag: str) -> str:
    """Force our affiliate tag onto the product link."""
    if not partner_tag:
        return url
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "tag"
    ]
    query.append(("tag", partner_tag))
    return urlunsplit(
        (
            parts.scheme or "https",
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


def item_to_offer(
    item: dict,
    *,
    partner_tag: str,
    marketplace: str,
    min_discount_pct: float = 0.0,
    category: Optional[str] = None,
) -> Offer:
    """Convert one Creators API item into an Offer or raise SkipItem."""
    title = str(
        (item.get("itemInfo") or {}).get("title", {}).get("displayValue") or ""
    ).strip()
    if not title:
        raise SkipItem("missing title")

    listing = _pick_listing(item)
    price, currency = _listing_price(listing)
    if price is None:  # pragma: no cover - _pick_listing guarantees a price
        raise SkipItem("missing price")

    price_node = listing.get("price") or {}
    old_price = _to_float(
        ((price_node.get("savingBasis") or {}).get("money") or {}).get("amount")
    )
    if old_price is not None and old_price <= price * 1.02:
        old_price = None

    discount = _to_float((price_node.get("savings") or {}).get("percentage"))
    if discount is None and old_price is not None:
        discount = round((1 - price / old_price) * 100, 1)
    if discount is not None:
        discount = round(discount, 1)
        if min_discount_pct and discount < min_discount_pct:
            raise SkipItem(
                f"discount {discount:g}% below AMAZON_MIN_DISCOUNT_PCT "
                f"{min_discount_pct:g}%"
            )

    asin = str(item.get("asin") or "").strip()
    link = str(item.get("detailPageURL") or "").strip()
    if not link:
        if not asin:
            raise SkipItem("missing product link")
        link = f"https://{marketplace}/dp/{asin}"
    link = _with_partner_tag(link, partner_tag)

    return Offer(
        title=title,
        link=link,
        price=round(price, 2),
        old_price=round(old_price, 2) if old_price is not None else None,
        discount_pct=discount,
        currency=currency,
        image_url=_best_image(item),
        category=category,
        source_type=SOURCE_AMAZON,
        source_channel=AMAZON_SOURCE_CHANNEL,
        content_hash=make_content_hash(title, link, price),
    )


# -------------------------------------------------------------------- poller
@dataclass(slots=True)
class CollectionResult:
    offers: list[Offer] = field(default_factory=list)
    searches: int = 0
    items: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PollResult:
    enabled: bool = False
    searches: int = 0
    items: int = 0
    skipped: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


def collect_offers(
    config: Config,
    client: CreatorsClient,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectionResult:
    """Run one SearchItems request per keyword and normalize the results."""
    result = CollectionResult()
    keywords = config.amazon_keywords
    for index, keyword in enumerate(keywords):
        try:
            items = client.search_items(
                keyword,
                item_count=config.amazon_item_count,
                min_saving_percent=config.amazon_min_discount_pct,
            )
        except AmazonError as exc:
            message = f"{keyword}: {exc}"
            result.errors.append(message)
            log.error("amazon search %r failed: %s", keyword, exc)
            continue

        result.searches += 1
        result.items += len(items)
        for item in items:
            try:
                offer = item_to_offer(
                    item,
                    partner_tag=client.partner_tag,
                    marketplace=client.marketplace,
                    min_discount_pct=config.amazon_min_discount_pct,
                    category=keyword,
                )
            except SkipItem as exc:
                result.skipped += 1
                log.info(
                    "amazon skip %s (%s): %s",
                    item.get("asin", "?"),
                    keyword,
                    exc,
                )
                continue
            result.offers.append(offer)

        if config.amazon_request_delay_seconds and index < len(keywords) - 1:
            sleep(config.amazon_request_delay_seconds)
    return result


def poll(
    config: Config,
    conn,
    *,
    client: Optional[CreatorsClient] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PollResult:
    """Fetch and persist one round of Amazon deals.

    Returns `enabled=False` without touching the network when the feature is
    off, which is the default Telegram-only launch branch.
    """
    if not config.amazon_enabled:
        log.debug("amazon poll skipped: AMAZON_ENABLED=0")
        return PollResult(enabled=False)

    config.require_amazon()
    if client is None:
        client = client_from_config(config)

    collection = collect_offers(config, client, sleep=sleep)
    result = PollResult(
        enabled=True,
        searches=collection.searches,
        items=collection.items,
        skipped=collection.skipped,
        errors=list(collection.errors),
    )
    for offer in collection.offers:
        decision = filters.evaluate_offer(conn, offer, config)
        offer_id = filters.save_offer(conn, offer, decision)
        if offer_id is None:
            result.duplicates += 1
            log.info("amazon duplicate skipped: %s", offer.title)
            continue
        if not decision.accepted:
            result.rejected += 1
            log.info(
                "amazon offer #%d rejected: %s", offer_id, decision.detail
            )
            continue
        result.stored += 1
        log.info(
            "stored amazon #%d %s | %s %s | -%s%%",
            offer_id,
            offer.title,
            offer.price,
            offer.currency,
            offer.discount_pct if offer.discount_pct is not None else "?",
        )

    if collection.searches:
        db.set_state(conn, "last_amazon_poll", now_iso())
    return result


def run_forever(config: Config, conn=None) -> None:
    """Poll every AMAZON_POLL_INTERVAL_MINUTES until interrupted."""
    config.require_amazon()
    own_conn = conn is None
    connection = conn or db.init_db(config.db_path)
    try:
        while True:
            if db.is_paused(connection):
                log.info("amazon poller idle: bot is paused")
            else:
                try:
                    result = poll(config, connection)
                    log.info(
                        "amazon poll: %d search(es), %d item(s), %d new, "
                        "%d duplicate(s), %d filtered, %d skipped, "
                        "%d error(s)",
                        result.searches,
                        result.items,
                        result.stored,
                        result.duplicates,
                        result.rejected,
                        result.skipped,
                        len(result.errors),
                    )
                except Exception as exc:  # noqa: BLE001 - keep the poller alive
                    log.exception("amazon poll failed")
                    asyncio.run(notify_error(config, "amazon", exc))
            time.sleep(max(60, config.amazon_poll_interval_minutes * 60))
    finally:
        if own_conn:
            connection.close()


__all__ = [
    "AMAZON_SOURCE_CHANNEL",
    "AmazonError",
    "CollectionResult",
    "CreatorsClient",
    "PollResult",
    "SkipItem",
    "client_from_config",
    "collect_offers",
    "item_to_offer",
    "poll",
    "run_forever",
]

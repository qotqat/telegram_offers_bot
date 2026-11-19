"""Bounded, credential-free lookup of titles from resolved Amazon product URLs."""

from __future__ import annotations

import math
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from http.client import HTTPException
from urllib.parse import parse_qsl, urljoin, urlsplit

from .links import extract_asin, is_amazon_link, is_short_amazon_link

__all__ = ["ProductLookupError", "fetch_product_title"]

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_SIZE = 64 * 1024
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ASIN_RE = re.compile(r"[A-Z0-9]{10}", re.ASCII | re.IGNORECASE)
# The link helper also accepts subdomains; this lookup accepts only storefronts.
_STOREFRONT_RE = re.compile(r"amazon\.(?:[a-z]+|(?:co|com)\.[a-z]+)", re.ASCII)


class ProductLookupError(Exception):
    """An invalid URL, rejected response, or unavailable product title."""


def _product_url(url: str, storefront: str | None = None, asin: str | None = None):
    if not isinstance(url, str) or any(ord(c) <= 32 or c == "\\" for c in url):
        raise ProductLookupError("Invalid Amazon product URL")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        if (
            parts.scheme not in {"http", "https"}
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443 if parts.scheme == "https" else 80}
            or not _STOREFRONT_RE.fullmatch(host)
            or not is_amazon_link(url)
            or is_short_amazon_link(url)
        ):
            raise ProductLookupError("URL must use an Amazon storefront host without userinfo")
        # Query ASINs must not override the identity in the actual product path.
        product_asin = extract_asin(parts._replace(query="", fragment="").geturl())
        if not product_asin or not _ASIN_RE.fullmatch(product_asin):
            raise ProductLookupError("URL must contain a valid product-path ASIN")
        product_asin = product_asin.upper()
        for key, value in parse_qsl(parts.query):
            if (
                key.lower() in {"asin", "pd_rd_i", "dib"}
                and _ASIN_RE.fullmatch(value)
                and value.upper() != product_asin
            ):
                raise ProductLookupError("Conflicting ASIN identity in product URL")
    except ValueError as exc:
        raise ProductLookupError("Invalid Amazon product URL") from exc
    if storefront is not None and host != storefront:
        raise ProductLookupError("Response or redirect changed the Amazon storefront")
    if asin is not None and product_asin != asin:
        raise ProductLookupError("Response or redirect changed the product ASIN")
    return f"https://www.{host}/dp/{product_asin}", host, product_asin


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate redirects ourselves, before making any request to their target.
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProductLookupError("Product lookup timed out")
    return remaining


def _read_body(response, deadline: float) -> bytes:
    declared = response.headers.get("Content-Length")
    try:
        length = int(declared) if declared is not None else None
    except ValueError as exc:
        raise ProductLookupError("Invalid response Content-Length") from exc
    if length is not None and not 0 <= length <= _MAX_RESPONSE_BYTES:
        raise ProductLookupError("Product response exceeds the byte limit")

    body = bytearray()
    # HTTPResponse.read1 performs at most one buffered/raw read, so a slow stream
    # cannot reset a socket timeout repeatedly inside a single large read().
    read = response.read1
    while True:
        remaining = _remaining(deadline)
        # urllib exposes no public API for tightening the response socket timeout.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read(min(_READ_SIZE, _MAX_RESPONSE_BYTES + 1 - len(body)))
        _remaining(deadline)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ProductLookupError("Product response exceeds the byte limit")
    if length is not None and len(body) != length:
        raise ProductLookupError("Incomplete product response (Content-Length mismatch)")
    return bytes(body)


class _TitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []
        self.blocked = False
        self._depth = 0
        self._text: list[str] = []
        self._page_title = False
        self._page_text: list[str] = []
        self._ignored: str | None = None
        self._challenge_tail = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self._ignored = tag
        if self._ignored:
            return
        if (
            (attrs.get("id") or "").lower() == "captchacharacters"
            or (tag == "form" and "validatecaptcha" in (attrs.get("action") or "").lower())
        ):
            self.blocked = True
        if tag == "title":
            self._page_title = True
        if tag == "span":
            if self._depth:
                self._depth += 1
            elif attrs.get("id") == "productTitle":
                self._depth = 1
                self._text = []
        if self._depth and tag == "br":
            self._text.append(" ")

    def handle_endtag(self, tag):
        if self._ignored:
            if tag == self._ignored:
                self._ignored = None
            return
        if tag == "title":
            self._page_title = False
        if tag == "span" and self._depth:
            self._depth -= 1
            if not self._depth:
                self.titles.append(" ".join("".join(self._text).split()))

    def handle_data(self, data):
        if self._ignored:
            return
        if self._page_title:
            self._page_text.append(data)
        if self._depth:
            self._text.append(data)
        visible = self._challenge_tail + data
        challenge_text = " ".join(visible.lower().split())
        self._challenge_tail = visible[-256:]
        if any(phrase in challenge_text for phrase in (
            "enter the characters you see below",
            "type the characters you see in this image",
            "automated access to amazon",
        )):
            self.blocked = True

    def product_title(self) -> str:
        page_title = " ".join("".join(self._page_text).lower().split())
        if self.blocked or "captcha" in page_title or "robot check" in page_title:
            raise ProductLookupError("Amazon CAPTCHA/access challenge; lookup stopped")
        if self._depth or len(self.titles) != 1 or not self.titles[0]:
            raise ProductLookupError("Amazon productTitle span is missing, empty, or ambiguous")
        return self.titles[0]


def fetch_product_title(url: str, *, timeout: float = 10.0) -> str:
    """Return the productTitle span, never a copied headline or HTML-title fallback.

    Accept only resolved storefront product URLs (not short links). The first
    request is canonical HTTPS; redirects retain their validated product paths
    without queries or fragments, preserving the original storefront and ASIN.
    The timeout is shared across redirects and reads; urllib socket timeouts also
    apply, though OS DNS resolution cannot be given a strict wall-clock deadline.
    No API credentials, CAPTCHA solving, retries, or failure caching are used.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ProductLookupError("Timeout must be a positive finite number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ProductLookupError("Timeout must be a positive finite number")
    current, storefront, asin = _product_url(url)
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        for redirects in range(_MAX_REDIRECTS + 1):
            request = urllib.request.Request(current, headers={
                "User-Agent": "TelegramOffersBot/1.0 (product title lookup)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
            })
            try:
                response = opener.open(request, timeout=_remaining(deadline))
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                _remaining(deadline)
                status = response.getcode()
                if status != 200 and status not in _REDIRECT_STATUSES:
                    raise ProductLookupError(f"Amazon product request failed: HTTP {status}")
                final = response.geturl()
                _product_url(final, storefront, asin)
                if urlsplit(final).scheme != "https":
                    raise ProductLookupError("Response or redirect downgraded HTTPS")
                if status in _REDIRECT_STATUSES:
                    if redirects == _MAX_REDIRECTS:
                        raise ProductLookupError("Too many Amazon product redirects")
                    location = response.headers.get("Location")
                    if not location or any(ord(c) <= 32 for c in location):
                        raise ProductLookupError("Invalid or missing product redirect Location")
                    target = urljoin(final, location)
                    _product_url(target, storefront, asin)
                    target_parts = urlsplit(target)
                    if target_parts.scheme != "https":
                        raise ProductLookupError("Product redirect downgraded HTTPS")
                    current = target_parts._replace(query="", fragment="").geturl()
                    continue
                if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                    raise ProductLookupError("Product response is not HTML")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ProductLookupError("Unsupported product response Content-Encoding")
                body = _read_body(response, deadline)
                charset = response.headers.get_content_charset() or "utf-8"
                parser = _TitleParser()
                parser.feed(body.decode(charset))
                parser.close()
                title = parser.product_title()
                _remaining(deadline)
                return title
    except (urllib.error.URLError, OSError, HTTPException) as exc:
        raise ProductLookupError("Amazon product request failed (network or protocol error)") from exc
    except (UnicodeError, LookupError, ValueError) as exc:
        raise ProductLookupError("Invalid product HTML or character encoding") from exc
    raise ProductLookupError("No Amazon product response")

"""Amazon link detection, affiliate rewriting and copied-post cleanup.

Source channels often post short Amazon links (``link.amazon``, ``amzn.to``)
that redirect to a product page carrying *their* affiliate tag. We resolve the
short link, keep the ASIN, rebuild the canonical product URL for our configured
marketplace, and attach our own partner tag. The copied text is then cleaned of
the source channel's username/display name and branded as ours.
"""

import html
import ipaddress
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

log = logging.getLogger(__name__)

# Amazon storefront domains (without the leading "www.").
_AMAZON_DOMAINS = (
    "amazon.com",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.it",
    "amazon.es",
    "amazon.nl",
    "amazon.com.be",
    "amazon.pl",
    "amazon.se",
    "amazon.com.tr",
    "amazon.ae",
    "amazon.sa",
    "amazon.eg",
    "amazon.in",
    "amazon.ca",
    "amazon.com.mx",
    "amazon.com.br",
    "amazon.com.au",
    "amazon.sg",
    "amazon.co.jp",
)

# Amazon-operated short-link domains.
_SHORT_HOSTS = {
    "amzn.to",
    "amzn.eu",
    "amzn.asia",
    "amzn.com",
    "link.amazon",
    "amzlinks.in",
    "a.co",
}

_URL_RE = re.compile(
    r"https?://[^\s<>\"']+|(?<![\w@./-])(?:[a-z0-9-]+\.)*(?:"
    + "|".join(re.escape(host) for host in (*_AMAZON_DOMAINS, *sorted(_SHORT_HOSTS)))
    + r")(?![\w.-])(?:[/?#][^\s<>\"']*)?",
    re.IGNORECASE,
)
_ASIN_PATH_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN|o/ASIN|product)"
    r"/([A-Z0-9]{10})(?:[/?]|$)",
    re.IGNORECASE,
)
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?\u061f)]}\u00bb\u201d\u2019'\""

# Query keys that carry someone else's attribution or click metadata.
_AFFILIATE_QUERY_KEYS = {
    "tag",
    "ascsubtag",
    "linkcode",
    "linkid",
    "camp",
    "creative",
    "creativeasin",
    "btn_ref",
    "btn_type",
    "pf_rd_p",
    "pf_rd_r",
    "pd_rd_r",
    "pd_rd_w",
    "pd_rd_wg",
}

Resolver = Callable[[str], Optional[str]]
_RESOLVE_CACHE: dict[str, Optional[str]] = {}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

_MAX_REDIRECT_HOPS = 6
_HTML_REDIRECT_BYTES = 64 * 1024
_FOREIGN_LINK_LOOKUP_LIMIT = 5

# Links that cannot be Amazon product redirects.
_FOREIGN_SKIP_HOSTS = {
    "t.me",
    "telegram.me",
    "telegram.dog",
    "instagram.com",
    "facebook.com",
    "fb.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "pinterest.com",
    "snapchat.com",
    "threads.net",
}

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_CONTENT_RE = re.compile(
    r"content\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_META_URL_RE = re.compile(r"url\s*=\s*([^;\"'\s>]+)", re.IGNORECASE)
_JS_HREF_RE = re.compile(
    r"(?:window\.)?location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_JS_CALL_RE = re.compile(
    r"location\.(?:replace|assign)\s*\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

@dataclass(slots=True)
class RewriteResult:
    """Text after Amazon-link and brand cleanup."""

    text: str
    links: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# ------------------------------------------------------------------- detection
def is_amazon_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if any(char in host for char in ":@/\\"):
        return False
    if host.startswith("www."):
        host = host[4:]
    if host in _SHORT_HOSTS:
        return True
    return any(
        host == domain or host.endswith("." + domain)
        for domain in _AMAZON_DOMAINS
    )


def _amazon_parts(url: str):
    try:
        cleaned = clean_url(url)
        if not cleaned:
            return None
        parts = urlsplit(cleaned)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or parts.username is not None
            or parts.password is not None
            or not is_amazon_host(parts.hostname or "")
        ):
            return None
        return parts
    except ValueError:
        return None


def is_amazon_link(url: str) -> bool:
    return _amazon_parts(url) is not None


def is_short_amazon_link(url: str) -> bool:
    parts = _amazon_parts(url)
    if parts is None:
        return False
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host in _SHORT_HOSTS


def post_urls(text: str) -> list[str]:
    """Include bare Amazon URLs that Telegram can turn into clickable links."""
    return [match.group(0).rstrip(_TRAILING_PUNCTUATION) for match in _URL_RE.finditer(text or "")]


def truncate_post_text(text: str, limit: int) -> str:
    """Avoid clipping off an affiliate query while leaving a clickable URL."""
    head = truncate_utf16(text, limit)
    for match in _URL_RE.finditer(text):
        if match.start() < len(head) < match.end():
            return head[:match.start()].rstrip()
    return head


def extract_asin(url: str) -> Optional[str]:
    """Pull an ASIN from a product path or common query parameters."""
    cleaned = clean_url(url)
    if not cleaned:
        return None
    parts = urlsplit(cleaned)
    query_asins = {
        value.upper()
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() in {"asin", "pd_rd_i", "dib"} and _ASIN_RE.fullmatch(value)
    }
    match = _ASIN_PATH_RE.search(parts.path)
    if match:
        path_asin = match.group(1).upper()
        # Never attach one product's source price/photo to another query ASIN.
        return path_asin if not query_asins or query_asins == {path_asin} else None
    return next(iter(query_asins)) if len(query_asins) == 1 else None


# ------------------------------------------------------------------ resolution
def _is_public_http_url(url: str) -> bool:
    """Reject non-http(s) and obvious local/private targets."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"}:
        return False
    if parts.username is not None or parts.password is not None:
        return False

    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    if any(
        host == item or host.endswith("." + item)
        for item in _FOREIGN_SKIP_HOSTS
    ):
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True

    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _meta_refresh_target(text: str) -> Optional[str]:
    for tag in _META_TAG_RE.findall(text):
        lower = tag.lower()
        if "http-equiv" not in lower or "refresh" not in lower:
            continue
        content = _META_CONTENT_RE.search(tag)
        if not content:
            continue
        value = next(
            (group for group in content.groups() if group is not None),
            "",
        )
        match = _META_URL_RE.search(value)
        if match:
            return match.group(1)
    return None


def _js_redirect_target(text: str) -> Optional[str]:
    match = _JS_HREF_RE.search(text) or _JS_CALL_RE.search(text)
    return match.group(1) if match else None


def _html_redirect_target(body: bytes, base: str) -> Optional[str]:
    if not body:
        return None
    text = body.decode("utf-8", "ignore")
    target = _meta_refresh_target(text) or _js_redirect_target(text)
    if not target:
        return None

    target = html.unescape(target.strip().strip("'\""))
    if not target or target.lower().startswith(
        ("javascript:", "data:", "mailto:", "#")
    ):
        return None

    target = urljoin(base, target)
    return target if _is_public_http_url(target) else None
    
def resolve_amazon_url(url: str, *, timeout: float = 10.0) -> Optional[str]:
    """Follow HTTP and HTML redirects and return the final URL, or None."""
    if url in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[url]

    current = clean_url(url)
    if not current or not _is_public_http_url(current):
        return None

    final: Optional[str] = None
    for _ in range(_MAX_REDIRECT_HOPS):
        # A direct Amazon product URL needs no network request.
        if is_amazon_link(current) and not is_short_amazon_link(current):
            final = current
            break

        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,*/*",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                final = response.geturl()
                body = (
                    b""
                    if is_amazon_link(final)
                    else response.read(_HTML_REDIRECT_BYTES)
                )
        except Exception as exc:  # noqa: BLE001 - one bad link must not kill ingestion
            log.warning("could not resolve link %s: %s", current, exc)
            final = None
            break

        if not final or is_amazon_link(final):
            break

        target = _html_redirect_target(body, final)
        if not target or target == current:
            break
        current = target

    if final is not None:
        if len(_RESOLVE_CACHE) > 2048:
            _RESOLVE_CACHE.clear()
        _RESOLVE_CACHE[url] = final
    return final

def resolve_foreign_amazon_links(
    urls: Iterable[str],
    *,
    resolver: Optional[Resolver] = None,
    limit: int = _FOREIGN_LINK_LOOKUP_LIMIT,
) -> dict[str, str]:
    """Resolve non-Amazon links and return ``{original: amazon_url}``.

    Telegram/social links are skipped, and the number of network lookups per
    post is capped. This is what lets a post whose only link is a third-party
    redirect still be ingested as an Amazon deal.
    """
    resolve = resolver or resolve_amazon_url
    found: dict[str, str] = {}
    seen: set[str] = set()
    attempts = 0

    for raw in urls:
        cleaned = clean_url(raw)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)

        if is_amazon_link(cleaned):
            continue
        if not _is_public_http_url(cleaned):
            continue
        if attempts >= limit:
            break

        attempts += 1
        final = resolve(cleaned)
        if final and is_amazon_link(final):
            found[cleaned] = final

    return found


# ------------------------------------------------------------------- affiliate
def _normalized_host(marketplace: str) -> str:
    host = marketplace.lower().strip()
    if host.startswith("https://") or host.startswith("http://"):
        host = urlsplit(host).netloc
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches_marketplace(host: str, marketplace: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    target = _normalized_host(marketplace)
    return bool(target) and (host == target or host.endswith("." + target))


def _with_tag(url: str, partner_tag: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _AFFILIATE_QUERY_KEYS
    ]
    if partner_tag:
        query.append(("tag", partner_tag))
    return urlunsplit(
        (
            parts.scheme or "https",
            parts.netloc,
            parts.path,
            urlencode(query, doseq=True),
            "",
        )
    )


def affiliate_link(
    url: str,
    *,
    marketplace: str,
    partner_tag: str,
    resolver: Optional[Resolver] = None,
) -> Optional[str]:
    """Canonical Amazon link; an empty partner_tag explicitly means no attribution."""
    cleaned = clean_url(url)
    if not cleaned or not is_amazon_link(cleaned):
        return None

    target = cleaned
    if is_short_amazon_link(cleaned):
        target = (resolver or resolve_amazon_url)(cleaned)
        if not target:
            return None
    if not is_amazon_link(target):
        return None

    asin = extract_asin(target)
    if asin:
        product = f"https://{marketplace}/dp/{asin}"
        return product + "?" + urlencode({"tag": partner_tag}) if partner_tag else product

    # Store/search links have no ASIN; only re-tag links already on our store.
    if not _host_matches_marketplace(
        urlsplit(target).hostname or "", marketplace
    ):
        return None
    return _with_tag(target, partner_tag)


def has_owned_amazon_links(
    text: str, primary_link: str, *, marketplace: str, partner_tag: str
) -> bool:
    """Final send-time guard, including rows queued under an older tag."""
    if not partner_tag or not is_amazon_link(primary_link):
        return False
    urls = [primary_link, *post_urls(text)]
    for raw in urls:
        url = raw.rstrip(_TRAILING_PUNCTUATION)
        if not is_amazon_link(url):
            continue
        parts = _amazon_parts(url)
        if parts is None:
            return False
        tags = [value for key, value in parse_qsl(parts.query) if key.lower() == "tag"]
        if (
            is_short_amazon_link(url)
            or not _host_matches_marketplace(parts.hostname or "", marketplace)
            or tags != [partner_tag]
        ):
            return False
    return True


def has_plain_amazon_links(text: str, primary_link: str, *, marketplace: str) -> bool:
    """Only canonical product URLs, with no source attribution or short redirects."""
    if not is_amazon_link(primary_link):
        return False
    for raw in [primary_link, *post_urls(text)]:
        if not is_amazon_link(raw):
            continue
        asin = extract_asin(raw)
        if not asin or raw != f"https://{marketplace}/dp/{asin}":
            return False
    return True


def rewrite_amazon_links(
    text: str,
    *,
    marketplace: str,
    partner_tag: str,
    resolver: Optional[Resolver] = None,
    foreign_links: Optional[Mapping[str, str]] = None,
) -> RewriteResult:
    """Replace every Amazon URL, and mapped foreign redirect, with our link."""
    links: list[str] = []
    failed: list[str] = []
    foreign = foreign_links or {}

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        cleaned = raw.rstrip(_TRAILING_PUNCTUATION)
        trailing = raw[len(cleaned):]

        target = cleaned
        if not is_amazon_link(cleaned):
            target = foreign.get(cleaned) or ""
            if not target:
                return raw

        link = affiliate_link(
            target,
            marketplace=marketplace,
            partner_tag=partner_tag,
            resolver=resolver,
        )
        if link is None:
            failed.append(cleaned)
            return raw

        links.append(link)
        return link + trailing

    return RewriteResult(
        text=_URL_RE.sub(replace, text or ""),
        links=links,
        failed=failed,
    )


# ---------------------------------------------------------------- brand cleanup
def replace_source_names(
    text: str,
    *,
    source_names: Iterable[str] = (),
    source_usernames: Iterable[str] = (),
    brand_name: str,
    brand_username: str,
    brand_url: str,
) -> str:
    """Replace source channel names/links with our brand."""
    text = text or ""

    usernames = []
    for username in source_usernames:
        username = (username or "").strip().lstrip("@")
        if username and username not in usernames:
            usernames.append(username)
    for name in source_names:
        candidate = (name or "").strip().lstrip("@")
        if candidate and candidate not in usernames:
            usernames.append(candidate)

    # Channel links first, otherwise the display-name pass could mangle them.
    for username in sorted(usernames, key=len, reverse=True):
        text = re.sub(
            rf"(?i)(?:https?://)?(?:t|telegram)\.me/{re.escape(username)}"
            r"(?![\w/])",
            brand_url,
            text,
        )
    # Private invite links should not advertise the source channel either.
    text = re.sub(
        r"(?i)(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)[\w-]+",
        brand_url,
        text,
    )
    # @mentions next.
    for username in sorted(usernames, key=len, reverse=True):
        text = re.sub(
            rf"(?i)@{re.escape(username)}(?!\w)",
            brand_username,
            text,
        )
    # Display names last, longest first.
    names = {
        (name or "").strip()
        for name in source_names
        if name and len((name or "").strip()) >= 3
    }
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), brand_name, text, flags=re.IGNORECASE)

    return text


def build_post_text(
    text: str,
    *,
    primary_link: str,
    source_names: Iterable[str] = (),
    source_usernames: Iterable[str] = (),
    marketplace: str,
    partner_tag: str,
    brand_name: str,
    brand_username: str,
    brand_url: str,
    resolver: Optional[Resolver] = None,
    foreign_links: Optional[Mapping[str, str]] = None,
) -> RewriteResult:
    """Rewrite links/names and guarantee the copied text contains our link."""
    rewritten = rewrite_amazon_links(
        text,
        marketplace=marketplace,
        partner_tag=partner_tag,
        resolver=resolver,
        foreign_links=foreign_links,
    )

    final = rewritten.text.strip()
    if primary_link and primary_link not in final:
        final = f"{final}\n\n{primary_link}".strip()

    final = replace_source_names(
        final,
        source_names=source_names,
        source_usernames=source_usernames,
        brand_name=brand_name,
        brand_username=brand_username,
        brand_url=brand_url,
    )
    return RewriteResult(
        text=final,
        links=rewritten.links,
        failed=rewritten.failed,
    )

def fallback_text(offer) -> str:
    """Generate post text for rows without copied text (e.g. Amazon poller)."""
    lines = [offer.title]
    if offer.price is not None:
        price = f"Price: {offer.price:g} {offer.currency}"
        if offer.old_price is not None and offer.old_price > offer.price:
            price += f" (was {offer.old_price:g} {offer.currency}"
            if offer.discount_pct is not None:
                price += f", -{offer.discount_pct:g}%"
            price += ")"
        lines.append(price)
    if offer.link:
        lines.append(offer.link)
    return "\n\n".join(lines)


__all__ = [
    "RewriteResult",
    "affiliate_link",
    "build_post_text",
    "extract_asin",
    "fallback_text",
    "has_owned_amazon_links",
    "has_plain_amazon_links",
    "is_amazon_host",
    "is_amazon_link",
    "is_short_amazon_link",
    "post_urls",
    "resolve_foreign_amazon_links",
    "resolve_amazon_url",
    "rewrite_amazon_links",
    "truncate_post_text",
    "replace_source_names",
]

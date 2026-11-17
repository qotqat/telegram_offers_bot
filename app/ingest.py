"""Turn raw Telegram posts into normalized `Offer` rows.

Everything here is pure (no Telethon or SQLite imports) so the parsing rules
can be tested against real-world deal formats. The listener passes in the post
text and the URLs it found in entities/buttons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import SOURCE_TELEGRAM, Offer
from .utils import normalize_text, sha256_text

DEFAULT_CURRENCY = "USD"


class BuildFailure(Exception):
    """The post does not contain enough information to become an offer."""


# --------------------------------------------------------------------- text
_DIGIT_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫",
    "01234567890123456789,.",
)
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_TITLE_DECORATION = " \t\r\n-–—•·|:：,،.؛;!?/\\()[]{}<>\"'`~*_+=#🔥✅⚡💥🛒👇👉👈⭐✨🎁💯❗❕📣📢🚨⏰💰🏷️🆕🆓"
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_INVITE_RE = re.compile(r"^https?://t\.me/(?:\+|joinchat)", re.IGNORECASE)

_PRODUCT_HOSTS = (
    "amazon.",
    "amzn.",
    "aliexpress.",
    "noon.com",
    "noon.",
    "ebay.",
    "walmart.",
    "target.",
    "bestbuy.",
    "shein.",
    "temu.",
    "jumia.",
    "flipkart.",
    "etsy.",
    "iherb.",
)
_SOCIAL_HOSTS = (
    "instagram.com",
    "facebook.com",
    "fb.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "pinterest.",
    "snapchat.com",
    "threads.net",
)

_TRACKING_PARAM_RE = re.compile(
    r"^(utm_.*|fbclid|gclid|yclid|msclkid|mc_cid|mc_eid|igshid|si|tag|ref|ref_|"
    r"aff|aff_id|affid|affsrc|linkcode|linkid|camp|ascsubtag)$",
    re.IGNORECASE,
)


def normalize_digits(text: str) -> str:
    return _INVISIBLE_RE.sub("", text or "").translate(_DIGIT_TRANSLATION)


def clean_url(url: str) -> Optional[str]:
    url = (url or "").strip().rstrip(".,;:!?)]}\u00bb\u201d\u2019'\"")
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        if "." not in url.split("/")[0]:
            return None
        url = "https://" + url
    if not urlsplit(url).netloc:
        return None
    return url


def canonicalize_url(url: str) -> str:
    """Stable form used for hashing: no tracking params, sorted query, no www."""
    cleaned = clean_url(url)
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [
        segment
        for segment in parts.path.split("/")
        if segment and not segment.lower().startswith("ref=")
    ]
    path = "/" + "/".join(segments) if segments else ""
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key and not _TRACKING_PARAM_RE.match(key)
    ]
    query.sort(key=lambda item: (item[0], item[1]))
    return urlunsplit(("https", host, path, urlencode(query, doseq=True), ""))


def pick_deal_url(urls: Iterable[str]) -> Optional[str]:
    """Choose the most likely buy link; skip channel invites."""
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in urls:
        url = clean_url(raw)
        if not url or _INVITE_RE.match(url):
            continue
        canonical = canonicalize_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        host = urlsplit(url).netloc.lower()
        if any(host == item or host.endswith("." + item) for item in ("t.me", "telegram.me")):
            score = 30
        elif any(marker in host for marker in _PRODUCT_HOSTS):
            score = 100
        elif any(marker in host for marker in _SOCIAL_HOSTS):
            score = 20
        else:
            score = 50
        candidates.append((score, url))

    if not candidates:
        return None
    # Stable sort keeps the author's order among equally good links.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


# -------------------------------------------------------------------- prices
@dataclass(slots=True)
class PriceInfo:
    price: Optional[float] = None
    old_price: Optional[float] = None
    discount_pct: Optional[float] = None
    currency: str = DEFAULT_CURRENCY


_NUMBER_RE = re.compile(
    r"\d{1,3}(?:[.,\u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?"  # grouped: 1,299.00
    r"|\d+(?:[.,]\d{1,2})?"  # plain: 1299 / 19.99
)

_CURRENCY_RE = re.compile(
    r"US\$|\$|€|£|﷼|ر\.س|د\.إ|ج\.م|د\.ك|ر\.ق|"
    r"\b(?:USD|EUR|GBP|SAR|AED|EGP|KWD|QAR)\b|دولار|يورو|ريال|درهم|جنيه|دينار",
    re.IGNORECASE,
)

# A currency marker only counts when it is adjacent to the number, otherwise
# "XM5 — $299" would read the model number as a $5 price.
_CURRENCY_TOKEN = (
    r"(US\$|\$|€|£|﷼|ر\.س|د\.إ|ج\.م|د\.ك|ر\.ق|"
    r"\b(?:USD|EUR|GBP|SAR|AED|EGP|KWD|QAR)\b|دولار|يورو|ريال|درهم|جنيه|دينار)"
)
_BEFORE_CURRENCY_RE = re.compile(_CURRENCY_TOKEN + r"\s*[:：]?\s*$", re.IGNORECASE)
_AFTER_CURRENCY_RE = re.compile(r"^\s*[:：]?\s*" + _CURRENCY_TOKEN, re.IGNORECASE)
_CURRENCY_MAP = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "دولار": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "يورو": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "﷼": "SAR",
    "ر.س": "SAR",
    "SAR": "SAR",
    "ريال": "SAR",
    "د.إ": "AED",
    "AED": "AED",
    "درهم": "AED",
    "ج.م": "EGP",
    "EGP": "EGP",
    "جنيه": "EGP",
    "د.ك": "KWD",
    "KWD": "KWD",
    "دينار": "KWD",
    "ر.ق": "QAR",
    "QAR": "QAR",
}
_PRICE_WORD_RE = re.compile(
    r"(?:السعر|سعر|price|only|فقط|الآن|now)\s*[:：]?\s*$", re.IGNORECASE
)
_OLD_PRICE_RE = re.compile(
    r"(?:was|instead\s+of|reg\.?|regular|list\s+price|msrp|rrp|before|orig(?:inal)?|"
    r"statt|au\s+lieu\s+de|من|بدل|بدلاً|سابق|قبل|كان)\s*[:：]?\s*$",
    re.IGNORECASE,
)
_NON_PRODUCT_PRICE_RE = re.compile(
    r"(?:payments?|installments?|instalments?|per\s+month|monthly|/mo|"
    r"shipping|delivery|أقساط|قسط|شهري|شحن|توصيل)"
    r"(?:\s+of)?\s*[:：]?\s*$",
    re.IGNORECASE,
)
_NON_PRODUCT_AFTER_RE = re.compile(
    r"^\s*(?:shipping|delivery|for\s+shipping|شحن|توصيل)", re.IGNORECASE
)
_DISCOUNT_RE = re.compile(
    r"(\d{1,3})\s*(?:%|٪|percent|بالمئة|في\s+المئة)", re.IGNORECASE
)
_TRAILING_CURRENCY_RE = re.compile(
    r"(?:US\$|\$|€|£|ر\.س|د\.إ|ج\.م|د\.ك|ر\.ق|\s)+$", re.IGNORECASE
)


def _context_before(before: str) -> str:
    """Text before a price with any trailing currency marker removed."""
    return _TRAILING_CURRENCY_RE.sub("", before)


def parse_number(token: str) -> Optional[float]:
    text = (
        token.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("٬", ",")
        .replace("٫", ".")
    )
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            text = f"{parts[0]}.{parts[1]}"
        else:
            text = "".join(parts)
    elif "." in text:
        parts = text.split(".")
        if len(parts) > 2:
            text = "".join(parts)
        elif len(parts[0]) <= 3 and len(parts[1]) == 3:
            # "1.299" / "299.000" are almost always thousands separators.
            text = "".join(parts)
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _currency_near(text: str, start: int, end: int) -> Optional[str]:
    after = text[end : end + 14]
    before = text[max(0, start - 14) : start]
    match = _AFTER_CURRENCY_RE.match(after) or _BEFORE_CURRENCY_RE.search(before)
    if not match:
        return None
    marker = match.group(1)
    return (
        _CURRENCY_MAP.get(marker.upper())
        or _CURRENCY_MAP.get(marker)
        or DEFAULT_CURRENCY
    )


def parse_pricing(text: str) -> PriceInfo:
    text = normalize_digits(text)

    stated_discount: Optional[float] = None
    for match in _DISCOUNT_RE.finditer(text):
        value = float(match.group(1))
        if 5 <= value <= 99:
            stated_discount = value
            break

    candidates: list[tuple[float, bool, str]] = []  # (value, is_old, currency)
    for match in _NUMBER_RE.finditer(text):
        value = parse_number(match.group(0))
        if value is None or value > 1_000_000:
            continue
        before = text[max(0, match.start() - 22) : match.start()]
        after = text[match.end() : match.end() + 14]
        context = _context_before(before)
        currency = _currency_near(text, match.start(), match.end())
        has_price_word = bool(_PRICE_WORD_RE.search(context))
        if not currency and not has_price_word:
            continue  # "iPhone 15", sizes, model numbers, ...
        if _NON_PRODUCT_PRICE_RE.search(context) or _NON_PRODUCT_AFTER_RE.search(after):
            continue  # installments, monthly payments, shipping fees
        is_old = bool(_OLD_PRICE_RE.search(context))
        candidates.append((value, is_old, currency or DEFAULT_CURRENCY))

    if not candidates:
        return PriceInfo()

    old_marked = [item for item in candidates if item[1]]
    normal = [item for item in candidates if not item[1]]

    price: Optional[float] = None
    old_price: Optional[float] = None
    currency = candidates[0][2]

    def use(pair: tuple[float, bool, str], as_old: bool) -> None:
        nonlocal price, old_price, currency
        if as_old:
            old_price = pair[0]
        else:
            price = pair[0]
            currency = pair[2]

    if len(candidates) >= 2:
        # Two prices: cheapest is the deal, highest is the reference price.
        price = min(value for value, _, _ in candidates)
        old_price = max(value for value, _, _ in candidates)
        price_entry = next(item for item in candidates if item[0] == price)
        currency = price_entry[2]
        if old_price <= price * 1.02:
            old_price = None
    elif old_marked:
        use(old_marked[0], True)
        price = None
    elif normal:
        use(normal[0], False)

    if price is None:
        # Only a "was" price: nothing to advertise.
        return PriceInfo(currency=currency)

    if old_price is None and stated_discount is not None and 10 <= stated_discount <= 95:
        old_price = round(price / (1 - stated_discount / 100), 2)

    discount: Optional[float] = None
    if old_price is not None and old_price > price:
        computed = (1 - price / old_price) * 100
        if 1 <= computed <= 99.9:
            discount = round(computed, 1)
    if discount is None and stated_discount is not None:
        discount = stated_discount

    return PriceInfo(
        price=round(price, 2),
        old_price=round(old_price, 2) if old_price else None,
        discount_pct=discount,
        currency=currency if len(currency) <= 3 else DEFAULT_CURRENCY,
    )


# --------------------------------------------------------------------- title
def _clean_title_line(line: str) -> str:
    line = _INVISIBLE_RE.sub("", line)
    line = re.sub(r"\s+", " ", line).strip()
    previous = None
    while line and line != previous:
        previous = line
        line = line.strip().lstrip(_TITLE_DECORATION).rstrip(_TITLE_DECORATION).strip()
    return line


def extract_title(text: str, urls: Iterable[str] = ()) -> str:
    cleaned = normalize_digits(text)
    for url in urls:
        if url:
            cleaned = cleaned.replace(url, " ")
    cleaned = _URL_IN_TEXT_RE.sub(" ", cleaned)

    for raw_line in cleaned.splitlines():
        line = _clean_title_line(raw_line)
        if len(line) < 3:
            continue
        # Skip lines that are only a price (with or without currency).
        without_currency = _CURRENCY_RE.sub(" ", line).strip()
        if _NUMBER_RE.fullmatch(without_currency) or _NUMBER_RE.fullmatch(line):
            continue
        if len(re.sub(r"[\W_]+", "", line)) < 2:
            continue
        return _truncate(line, 180)
    raise BuildFailure("no usable title")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]).rstrip() + "…"


# --------------------------------------------------------------------- offer
def make_content_hash(title: str, link: str, price: Optional[float]) -> str:
    price_part = f"{price:.2f}" if price is not None else ""
    return sha256_text(normalize_text(title), canonicalize_url(link), price_part)


def build_offer(
    text: str,
    urls: Iterable[str],
    *,
    source_channel: str,
    source_msg_id: Optional[int],
    category: Optional[str] = None,
) -> Offer:
    """Build an Offer from a post or raise BuildFailure with the reason."""
    url_list = [url for url in urls if url]
    if not url_list:
        url_list = _URL_IN_TEXT_RE.findall(text or "")

    link = pick_deal_url(url_list)
    if not link:
        raise BuildFailure("no usable deal link")

    title = extract_title(text or "", url_list)
    pricing = parse_pricing(text or "")

    return Offer(
        title=title,
        link=link,
        price=pricing.price,
        old_price=pricing.old_price,
        discount_pct=pricing.discount_pct,
        currency=pricing.currency,
        source_type=SOURCE_TELEGRAM,
        source_channel=source_channel,
        source_msg_id=source_msg_id,
        category=category,
        content_hash=make_content_hash(title, link, pricing.price),
    )

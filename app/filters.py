"""Post-ingestion filters: exclusions, discount, fuzzy dedupe, source cap.

Every new offer passes through this module before it becomes eligible for the
broadcaster. Survivors keep ``status='pending'`` (auto-approved); rejected
offers are still stored with ``status='rejected'`` for auditing and to reserve
their exact content hash.

Fuzzy dedupe follows the launch plan: compare against the last 500 accepted
offers, only within a +/-10% price bucket, and reject at a title similarity of
``FUZZY_THRESHOLD`` (default 88). A canonical-link match is also a duplicate
inside the same price bucket, which lets a genuine price drop (outside the
bucket) post as a new deal.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from rapidfuzz import fuzz

from . import db
from .config import Config
from .ingest import canonicalize_url, normalize_digits
from .models import STATUS_REJECTED, Offer, StoredOffer
from .utils import hours_ago_iso, normalize_text

# Plan: last 500 offers, same price bucket +/-10%, ratio >= 88.
FUZZY_WINDOW = 500
PRICE_BUCKET_TOLERANCE = 0.10

REASON_ACCEPTED = "accepted"
REASON_EXCLUDED_KEYWORD = "excluded_keyword"
REASON_LOW_DISCOUNT = "low_discount"
REASON_NEAR_DUPLICATE = "near_duplicate"
REASON_SOURCE_CAP = "source_cap"

_ARABIC_DIACRITICS_RE = re.compile(
    r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]"
)
_NON_TITLE_RE = re.compile(r"[^\w]+", re.UNICODE)
_ARABIC_FOLD = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ى": "ي",
        "ئ": "ي",
        "ؤ": "و",
        "ة": "ه",
    }
)


@dataclass(slots=True)
class FilterDecision:
    """Outcome of the filter pipeline for one offer."""

    accepted: bool
    reason: str = REASON_ACCEPTED
    detail: str = ""
    duplicate_of: Optional[int] = None
    score: Optional[float] = None


@dataclass(slots=True)
class DuplicateMatch:
    """A previously accepted offer that this one duplicates."""

    offer_id: int
    title: str
    kind: str  # "link" | "title"
    score: float


# ------------------------------------------------------------------ title prep
def normalize_title(text: str) -> str:
    """Normalize titles for fuzzy comparison (case, digits, Arabic, symbols)."""
    text = normalize_digits(text or "")
    text = _ARABIC_DIACRITICS_RE.sub("", text)
    text = text.translate(_ARABIC_FOLD)
    text = text.replace("_", " ")
    text = _NON_TITLE_RE.sub(" ", text)
    return normalize_text(text)


def same_price_bucket(
    price: Optional[float],
    other: Optional[float],
    tolerance: float = PRICE_BUCKET_TOLERANCE,
) -> bool:
    """True when both prices are known and within +/-``tolerance``.

    Offers with unknown prices only compare with other unknown prices, so a
    priced deal is never collapsed into a text-only post (and vice versa).
    """
    if price is None or other is None:
        return price is None and other is None
    if price <= 0 or other <= 0:
        return False
    return abs(price - other) <= max(price, other) * tolerance


def _same_currency(left: str, right: str) -> bool:
    return (left or "USD").upper() == (right or "USD").upper()


# ------------------------------------------------------------------- matching
def find_duplicate(
    offer: Offer,
    recent: Sequence[StoredOffer],
    *,
    threshold: float,
) -> Optional[DuplicateMatch]:
    """Return the best duplicate in ``recent`` or None.

    Canonical links win outright. Titles are compared only when the currency
    and the +/-10% price bucket match, which keeps different products with
    similar names (and genuine price drops) out of the duplicate net.
    """
    canonical = canonicalize_url(offer.link)
    title = normalize_title(offer.title)
    best: Optional[DuplicateMatch] = None

    for candidate in recent:
        if candidate.status == STATUS_REJECTED:
            continue
        if not _same_currency(offer.currency, candidate.currency):
            continue
        if not same_price_bucket(offer.price, candidate.price):
            continue

        if canonical and canonical == canonicalize_url(candidate.link):
            return DuplicateMatch(
                offer_id=candidate.id,
                title=candidate.title,
                kind="link",
                score=100.0,
            )

        if not title:
            continue
        candidate_title = normalize_title(candidate.title)
        if not candidate_title:
            continue
        score = float(fuzz.ratio(title, candidate_title))
        if score >= threshold and (best is None or score > best.score):
            best = DuplicateMatch(
                offer_id=candidate.id,
                title=candidate.title,
                kind="title",
                score=score,
            )
    return best


def _keyword_hit(title: str, keywords: Iterable[str]) -> Optional[str]:
    normalized = normalize_title(title)
    for keyword in keywords:
        needle = normalize_title(keyword)
        if needle and needle in normalized:
            return keyword
    return None


# ------------------------------------------------------------------ decisions
def evaluate_offer(
    conn: sqlite3.Connection,
    offer: Offer,
    config: Config,
    *,
    recent: Optional[Sequence[StoredOffer]] = None,
    window: int = FUZZY_WINDOW,
) -> FilterDecision:
    """Run every pre-queue filter. Accepted means auto-approved pending."""
    keyword = _keyword_hit(offer.title, config.exclude_keywords)
    if keyword:
        return FilterDecision(
            accepted=False,
            reason=REASON_EXCLUDED_KEYWORD,
            detail=f"excluded keyword {keyword!r}",
        )

    if (
        offer.discount_pct is not None
        and offer.discount_pct < config.min_discount_pct
    ):
        return FilterDecision(
            accepted=False,
            reason=REASON_LOW_DISCOUNT,
            detail=(
                f"discount {offer.discount_pct:g}% is below "
                f"MIN_DISCOUNT_PCT {config.min_discount_pct:g}%"
            ),
        )

    if recent is None:
        recent = db.recent_offers(
            conn, limit=window, include_rejected=False
        )
    duplicate = find_duplicate(
        offer, recent, threshold=config.fuzzy_threshold
    )
    if duplicate is not None:
        return FilterDecision(
            accepted=False,
            reason=REASON_NEAR_DUPLICATE,
            detail=(
                f"{duplicate.kind} duplicate of offer #{duplicate.offer_id} "
                f"({duplicate.score:.1f}): {duplicate.title!r}"
            ),
            duplicate_of=duplicate.offer_id,
            score=duplicate.score,
        )

    if offer.source_channel:
        accepted_from_source = db.count_offers_since(
            conn,
            hours_ago_iso(1),
            source_channel=offer.source_channel,
        )
        if accepted_from_source >= config.max_per_source_per_hour:
            return FilterDecision(
                accepted=False,
                reason=REASON_SOURCE_CAP,
                detail=(
                    f"{offer.source_channel} already has "
                    f"{accepted_from_source} accepted offer(s) in the last "
                    f"hour (MAX_PER_SOURCE_PER_HOUR="
                    f"{config.max_per_source_per_hour})"
                ),
            )

    return FilterDecision(accepted=True)


def save_offer(
    conn: sqlite3.Connection,
    offer: Offer,
    decision: FilterDecision,
) -> Optional[int]:
    """Persist an offer, marking it rejected when the decision failed.

    Returns the new row id, or None when the exact content hash already
    exists (another source posted the same title/link/price).
    """
    offer_id = db.insert_offer(conn, offer)
    if offer_id is None:
        return None
    if not decision.accepted:
        db.mark_offer_rejected(conn, offer_id)
    return offer_id


__all__ = [
    "FUZZY_WINDOW",
    "PRICE_BUCKET_TOLERANCE",
    "REASON_ACCEPTED",
    "REASON_EXCLUDED_KEYWORD",
    "REASON_LOW_DISCOUNT",
    "REASON_NEAR_DUPLICATE",
    "REASON_SOURCE_CAP",
    "DuplicateMatch",
    "FilterDecision",
    "evaluate_offer",
    "find_duplicate",
    "normalize_title",
    "same_price_bucket",
    "save_offer",
]

"""Data models shared across listener, filter pipeline and broadcaster."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SOURCE_AMAZON = "amazon"
SOURCE_TELEGRAM = "telegram"

STATUS_PENDING = "pending"
STATUS_POSTED = "posted"
STATUS_REJECTED = "rejected"


@dataclass(slots=True)
class Offer:
    """A normalized deal ready for dedupe / filtering / queuing."""

    title: str
    link: str
    source_type: str
    content_hash: str
    price: Optional[float] = None
    old_price: Optional[float] = None
    discount_pct: Optional[float] = None
    currency: str = "USD"
    image_url: Optional[str] = None
    category: Optional[str] = None
    source_channel: Optional[str] = None
    source_msg_id: Optional[int] = None
    # Local path of a downloaded image, if we captured media bytes.
    image_path: Optional[str] = None

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("offer title must not be empty")
        if not self.link:
            raise ValueError("offer link must not be empty")
        if self.source_type not in (SOURCE_AMAZON, SOURCE_TELEGRAM):
            raise ValueError(f"unknown source_type: {self.source_type!r}")
        if not self.content_hash:
            raise ValueError("content_hash is required")


@dataclass(slots=True)
class StoredOffer:
    """An offer as persisted in SQLite."""

    id: int
    title: str
    link: str
    source_type: str
    content_hash: str
    price: Optional[float] = None
    old_price: Optional[float] = None
    discount_pct: Optional[float] = None
    currency: str = "USD"
    image_url: Optional[str] = None
    category: Optional[str] = None
    source_channel: Optional[str] = None
    source_msg_id: Optional[int] = None
    status: str = STATUS_PENDING
    created_at: Optional[str] = None
    posted_at: Optional[str] = None


@dataclass(slots=True)
class Delivery:
    """Per-channel delivery record for an offer (multi-channel fan-out)."""

    offer_id: int
    channel: str
    status: str = "pending"
    message_id: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    sent_at: Optional[str] = None


@dataclass(slots=True)
class Stats:
    collected_today: int = 0
    posted_today: int = 0
    pending: int = 0
    rejected_today: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    paused: bool = False

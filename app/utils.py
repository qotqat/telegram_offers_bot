"""Small shared helpers (timestamps, hashing, text detection)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_WHITESPACE_RE = re.compile(r"\s+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """UTC timestamp in the format used across the DB (sortable as text)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_ISO_FORMAT)


def now_iso() -> str:
    return to_iso(utc_now())


def hours_ago_iso(hours: float) -> str:
    return to_iso(utc_now() - timedelta(hours=hours))


def utc_day_start_iso() -> str:
    start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start)


def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip common tracking noise."""
    return _WHITESPACE_RE.sub(" ", text or "").strip().lower()


def sha256_text(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update((part or "").encode("utf-8"))
        digest.update(b"\x1f")  # unit separator avoids accidental concatenation
    return digest.hexdigest()


def utf16_length(text: str) -> int:
    """Telegram text/caption limits count non-BMP emoji as two code units."""
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, limit: int) -> str:
    return text.encode("utf-16-le")[: max(0, limit) * 2].decode("utf-16-le", errors="ignore")


def contains_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))

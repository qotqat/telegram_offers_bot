"""Utility tests: timestamps, hashing, Arabic detection."""

from __future__ import annotations

from app.utils import (
    contains_arabic,
    hours_ago_iso,
    normalize_text,
    now_iso,
    sha256_text,
    utc_day_start_iso,
)


def test_iso_timestamps_are_sortable_utc() -> None:
    now = now_iso()
    hour_ago = hours_ago_iso(1)
    day_start = utc_day_start_iso()

    assert now.endswith("Z") and len(now) == len("2026-11-17T12:00:00Z")
    assert hour_ago < now
    assert day_start.endswith("T00:00:00Z")
    assert day_start <= now


def test_hashing_is_stable_and_part_sensitive() -> None:
    assert sha256_text("a", "b") == sha256_text("a", "b")
    assert sha256_text("a", "b") != sha256_text("ab")
    assert sha256_text("a", "b") != sha256_text("b", "a")


def test_normalize_text() -> None:
    assert normalize_text("  Sony   XM5 \n Headphones ") == "sony xm5 headphones"
    assert normalize_text("") == ""


def test_contains_arabic() -> None:
    assert contains_arabic("خصم 30% على السماعات")
    assert not contains_arabic("30% off headphones")
    assert not contains_arabic("")

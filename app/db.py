"""SQLite access layer.

All three processes (listener, broadcaster, admin bot) share one database
file. WAL mode allows concurrent readers with a single writer, and a busy
timeout keeps short write bursts from raising "database is locked".
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .models import (
    STATUS_PENDING,
    STATUS_POSTED,
    STATUS_REJECTED,
    Offer,
    Stats,
    StoredOffer,
)
from .utils import utc_day_start_iso

SCHEMA_VERSION = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    price REAL,
    old_price REAL,
    discount_pct REAL,
    currency TEXT DEFAULT 'USD',
    link TEXT NOT NULL,
    image_url TEXT,
    image_path TEXT,
    post_text TEXT,
    category TEXT,
    source_type TEXT NOT NULL,              -- 'amazon' | 'telegram'
    source_channel TEXT,
    source_msg_id INTEGER,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | posted | rejected
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    posted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_created ON offers(created_at);
CREATE INDEX IF NOT EXISTS idx_offers_source ON offers(source_channel, created_at);
CREATE INDEX IF NOT EXISTS idx_offers_msg ON offers(source_channel, source_msg_id);

-- One row per (offer, channel): fan-out bookkeeping for multi-channel posting.
CREATE TABLE IF NOT EXISTS post_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed | skipped
    message_id INTEGER,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sent_at TEXT,
    UNIQUE (offer_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_status ON post_deliveries(status, channel);
CREATE INDEX IF NOT EXISTS idx_deliveries_sent ON post_deliveries(sent_at);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_SCHEMA_OBJECTS = ("offers", "post_deliveries", "state")

# Forward-only migrations, keyed by the version they produce. New installs get
# the full schema above, so migrations only run on databases created by older
# code.
_MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE offers ADD COLUMN image_path TEXT"],
    3: ["ALTER TABLE offers ADD COLUMN post_text TEXT"],
}

_DEFAULTS = {
    "paused": "0",
    "last_amazon_poll": "",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and if needed create) the shared database in WAL mode."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path_or_conn: str | Path | sqlite3.Connection) -> sqlite3.Connection:
    """Create schema if missing and apply pending migrations (idempotent)."""
    if isinstance(db_path_or_conn, sqlite3.Connection):
        conn = db_path_or_conn
    else:
        conn = connect(db_path_or_conn)

    with conn:
        conn.executescript(_SCHEMA_SQL)
        for key, value in _DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO state (key, value) VALUES (?, ?)", (key, value)
            )
        current = get_state(conn, "schema_version")
        if current is None:
            set_state(conn, "schema_version", str(SCHEMA_VERSION))
        else:
            version = int(current)
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{version} is newer than this code (v{SCHEMA_VERSION})"
                )
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS.get(target, []):
                    _apply_migration(conn, statement)
                set_state(conn, "schema_version", str(target))
    return conn


def _apply_migration(conn: sqlite3.Connection, statement: str) -> None:
    """Run one migration statement, tolerating an already-applied column add."""
    try:
        conn.execute(statement)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def schema_version(conn: sqlite3.Connection) -> int:
    raw = get_state(conn, "schema_version")
    return int(raw) if raw else 0


# --------------------------------------------------------------------- offers
def insert_offer(conn: sqlite3.Connection, offer: Offer) -> Optional[int]:
    """Insert an offer; return its id, or None when the hash already exists."""
    with conn:
        cur = conn.execute(
            """
            INSERT INTO offers (
                title, price, old_price, discount_pct, currency, link, image_url,
                image_path, post_text, category, source_type, source_channel,
                source_msg_id, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (
                offer.title,
                offer.price,
                offer.old_price,
                offer.discount_pct,
                offer.currency,
                offer.link,
                offer.image_url,
                offer.image_path,
                offer.post_text,
                offer.category,
                offer.source_type,
                offer.source_channel,
                offer.source_msg_id,
                offer.content_hash,
            ),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)


def get_offer(conn: sqlite3.Connection, offer_id: int) -> Optional[StoredOffer]:
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    return _row_to_offer(row) if row else None


def offer_exists_by_source(
    conn: sqlite3.Connection, source_channel: str, source_msg_id: int
) -> bool:
    """Cheap re-run guard: catch-up must not re-process known messages."""
    row = conn.execute(
        "SELECT 1 FROM offers WHERE source_channel = ? AND source_msg_id = ? LIMIT 1",
        (source_channel, source_msg_id),
    ).fetchone()
    return row is not None


def recent_offers(
    conn: sqlite3.Connection,
    limit: int = 500,
    *,
    include_rejected: bool = True,
) -> list[StoredOffer]:
    """Most recent offers, used as the fuzzy-dedupe comparison window.

    The filter pipeline asks for accepted offers only, so a deal rejected for
    an unrelated reason cannot veto a later, legitimate offer.
    """
    sql = "SELECT * FROM offers"
    params: list[object] = []
    if not include_rejected:
        sql += " WHERE status != ?"
        params.append(STATUS_REJECTED)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_offer(row) for row in rows]


def mark_offer_posted(conn: sqlite3.Connection, offer_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE offers SET status = ?, posted_at = "
            "(strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "WHERE id = ? AND status != ?",
            (STATUS_POSTED, offer_id, STATUS_POSTED),
        )


def mark_offer_rejected(conn: sqlite3.Connection, offer_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE offers SET status = ? WHERE id = ?",
            (STATUS_REJECTED, offer_id),
        )


def count_offers_since(
    conn: sqlite3.Connection, since_iso: str, *, source_channel: Optional[str] = None
) -> int:
    """Accepted offers (any status except rejected) created since a timestamp."""
    sql = "SELECT COUNT(*) FROM offers WHERE created_at >= ? AND status != ?"
    params: list[object] = [since_iso, STATUS_REJECTED]
    if source_channel is not None:
        sql += " AND source_channel = ?"
        params.append(source_channel)
    return int(conn.execute(sql, params).fetchone()[0])


# ----------------------------------------------------------------- deliveries
def ensure_delivery(
    conn: sqlite3.Connection, offer_id: int, channel: str
) -> int:
    """Return the delivery id for (offer, channel), creating a pending row."""
    with conn:
        conn.execute(
            """
            INSERT INTO post_deliveries (offer_id, channel, status)
            VALUES (?, ?, 'pending')
            ON CONFLICT(offer_id, channel) DO NOTHING
            """,
            (offer_id, channel),
        )
    row = conn.execute(
        "SELECT id FROM post_deliveries WHERE offer_id = ? AND channel = ?",
        (offer_id, channel),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive, insert above guarantees a row
        raise RuntimeError(f"failed to create delivery for offer={offer_id} channel={channel}")
    return int(row["id"])


def next_pending_for_channel(
    conn: sqlite3.Connection, channel: str
) -> Optional[StoredOffer]:
    """Oldest pending offer with no sent/skipped delivery for this channel."""
    row = conn.execute(
        """
        SELECT o.* FROM offers o
        WHERE o.status = 'pending'
          AND NOT EXISTS (
              SELECT 1 FROM post_deliveries d
              WHERE d.offer_id = o.id
                AND d.channel = ?
                AND d.status IN ('sent', 'skipped')
          )
        ORDER BY o.id ASC
        LIMIT 1
        """,
        (channel,),
    ).fetchone()
    return _row_to_offer(row) if row else None


def mark_delivery_sent(
    conn: sqlite3.Connection, delivery_id: int, message_id: Optional[int]
) -> None:
    with conn:
        conn.execute(
            "UPDATE post_deliveries SET status = 'sent', message_id = ?, error = NULL, "
            "sent_at = (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) WHERE id = ?",
            (message_id, delivery_id),
        )


def mark_delivery_failed(
    conn: sqlite3.Connection, delivery_id: int, error: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE post_deliveries SET status = 'failed', error = ? WHERE id = ?",
            (error[:1000], delivery_id),
        )


def mark_delivery_skipped(
    conn: sqlite3.Connection, delivery_id: int, reason: str
) -> None:
    """Mark a delivery as intentionally not sent (e.g. route mismatch)."""
    with conn:
        conn.execute(
            "UPDATE post_deliveries SET status = 'skipped', error = ? WHERE id = ?",
            (reason[:1000], delivery_id),
        )


def count_sent_since(
    conn: sqlite3.Connection, since_iso: str, *, channel: Optional[str] = None
) -> int:
    sql = "SELECT COUNT(*) FROM post_deliveries WHERE status = 'sent' AND sent_at >= ?"
    params: list[object] = [since_iso]
    if channel is not None:
        sql += " AND channel = ?"
        params.append(channel)
    return int(conn.execute(sql, params).fetchone()[0])


def all_channels_delivered(
    conn: sqlite3.Connection,
    offer_id: int,
    channels: Optional[Iterable[str]] = None,
) -> bool:
    """True when every expected channel has a sent/skipped delivery.

    Passing ``channels`` is the safe form for fan-out: it also catches target
    channels that do not yet have a delivery row. Without it, the legacy
    behavior only checks existing rows.
    """
    if channels is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM post_deliveries WHERE offer_id = ? "
            "AND status NOT IN ('sent', 'skipped')",
            (offer_id,),
        ).fetchone()
        return int(row[0]) == 0

    expected = list(dict.fromkeys(channels))
    if not expected:
        return False
    placeholders = ",".join("?" for _ in expected)
    row = conn.execute(
        f"SELECT COUNT(DISTINCT channel) FROM post_deliveries "
        f"WHERE offer_id = ? AND status IN ('sent', 'skipped') "
        f"AND channel IN ({placeholders})",
        (offer_id, *expected),
    ).fetchone()
    return int(row[0]) >= len(expected)


# --------------------------------------------------------------------- state
def get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_state(conn, "paused") == "1"


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    set_state(conn, "paused", "1" if paused else "0")


# --------------------------------------------------------------------- stats
def get_stats(conn: sqlite3.Connection) -> Stats:
    day_start = utc_day_start_iso()
    collected = int(
        conn.execute(
            "SELECT COUNT(*) FROM offers WHERE created_at >= ?", (day_start,)
        ).fetchone()[0]
    )
    rejected = int(
        conn.execute(
            "SELECT COUNT(*) FROM offers WHERE created_at >= ? AND status = ?",
            (day_start, STATUS_REJECTED),
        ).fetchone()[0]
    )
    posted = int(
        conn.execute(
            "SELECT COUNT(*) FROM post_deliveries WHERE sent_at >= ?", (day_start,)
        ).fetchone()[0]
    )
    pending = int(
        conn.execute(
            "SELECT COUNT(*) FROM offers WHERE status = ?", (STATUS_PENDING,)
        ).fetchone()[0]
    )
    per_source = {
        row["source_channel"] or "(unknown)": int(row["n"])
        for row in conn.execute(
            "SELECT source_channel, COUNT(*) AS n FROM offers "
            "WHERE created_at >= ? GROUP BY source_channel",
            (day_start,),
        ).fetchall()
    }
    return Stats(
        collected_today=collected,
        posted_today=posted,
        pending=pending,
        rejected_today=rejected,
        per_source=per_source,
        paused=is_paused(conn),
    )


# -------------------------------------------------------------------- helpers
def _row_to_offer(row: sqlite3.Row) -> StoredOffer:
    data = dict(row)
    return StoredOffer(
        id=data["id"],
        title=data["title"],
        link=data["link"],
        source_type=data["source_type"],
        content_hash=data["content_hash"],
        price=data["price"],
        old_price=data["old_price"],
        discount_pct=data["discount_pct"],
        currency=data["currency"],
        image_url=data["image_url"],
        image_path=data["image_path"],
        post_text=data["post_text"],
        category=data["category"],
        source_channel=data["source_channel"],
        source_msg_id=data["source_msg_id"],
        status=data["status"],
        created_at=data["created_at"],
        posted_at=data["posted_at"],
    )


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "init_db",
    "schema_version",
    "insert_offer",
    "get_offer",
    "offer_exists_by_source",
    "recent_offers",
    "mark_offer_posted",
    "mark_offer_rejected",
    "count_offers_since",
    "ensure_delivery",
    "next_pending_for_channel",
    "mark_delivery_sent",
    "mark_delivery_failed",
    "mark_delivery_skipped",
    "count_sent_since",
    "all_channels_delivered",
    "get_state",
    "set_state",
    "is_paused",
    "set_paused",
    "get_stats",
    "table_names",
]

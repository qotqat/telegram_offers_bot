"""Inspect recent offers (useful for the M1 listener check).

Usage:
    python -m scripts.list_offers
    python -m scripts.list_offers --limit 50 --source @chan1
    python -m scripts.list_offers --status pending
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import db
from app.config import Config, ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List recent offers")
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument("--db", default=None, help="override DB_PATH")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--source", default=None, help="filter by source channel")
    parser.add_argument("--status", default=None, help="pending | posted | rejected")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else None
    if db_path is None:
        try:
            db_path = Config.load(args.env).db_path
        except ConfigError:
            db_path = Path("data/offers.db")

    if not Path(db_path).exists():
        print(f"No database at {db_path} — run python -m scripts.init_db", file=sys.stderr)
        return 1

    conn = db.connect(db_path)
    try:
        sql = "SELECT * FROM offers"
        clauses: list[str] = []
        params: list[object] = []
        if args.source:
            clauses.append("source_channel = ?")
            params.append(args.source)
        if args.status:
            clauses.append("status = ?")
            params.append(args.status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, args.limit))

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("No offers match.")
            return 0

        for row in rows:
            price = f"{row['price']:.2f}" if row["price"] is not None else "?"
            discount = (
                f"-{row['discount_pct']:.1f}%" if row["discount_pct"] is not None else "  ?  "
            )
            image = "img" if row["image_path"] or row["image_url"] else "   "
            print(
                f"#{row['id']:<4} {row['created_at']}  {row['status']:<8}  "
                f"{discount:>6}  {price:>9} {row['currency'] or '':<4} {image}  "
                f"[{row['source_channel'] or '-'}] {row['title'][:70]}"
            )
        print(f"\n{len(rows)} offer(s)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Create/verify the shared SQLite database.

Usage:
    python -m scripts.init_db
    python -m scripts.init_db --env .env --db data/offers.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import db
from app.config import Config, ConfigError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize the offers database")
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument("--db", default=None, help="override DB_PATH")
    args = parser.parse_args(argv)

    db_path: Path
    if args.db:
        db_path = Path(args.db)
    else:
        try:
            cfg = Config.load(args.env)
            db_path = cfg.db_path
        except ConfigError as exc:
            # DB creation does not depend on Telegram credentials, so keep
            # the tool usable before `.env` is filled in.
            print(f"[warn] no usable config ({exc}); using data/offers.db", file=sys.stderr)
            db_path = Path("data/offers.db")
    conn = db.init_db(db_path)
    try:
        tables = db.table_names(conn)
        print(f"Database ready: {db_path.resolve()}")
        print(f"Schema version: {db.schema_version(conn)}")
        print(f"Tables: {', '.join(tables)}")
        print(f"Journal mode: {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

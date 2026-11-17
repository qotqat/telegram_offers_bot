"""Entry point for the Telethon listener process.

Examples:
    python listener.py                     # catch-up then live listening
    python listener.py --once              # catch-up only, then exit
    python listener.py --no-catchup        # live only
    python listener.py --limit 10 --once   # small test run
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app import listener
from app.config import Config, ConfigError
from app.logging_setup import setup_logging


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram source listener")
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument(
        "--once", action="store_true", help="catch-up only, then exit"
    )
    parser.add_argument(
        "--no-catchup", action="store_true", help="skip the startup catch-up"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="catch-up messages per source"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = Config.load(args.env)
        config.require_listener()
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        print("\nHint: copy .env.example to .env and fill in the values.", file=sys.stderr)
        return 1

    setup_logging(config.log_path, config.log_level)
    try:
        asyncio.run(
            listener.run(
                config,
                catchup=not args.no_catchup,
                once=args.once,
                limit=args.limit,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

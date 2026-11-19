"""Entry point for the Telegram broadcaster.

Examples:
    python broadcaster.py --once               # one pass, then exit
    python broadcaster.py --once --limit 3     # up to 3 posts per channel
    python broadcaster.py --dry-run            # preview pending posts
    python broadcaster.py                      # run until Ctrl+C

The bot from BOT_TOKEN must be an admin with "Post messages" rights in every
channel listed in TARGET_CHANNELS.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from telegram import Bot

from app import broadcaster, db
from app.config import Config, ConfigError
from app.logging_setup import process_log_path, setup_logging
from app.runtime import run_worker


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram offer broadcaster")
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="maximum posts per channel in one pass (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print pending posts without sending or writing deliveries",
    )
    return parser.parse_args(argv)


async def _run(config: Config, args: argparse.Namespace) -> int:
    conn = db.init_db(config.db_path)
    try:
        if args.dry_run:
            result = await broadcaster.broadcast_once(
                conn, config, None, limit_per_channel=args.limit, dry_run=True
            )
            print(f"\nDry run: {result.sent} pending post(s) previewed.")
            return 0

        assert config.bot_token is not None
        async with Bot(config.bot_token) as bot:
            if args.once:
                result = await broadcaster.broadcast_once(
                    conn, config, bot, limit_per_channel=args.limit
                )
                for channel, count in result.channels.items():
                    print(f"{channel}: {count} sent")
                print(
                    f"\nSent {result.sent}, skipped {result.skipped}, "
                    f"failed {result.failed}."
                )
                return 1 if result.failed else 0

            await broadcaster.run_forever(config, conn, bot)
            return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = Config.load(args.env)
        if not args.dry_run:
            config.require_broadcaster()
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        return 1

    setup_logging(
        process_log_path(config.log_path, "broadcaster"), config.log_level,
        secrets=(config.bot_token, config.tg_api_hash, config.amazon_credential_secret),
    )
    try:
        return asyncio.run(run_worker(
            config, "broadcaster", lambda: _run(config, args), alerts=not args.dry_run
        ))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nBroadcaster stopped.")
        return 0
    except Exception as exc:
        print(f"Broadcaster failed ({type(exc).__name__}); see local logs.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

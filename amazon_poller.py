"""Entry point for the Amazon Creators API poller.

Examples:
    python amazon_poller.py --dry-run              # fetch, print, no DB writes
    python amazon_poller.py --once                 # one poll, then exit
    python amazon_poller.py                        # poll forever

With AMAZON_ENABLED=0 (the Telegram-only launch) every mode exits cleanly
without touching the network.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app import amazon, db
from app.config import Config, ConfigError
from app.logging_setup import process_log_path, setup_logging
from app.runtime import notify_error, termination_handler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Amazon Creators API poller")
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument("--once", action="store_true", help="one poll, then exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print offers without writing to the database",
    )
    parser.add_argument(
        "--keywords",
        default=None,
        help="comma-separated keywords overriding AMAZON_KEYWORDS",
    )
    return parser.parse_args(argv)


def _print_offer_line(offer) -> None:
    discount = (
        f"-{offer.discount_pct:g}%" if offer.discount_pct is not None else "-?"
    )
    print(
        f"  {offer.price if offer.price is not None else '?':>9} "
        f"{offer.currency:<4} {discount:>6}  [{offer.category or '-'}] "
        f"{offer.title[:90]}"
    )
    print(f"  {'':>9} {'':<4} {'':>6}  {offer.link}")


def _print_errors(errors: list[str]) -> None:
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)


def dry_run(config: Config) -> int:
    client = amazon.client_from_config(config)
    collection = amazon.collect_offers(config, client)

    print("== Amazon dry run ==")
    print(
        f"{collection.searches} search(es), {collection.items} item(s), "
        f"{len(collection.offers)} qualifying, {collection.skipped} skipped"
    )
    for offer in collection.offers:
        _print_offer_line(offer)
    _print_errors(collection.errors)
    print("\nNothing was written to the database.")
    return 1 if collection.errors else 0


def print_result(result: amazon.PollResult) -> None:
    print(
        f"{result.searches} search(es), {result.items} item(s), "
        f"{result.stored} new, {result.duplicates} duplicate(s), "
        f"{result.rejected} filtered, {result.skipped} skipped, "
        f"{len(result.errors)} error(s)"
    )
    _print_errors(result.errors)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = Config.load(args.env)
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        return 1

    if args.keywords:
        config.amazon_keywords = [
            keyword.strip()
            for keyword in args.keywords.split(",")
            if keyword.strip()
        ]

    if not config.amazon_enabled:
        print(
            "Amazon is disabled (AMAZON_ENABLED=0).\n"
            "This is the Telegram-only launch branch: nothing to poll.\n"
            "Set AMAZON_ENABLED=1 plus the Creators API credentials in .env "
            "to fetch Amazon deals."
        )
        return 0

    try:
        config.require_amazon()
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        return 1

    setup_logging(
        process_log_path(config.log_path, "amazon"), config.log_level,
        secrets=(config.bot_token, config.tg_api_hash, config.amazon_credential_secret),
    )

    def stop() -> None:
        raise KeyboardInterrupt

    try:
        with termination_handler(stop):
            return _run(config, args)
    except KeyboardInterrupt:
        print("\nAmazon poller stopped.")
        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Amazon poller stopped unexpectedly")
        asyncio.run(notify_error(config, "amazon", exc))
        return 1


def _run(config: Config, args: argparse.Namespace) -> int:
    if args.dry_run:
        try:
            return dry_run(config)
        except amazon.AmazonError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

    if args.once:
        conn = db.init_db(config.db_path)
        try:
            result = amazon.poll(config, conn)
        except amazon.AmazonError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        print_result(result)
        return 1 if result.errors else 0

    amazon.run_forever(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

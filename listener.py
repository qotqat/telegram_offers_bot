"""Telethon listener: newest message per source, then live events and polling.

LISTENER_LATEST_ONLY=1 (default) caps positive CATCHUP_LIMIT/--limit values at
one message in EACH source; a non-Amazon newest post skips that source without
scanning backwards. Set LISTENER_LATEST_ONLY=0 for explicit legacy backfill.
Continuous runs register NewMessage events before startup reads, then also poll
each source's latest message every SOURCE_POLL_INTERVAL_SECONDS (default 30,
minimum 5). A shared ingestion lock and source-message/content dedupe prevent
startup/event/poll races and duplicate ingestion.

AMAZON_LINK_MODE=plain (default; plain|affiliate) rebuilds tracking-free /dp/ASIN
links using AMAZON_MARKETPLACE, without a tag, confirmation or API credentials.
Affiliate mode requires your own tag and AMAZON_TAG_CONFIRMED=1. Amazon titles
always come from bounded HTML productTitle lookup via app/products.py, without
CAPTCHA bypass or source-title fallback. Missing/blocked titles skip ingestion
and are retried by the next poll if the message is still latest. AMAZON_ENABLED
controls only the separate, confirmed-tag-gated Creators API poller.

Examples:
    python listener.py                    # newest per source, then events + polling
    python listener.py --once             # newest per source, then exit
    python listener.py --no-catchup       # no startup reads; events + polling
    python listener.py --limit 0 --once   # login/session setup only; no message reads
    python listener.py --limit 10 --once  # backfill only if LISTENER_LATEST_ONLY=0
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app import listener
from app.config import Config, ConfigError
from app.logging_setup import process_log_path, setup_logging
from app.runtime import run_worker


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Telegram source listener: check newest message in EACH source, "
            "then watch live events and poll latest messages "
            "(SOURCE_POLL_INTERVAL_SECONDS: default 30, minimum 5). "
            "Non-Amazon newest posts skip that source in Amazon-only mode; "
            "no backwards scan."
        ),
        epilog=(
            "AMAZON_LINK_MODE=plain (default) uses tracking-free /dp/ASIN links "
            "and needs a marketplace, not a tag, confirmation or API credentials. "
            "Affiliate mode requires your own tag and AMAZON_TAG_CONFIRMED=1. "
            "Amazon titles come from bounded HTML productTitle lookup; "
            "no CAPTCHA bypass or original-title fallback. Unavailable titles "
            "skip ingestion and retry on a later latest-message poll. "
            "AMAZON_ENABLED controls only the separate, confirmed-tag-gated API poller."
        ),
    )
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument(
        "--once", action="store_true",
        help="perform startup checks only, then exit; no live events or polling",
    )
    parser.add_argument(
        "--no-catchup", action="store_true",
        help="skip startup message reads; keep live events and latest-message polling",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=(
            "startup messages per source (otherwise CATCHUP_LIMIT); positive values "
            "capped at 1 by LISTENER_LATEST_ONLY=1 (default). Set "
            "LISTENER_LATEST_ONLY=0 for legacy backfill. 0 skips initial reads; "
            "combine with --once for login/session setup only, otherwise events "
            "and polling still run"
        ),
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="use an existing authorized session; never prompt for login",
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

    setup_logging(
        process_log_path(config.log_path, "listener"), config.log_level,
        secrets=(config.bot_token, config.tg_api_hash, config.amazon_credential_secret),
    )
    try:
        asyncio.run(
            run_worker(
                config, "listener", lambda: listener.run(
                    config,
                    catchup=not args.no_catchup,
                    once=args.once,
                    limit=args.limit,
                    non_interactive=args.non_interactive,
                ),
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nListener stopped.")
    except Exception as exc:
        print(f"Listener failed ({type(exc).__name__}); see local logs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

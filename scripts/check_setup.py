"""Kickoff self-check: config, DB, and account readiness at a glance.

Usage:
    python -m scripts.check_setup

Exit code 0 means the Telegram pipeline has everything it needs.
Exit code 2 means something required is missing or invalid.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app import db
from app.config import Config, ConfigError


def _mark(ok: bool) -> str:
    return "[OK]  " if ok else "[MISS]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check bot configuration and database")
    parser.add_argument("--env", default=".env", help="path to .env file")
    args = parser.parse_args(argv)

    if not Path(args.env).is_file():
        print(f"[MISS] {args.env} not found - copy .env.example to .env first")
        print("\nNothing else can be checked until the config file exists.")
        return 2

    try:
        cfg = Config.load(args.env)
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return 2

    print("== Configuration ==")
    for label, ok, hint in cfg.account_checklist():
        if label.startswith("Amazon") and not cfg.amazon_enabled:
            print("[SKIP] Amazon Creators API (optional; Telegram-only launch)")
            continue
        print(f"{_mark(ok)} {label}" + ("" if ok else f"  ({hint})"))

    print("\n== Non-fatal warnings ==")
    if cfg.warnings:
        for warning in cfg.warnings:
            print(f"[WARN] {warning}")
    else:
        print("none")

    print("\n== Database ==")
    try:
        conn = db.init_db(cfg.db_path)
        try:
            print(f"[OK]   {Path(cfg.db_path).resolve()}")
            print(f"[OK]   schema v{db.schema_version(conn)}, WAL")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - surface any DB problem verbatim
        print(f"[FAIL] could not initialize database: {exc}")
        return 2

    print("\n== Process requirements ==")
    problems = 0
    for label, checker in (
        ("listener", cfg.require_listener),
        ("broadcaster/admin", cfg.require_bot),
    ):
        try:
            checker()
            print(f"{_mark(True)} {label}")
        except ConfigError as exc:
            problems += 1
            print(f"{_mark(False)} {label}: {exc}")

    if cfg.amazon_enabled:
        if cfg.amazon_ready:
            print(
                f"{_mark(True)} amazon poller (enabled: credential "
                f"v{cfg.amazon_credential_version}, {cfg.amazon_marketplace}, "
                f"{len(cfg.amazon_keywords)} keyword(s))"
            )
        else:
            problems += 1
            print(f"{_mark(False)} amazon poller: credentials incomplete")
    else:
        print("[SKIP] amazon poller (AMAZON_ENABLED=0, Telegram-only launch)")

    print("\n== Session ==")
    session_file = Path(str(cfg.tg_session_path) + ".session")
    if session_file.exists():
        print(f"[OK]   listener session present ({session_file})")
    else:
        print("[INFO] no listener session yet - the first `python listener.py` asks for phone/code")

    print("\n== Summary ==")
    if problems:
        print(f"{problems} blocking problem(s) - fix the [MISS] items above.")
        return 2
    if cfg.amazon_enabled:
        print(
            "Configuration ready. Next: `python amazon_poller.py --dry-run` "
            "(Amazon), then `python listener.py --once`."
        )
    else:
        print(
            "Core pipeline configuration is ready. Next: `python listener.py --once`."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

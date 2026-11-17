"""Environment-driven configuration loaded from `.env` (no settings UI in v1)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from dotenv import load_dotenv

CHANNEL_MODE_ALL = "all"
CHANNEL_MODE_ROUTE = "route"

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(Exception):
    """Raised when `.env` is missing required or valid values."""


def _split_csv(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_bool(raw: Optional[str], *, name: str, errors: list[str]) -> bool:
    if raw is None or raw.strip() == "":
        return False
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    errors.append(f"{name} must be a boolean (0/1/true/false), got {raw!r}")
    return False


def _parse_int(
    raw: Optional[str],
    *,
    name: str,
    default: Optional[int],
    errors: list[str],
    minimum: Optional[int] = None,
) -> Optional[int]:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        errors.append(f"{name} must be an integer, got {raw!r}")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"{name} must be >= {minimum}, got {value}")
    return value


def _parse_float(
    raw: Optional[str],
    *,
    name: str,
    default: float,
    errors: list[str],
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        errors.append(f"{name} must be a number, got {raw!r}")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        errors.append(f"{name} must be <= {maximum}, got {value}")
    return value


@dataclass(slots=True)
class Config:
    # Telegram user account (Telethon listener)
    tg_api_id: Optional[int] = None
    tg_api_hash: Optional[str] = None
    tg_session_path: Path = Path("data/listener_session")

    # Broadcast bot
    bot_token: Optional[str] = None
    owner_id: Optional[int] = None

    # Output channels
    target_channels: list[str] = field(default_factory=list)
    channel_mode: str = CHANNEL_MODE_ALL
    route_rules: list[tuple[str, str]] = field(default_factory=list)

    # Source channels
    source_channels: list[str] = field(default_factory=list)

    # Filters / queue
    min_discount_pct: float = 20.0
    exclude_keywords: list[str] = field(default_factory=list)
    post_interval_seconds: int = 360
    max_posts_per_hour: int = 10
    max_per_source_per_hour: int = 3
    fuzzy_threshold: float = 88.0

    # Amazon Creators API (optional at launch)
    amazon_enabled: bool = False
    amazon_credential_id: Optional[str] = None
    amazon_credential_secret: Optional[str] = None
    amazon_associate_tag: Optional[str] = None
    amazon_region: str = "US"
    amazon_poll_interval_minutes: int = 30

    # Storage / logging
    db_path: Path = Path("data/offers.db")
    log_path: Path = Path("data/bot.log")
    log_level: str = "INFO"

    # Non-fatal issues found while parsing (shown by the setup check).
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(
        cls,
        env_file: Optional[str | Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "Config":
        """Load config from `.env` plus process env.

        Real environment variables win over `.env` values, which keeps Docker
        overrides and one-off test runs working without editing files.
        """
        if env is None and env_file is not None:
            path = Path(env_file)
            if path.is_file():
                load_dotenv(path, override=False)
        source = os.environ if env is None else env

        errors: list[str] = []
        warnings: list[str] = []

        def get(name: str) -> Optional[str]:
            value = source.get(name)
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        target_channels = _split_csv(get("TARGET_CHANNELS"))
        source_channels = _split_csv(get("SOURCE_CHANNELS"))
        exclude_keywords = [kw.lower() for kw in _split_csv(get("EXCLUDE_KEYWORDS"))]

        channel_mode = (get("CHANNEL_MODE") or CHANNEL_MODE_ALL).lower()
        if channel_mode not in (CHANNEL_MODE_ALL, CHANNEL_MODE_ROUTE):
            errors.append(
                f"CHANNEL_MODE must be '{CHANNEL_MODE_ALL}' or '{CHANNEL_MODE_ROUTE}', "
                f"got {channel_mode!r}"
            )

        route_rules: list[tuple[str, str]] = []
        for rule in _split_csv(get("ROUTE_RULES")):
            channel, sep, match = rule.partition(":")
            if not sep or not channel.strip() or not match.strip():
                errors.append(
                    f"ROUTE_RULES entry {rule!r} must look like 'channel:match'"
                )
                continue
            route_rules.append((channel.strip(), match.strip().lower()))

        if not target_channels:
            errors.append("TARGET_CHANNELS is required (at least one output channel)")
        else:
            dupes = {c for c in target_channels if target_channels.count(c) > 1}
            if dupes:
                errors.append(f"TARGET_CHANNELS has duplicates: {', '.join(sorted(dupes))}")

        if len(source_channels) != 10:
            warnings.append(
                f"SOURCE_CHANNELS has {len(source_channels)} entries; the launch plan "
                "expects exactly 10"
            )

        if channel_mode == CHANNEL_MODE_ROUTE:
            if not route_rules:
                warnings.append(
                    "CHANNEL_MODE=route but ROUTE_RULES is empty; every offer will "
                    "fall back to the first target channel"
                )
            for channel, _match in route_rules:
                if channel not in target_channels:
                    warnings.append(
                        f"ROUTE_RULES targets {channel} which is not in TARGET_CHANNELS"
                    )

        min_discount = _parse_float(
            get("MIN_DISCOUNT_PCT"),
            name="MIN_DISCOUNT_PCT",
            default=20.0,
            errors=errors,
            minimum=0,
            maximum=100,
        )
        post_interval = _parse_int(
            get("POST_INTERVAL_SECONDS"),
            name="POST_INTERVAL_SECONDS",
            default=360,
            errors=errors,
            minimum=30,
        )
        max_per_hour = _parse_int(
            get("MAX_POSTS_PER_HOUR"),
            name="MAX_POSTS_PER_HOUR",
            default=10,
            errors=errors,
            minimum=1,
        )
        max_per_source = _parse_int(
            get("MAX_PER_SOURCE_PER_HOUR"),
            name="MAX_PER_SOURCE_PER_HOUR",
            default=3,
            errors=errors,
            minimum=1,
        )
        fuzzy_threshold = _parse_float(
            get("FUZZY_THRESHOLD"),
            name="FUZZY_THRESHOLD",
            default=88.0,
            errors=errors,
            minimum=0,
            maximum=100,
        )

        owner_id = _parse_int(get("OWNER_ID"), name="OWNER_ID", default=None, errors=errors)

        log_level = (get("LOG_LEVEL") or "INFO").upper()
        if log_level not in VALID_LOG_LEVELS:
            errors.append(
                f"LOG_LEVEL must be one of {', '.join(sorted(VALID_LOG_LEVELS))}, "
                f"got {log_level!r}"
            )

        amazon_enabled = _parse_bool(get("AMAZON_ENABLED"), name="AMAZON_ENABLED", errors=errors)
        amazon_id = get("AMAZON_CREDENTIAL_ID")
        amazon_secret = get("AMAZON_CREDENTIAL_SECRET")
        amazon_tag = get("AMAZON_ASSOCIATE_TAG")
        if amazon_enabled:
            missing = [
                name
                for name, value in (
                    ("AMAZON_CREDENTIAL_ID", amazon_id),
                    ("AMAZON_CREDENTIAL_SECRET", amazon_secret),
                    ("AMAZON_ASSOCIATE_TAG", amazon_tag),
                )
                if not value
            ]
            if missing:
                errors.append(
                    "AMAZON_ENABLED=1 but missing: " + ", ".join(missing)
                )

        cfg = cls(
            tg_api_id=_parse_int(get("TG_API_ID"), name="TG_API_ID", default=None, errors=errors),
            tg_api_hash=get("TG_API_HASH"),
            tg_session_path=Path(get("TG_SESSION_PATH") or "data/listener_session"),
            bot_token=get("BOT_TOKEN"),
            owner_id=owner_id,
            target_channels=target_channels,
            channel_mode=channel_mode,
            route_rules=route_rules,
            source_channels=source_channels,
            min_discount_pct=min_discount,
            exclude_keywords=exclude_keywords,
            post_interval_seconds=post_interval or 360,
            max_posts_per_hour=max_per_hour or 10,
            max_per_source_per_hour=max_per_source or 3,
            fuzzy_threshold=fuzzy_threshold,
            amazon_enabled=amazon_enabled,
            amazon_credential_id=amazon_id,
            amazon_credential_secret=amazon_secret,
            amazon_associate_tag=amazon_tag,
            amazon_region=(get("AMAZON_REGION") or "US").upper(),
            amazon_poll_interval_minutes=_parse_int(
                get("AMAZON_POLL_INTERVAL_MINUTES"),
                name="AMAZON_POLL_INTERVAL_MINUTES",
                default=30,
                errors=errors,
                minimum=5,
            )
            or 30,
            db_path=Path(get("DB_PATH") or "data/offers.db"),
            log_path=Path(get("LOG_PATH") or "data/bot.log"),
            log_level=log_level,
            warnings=warnings,
        )

        if errors:
            raise ConfigError(
                "Invalid configuration:\n  - " + "\n  - ".join(errors)
            )
        return cfg

    # --------------------------------------------------------------- checks
    def _require(self, names: Iterable[tuple[str, object]]) -> None:
        missing = [name for name, value in names if not value]
        if missing:
            raise ConfigError("Missing required configuration: " + ", ".join(missing))

    def require_listener(self) -> None:
        """Everything the Telethon listener process needs."""
        self._require(
            (
                ("TG_API_ID", self.tg_api_id),
                ("TG_API_HASH", self.tg_api_hash),
                ("SOURCE_CHANNELS", self.source_channels),
            )
        )

    def require_bot(self) -> None:
        """Everything the broadcaster/admin bot needs."""
        self._require(
            (
                ("BOT_TOKEN", self.bot_token),
                ("OWNER_ID", self.owner_id),
                ("TARGET_CHANNELS", self.target_channels),
            )
        )

    def require_amazon(self) -> None:
        """Everything the Amazon poller needs (only when enabled)."""
        if not self.amazon_enabled:
            return
        self._require(
            (
                ("AMAZON_CREDENTIAL_ID", self.amazon_credential_id),
                ("AMAZON_CREDENTIAL_SECRET", self.amazon_credential_secret),
                ("AMAZON_ASSOCIATE_TAG", self.amazon_associate_tag),
            )
        )

    # ------------------------------------------------------------ helpers
    @property
    def amazon_ready(self) -> bool:
        return bool(
            self.amazon_enabled
            and self.amazon_credential_id
            and self.amazon_credential_secret
            and self.amazon_associate_tag
        )

    def account_checklist(self) -> list[tuple[str, bool, str]]:
        """Human-readable launch checklist derived from config values."""
        return [
            (
                "Telegram api_id/api_hash",
                bool(self.tg_api_id and self.tg_api_hash),
                "my.telegram.org -> API development tools",
            ),
            (
                "Bot token",
                bool(self.bot_token),
                "@BotFather; bot must be channel admin",
            ),
            (
                "Owner user id",
                bool(self.owner_id),
                "get it from @userinfobot",
            ),
            (
                "Target channels",
                bool(self.target_channels),
                "bot admin with post rights in each",
            ),
            (
                "Source channels (10)",
                len(self.source_channels) == 10,
                f"found {len(self.source_channels)}; listener account must have joined them",
            ),
            (
                "Amazon Creators API",
                self.amazon_ready,
                "optional for launch (AMAZON_ENABLED=0 = Telegram-only)",
            ),
        ]

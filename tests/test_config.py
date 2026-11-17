"""Config parsing and validation tests."""

from __future__ import annotations

import pytest

from app.config import CHANNEL_MODE_ROUTE, Config, ConfigError


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "TG_API_ID": "123456",
        "TG_API_HASH": "abcdef",
        "BOT_TOKEN": "123:abc",
        "OWNER_ID": "42",
        "TARGET_CHANNELS": "@deals_en,@deals_ar",
        "SOURCE_CHANNELS": ",".join(f"@chan{i}" for i in range(10)),
        "MIN_DISCOUNT_PCT": "25",
        "EXCLUDE_KEYWORDS": "Fake, SCAM",
        "POST_INTERVAL_SECONDS": "300",
        "MAX_POSTS_PER_HOUR": "12",
        "MAX_PER_SOURCE_PER_HOUR": "2",
        "FUZZY_THRESHOLD": "90",
        "DB_PATH": "data/test.db",
        "LOG_PATH": "data/test.log",
        "LOG_LEVEL": "debug",
    }
    env.update(overrides)
    return env


def test_loads_valid_config() -> None:
    cfg = Config.load(env=base_env())

    assert cfg.tg_api_id == 123456
    assert cfg.owner_id == 42
    assert cfg.target_channels == ["@deals_en", "@deals_ar"]
    assert len(cfg.source_channels) == 10
    assert cfg.channel_mode == "all"
    assert cfg.min_discount_pct == 25
    assert cfg.exclude_keywords == ["fake", "scam"]
    assert cfg.post_interval_seconds == 300
    assert cfg.max_posts_per_hour == 12
    assert cfg.max_per_source_per_hour == 2
    assert cfg.fuzzy_threshold == 90
    assert cfg.log_level == "DEBUG"
    assert str(cfg.db_path).endswith("test.db")
    assert cfg.warnings == []


def test_missing_target_channels_is_an_error() -> None:
    with pytest.raises(ConfigError, match="TARGET_CHANNELS"):
        Config.load(env=base_env(TARGET_CHANNELS=""))


def test_invalid_channel_mode_is_an_error() -> None:
    with pytest.raises(ConfigError, match="CHANNEL_MODE"):
        Config.load(env=base_env(CHANNEL_MODE="broadcast"))


def test_route_mode_parses_rules() -> None:
    cfg = Config.load(
        env=base_env(
            CHANNEL_MODE="route",
            ROUTE_RULES="@deals_ar:ar,@deals_tech:tech,@deals_en:*",
        )
    )
    assert cfg.channel_mode == CHANNEL_MODE_ROUTE
    assert cfg.route_rules == [
        ("@deals_ar", "ar"),
        ("@deals_tech", "tech"),
        ("@deals_en", "*"),
    ]


def test_bad_route_rule_is_an_error() -> None:
    with pytest.raises(ConfigError, match="ROUTE_RULES"):
        Config.load(env=base_env(CHANNEL_MODE="route", ROUTE_RULES="@deals_ar"))


def test_route_mode_without_rules_warns() -> None:
    cfg = Config.load(env=base_env(CHANNEL_MODE="route"))
    assert any("ROUTE_RULES is empty" in warning for warning in cfg.warnings)


def test_source_count_other_than_ten_warns() -> None:
    cfg = Config.load(env=base_env(SOURCE_CHANNELS="@only_one"))
    assert any("exactly 10" in warning for warning in cfg.warnings)
    assert cfg.source_channels == ["@only_one"]


def test_duplicate_target_channels_is_an_error() -> None:
    with pytest.raises(ConfigError, match="duplicates"):
        Config.load(env=base_env(TARGET_CHANNELS="@a,@a"))


def test_amazon_disabled_needs_no_credentials() -> None:
    cfg = Config.load(env=base_env(AMAZON_ENABLED="0"))
    assert cfg.amazon_enabled is False
    assert cfg.amazon_ready is False


def test_amazon_enabled_without_credentials_is_an_error() -> None:
    with pytest.raises(ConfigError, match="AMAZON_CREDENTIAL_ID"):
        Config.load(env=base_env(AMAZON_ENABLED="1", AMAZON_ASSOCIATE_TAG="tag-20"))


def test_amazon_ready_when_fully_configured() -> None:
    cfg = Config.load(
        env=base_env(
            AMAZON_ENABLED="true",
            AMAZON_CREDENTIAL_ID="amzn1.cred",
            AMAZON_CREDENTIAL_SECRET="secret",
            AMAZON_ASSOCIATE_TAG="tag-20",
        )
    )
    assert cfg.amazon_ready is True


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("MIN_DISCOUNT_PCT", "abc", "MIN_DISCOUNT_PCT"),
        ("MIN_DISCOUNT_PCT", "150", "MIN_DISCOUNT_PCT"),
        ("POST_INTERVAL_SECONDS", "5", "POST_INTERVAL_SECONDS"),
        ("MAX_POSTS_PER_HOUR", "0", "MAX_POSTS_PER_HOUR"),
        ("LOG_LEVEL", "verbose", "LOG_LEVEL"),
        ("TG_API_ID", "not-a-number", "TG_API_ID"),
    ],
)
def test_numeric_and_enum_validation(name: str, value: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        Config.load(env=base_env(**{name: value}))


def test_require_listener_and_bot() -> None:
    cfg = Config.load(env=base_env())
    cfg.require_listener()
    cfg.require_bot()

    missing = Config.load(env=base_env(TG_API_HASH="", BOT_TOKEN=""))
    with pytest.raises(ConfigError, match="TG_API_HASH"):
        missing.require_listener()
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        missing.require_bot()


def test_loads_from_dotenv_file(tmp_path, monkeypatch) -> None:
    for key in base_env():
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in base_env().items()), encoding="utf-8"
    )

    cfg = Config.load(env_file)
    assert cfg.target_channels == ["@deals_en", "@deals_ar"]
    assert cfg.owner_id == 42

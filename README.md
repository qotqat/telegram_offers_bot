# Telegram Offers Bot

Automatic bilingual (Arabic/English) deal aggregator: listens to Telegram source
channels, normalizes and dedupes offers, then broadcasts them to one or more
channels run by a single bot.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| **1. Foundations** | Skeleton, `.env` config + validation, SQLite schema/access layer, self-check tooling, tests | ✅ done |
| **2. Telegram listener** | Telethon session, 10 sources, live + 50-msg catch-up, media capture, normalize + hash insert | next |
| **3. Amazon ingestion** | Creators API (Branch A) or stubbed module behind `AMAZON_ENABLED` (Branch B) | pending |
| **4. Filters & dedupe** | rapidfuzz near-dup, discount/keyword/source-cap filters, auto-approve | pending |
| **5. Broadcaster** | `all` / `route` fan-out, AR/EN templates, buttons, rate limits, per-channel error isolation | pending |
| **6. Admin & hardening** | `/pause` `/resume` `/stats`, crash alerts, rotating logs, graceful shutdown | pending |
| **7. Deploy** | Docker Compose (3 services, shared `/data`), free host, soak test, runbook | pending |

## Requirements

- Python 3.11+ (3.12 tested)
- A **secondary** Telegram account for the listener (never your main)
- Bot from [@BotFather](https://t.me/BotFather), admin with post rights in every target channel
- Source channels joined by the listener account

## Quickstart (local)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

copy .env.example .env      # then fill in the values
python -m scripts.check_setup
python -m scripts.init_db
pytest
```

`check_setup` prints a per-account checklist plus config warnings and exits
non-zero while a required value is missing. `init_db` creates the schema and is
safe to re-run.

## Layout

```
app/
  config.py         # .env parsing + validation (single source of truth)
  db.py             # SQLite (WAL) schema and access layer
  models.py         # Offer / StoredOffer / Delivery / Stats
  utils.py          # UTC timestamps, hashing, Arabic detection
  logging_setup.py  # rotating file + UTF-8 console logging
scripts/
  check_setup.py    # kickoff self-check
  init_db.py        # schema creation / verification
tests/              # config, DB and utils tests
```

## Data model (schema v1)

- `offers` — one row per unique deal; `content_hash` is the exact-dupe guard.
  `status` is `pending` → `posted`, or `rejected`.
- `post_deliveries` — one row per (offer, channel), so multi-channel fan-out
  can be tracked, retried and rate-limited per channel.
- `state` — small key/value store: `schema_version`, `paused`,
  `last_amazon_poll`.

All timestamps are UTC in `YYYY-MM-DDTHH:MM:SSZ` form, which sorts correctly as text.

## Configuration notes

- Real environment variables override `.env` values, so Docker/deploy overrides
  need no file edits.
- `AMAZON_ENABLED=0` ships Telegram-only. Flip to `1` after the Associates
  account meets the Creators API sales gate; the rest of the pipeline does not
  change.
- `CHANNEL_MODE=all` posts every accepted offer to every target channel.
  `CHANNEL_MODE=route` uses `ROUTE_RULES` (`channel:match`), with match values
  `ar`, `en`, a category keyword, or `*` for catch-all.

## Operational guardrails (baked into config)

- `MAX_POSTS_PER_HOUR` per channel, `POST_INTERVAL_SECONDS` between posts
- `MAX_PER_SOURCE_PER_HOUR` so one source cannot dominate the feed
- `MIN_DISCOUNT_PCT` and `EXCLUDE_KEYWORDS` quality gate
- `FUZZY_THRESHOLD` for near-duplicate titles

## Open decision (Phase 3)

**Q10:** Does the Amazon Associates account have ≥10 qualifying sales in the
last 30 days (rolling)? That decides Creators API Branch A vs Telegram-only
Branch B. Phase 3 works either way; check before that phase starts.

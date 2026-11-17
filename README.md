# Telegram Offers Bot

Automatic bilingual (Arabic/English) deal aggregator: listens to Telegram source
channels, normalizes and dedupes offers, then broadcasts them to one or more
channels run by a single bot.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| **1. Foundations** | Skeleton, `.env` config + validation, SQLite schema/access layer, self-check tooling, tests | ✅ done |
| **2. Telegram listener** | Telethon session, 10 sources, live + 50-msg catch-up, media capture, normalize + hash insert | ✅ done |
| **3. Amazon ingestion** | Creators API (Branch A) or stubbed module behind `AMAZON_ENABLED` (Branch B) | next |
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

### First listener run

```powershell
python listener.py --once          # catch-up only, then exit
python -m scripts.list_offers      # verify offers landed in SQLite
python listener.py                 # catch-up + live listening
```

The first run asks for the **listener account's** phone number and login code
(and 2FA password if set). That account must already be a member of every
channel in `SOURCE_CHANNELS`; join them manually before starting. The session
file (`TG_SESSION_PATH`) persists, so later runs are non-interactive.

`check_setup` prints a per-account checklist plus config warnings and exits
non-zero while a required value is missing. `init_db` creates the schema and is
safe to re-run.

## Layout

```
app/
  config.py         # .env parsing + validation (single source of truth)
  db.py             # SQLite (WAL) schema, migrations and access layer
  ingest.py         # post -> Offer: link pick, price/discount parsing, hash
  listener.py       # Telethon catch-up + live message ingestion
  models.py         # Offer / StoredOffer / Delivery / Stats
  utils.py          # UTC timestamps, hashing, Arabic detection
  logging_setup.py  # rotating file + UTF-8 console logging
listener.py         # listener process entry point
scripts/
  check_setup.py    # kickoff self-check
  init_db.py        # schema creation / verification
  list_offers.py    # inspect recent rows (M1 check)
tests/              # config, DB, ingest and listener tests
```

## Listener behavior (Phase 2)

- On start it reads the last `CATCHUP_LIMIT` (default 50) messages per source,
  oldest first, then keeps listening for new posts.
- Messages are normalized: the best link is picked (product shops win over
  social links; channel invites are ignored), the first meaningful line becomes
  the title, and `$`, `€`, `£`, `ر.س`, `د.إ`, `ج.م`, `د.ك`, `ر.ق` prices are
  parsed (including Arabic-Indic digits and `1.299,00` formats).
- Exact dedupe uses `content_hash` = normalized title + canonical link +
  price. Tracking parameters (`utm_*`, `tag=`, Amazon `/ref=`) are stripped
  before hashing, so the same deal with a different affiliate tag collapses.
- Messages already ingested (`source_channel` + `source_msg_id`) are skipped
  cheaply, so restarts do not re-download media.
- Photos are downloaded to `MEDIA_DIR` (default `data/media`) when ≤
  `MAX_IMAGE_BYTES` (default 1 MB); larger images are skipped and the offer is
  stored text-only. Downloads never crash ingestion.
- `--once`, `--no-catchup` and `--limit N` support quick tests.

## Data model (schema v2)

- `offers` — one row per unique deal; `content_hash` is the exact-dupe guard.
  `status` is `pending` → `posted`, or `rejected`. `image_path` points at a
  downloaded Telegram photo; `image_url` is for remote (Amazon) images.
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

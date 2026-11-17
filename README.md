# Telegram Offers Bot

Automatic bilingual (Arabic/English) deal aggregator: listens to Telegram source
channels, normalizes and dedupes offers, then broadcasts them to one or more
channels run by a single bot.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| **1. Foundations** | Skeleton, `.env` config + validation, SQLite schema/access layer, self-check tooling, tests | ✅ done |
| **2. Telegram listener** | Telethon session, 10 sources, live + 50-msg catch-up, media capture, normalize + hash insert | ✅ done |
| **3. Amazon ingestion** | Creators API client (Branch A) + disabled-by-default poller (Branch B) | ✅ done |
| **4. Filters & dedupe** | rapidfuzz near-dup, discount/keyword/source-cap filters, auto-approve | ✅ done |
| **5. Broadcaster** | `all` / `route` fan-out, AR/EN templates, buttons, rate limits, per-channel error isolation | next |
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

### Amazon poller (optional)

`AMAZON_ENABLED=0` (the default) is the Telegram-only launch branch: the poller
prints a notice and exits without using the network. Once the Associates
account has Creators API access:

```powershell
# .env: AMAZON_ENABLED=1 + credentials + keywords
python amazon_poller.py --dry-run    # fetch and print, no DB writes
python amazon_poller.py --once       # one poll into SQLite
python amazon_poller.py              # poll every AMAZON_POLL_INTERVAL_MINUTES
```

## Layout

```
app/
  config.py         # .env parsing + validation (single source of truth)
  db.py             # SQLite (WAL) schema, migrations and access layer
  ingest.py         # post -> Offer: link pick, price/discount parsing, hash
  filters.py        # keyword/discount/fuzzy dedupe/per-source cap + auto-approve
  amazon.py         # Creators API client + poller (SearchItems -> offers)
  listener.py       # Telethon catch-up + live message ingestion
  models.py         # Offer / StoredOffer / Delivery / Stats
  utils.py          # UTC timestamps, hashing, Arabic detection
  logging_setup.py  # rotating file + UTF-8 console logging
listener.py         # listener process entry point
amazon_poller.py    # Amazon poller entry point
scripts/
  check_setup.py    # kickoff self-check
  init_db.py        # schema creation / verification
  list_offers.py    # inspect recent rows (M1 check)
tests/              # config, DB, ingest, filters, listener and Amazon tests
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

## Amazon behavior (Phase 3)

- **Auth:** OAuth 2.0 client credentials. `AMAZON_CREDENTIAL_VERSION` picks the
  token host (`3.1` North America, `3.2` Europe, `3.3` Far East). Tokens are
  cached until a minute before expiry; a `401` triggers one refresh + retry.
- **Fetch:** one `SearchItems` request per keyword per cycle (max 10 items),
  with `minSavingPercent=AMAZON_MIN_DISCOUNT_PCT`, the `x-marketplace` header,
  and only title/image/price resources to conserve quota.
- **Normalize:** the buy-box, new, in-stock listing wins. Price comes from
  `offersV2`, list price from `savingBasis`, discount from `savings.percentage`
  (computed when absent). The affiliate tag is forced onto the product link.
- **Store:** offers use `source_type=amazon`, `source_channel=amazon`, and the
  search keyword as `category` (usable by `ROUTE_RULES`). Exact-hash dedupe is
  shared with Telegram; a changed price hashes differently on purpose, so price
  drops can be posted as new deals.
- **Failures:** one bad keyword or item cannot stop the cycle; `--once` exits
  non-zero when any search failed, so cron/alerts can notice. `last_amazon_poll`
  is only updated when at least one search succeeded.

## Filtering & dedupe (Phase 4)

Every new offer — Telegram or Amazon — runs through `app/filters.py` before it
can reach the broadcaster. Passing offers stay `status='pending'`, which is the
auto-approved queue (Q5); failures are stored as `rejected` and the reason is
logged.

1. **Excluded keywords** — case-insensitive, Arabic-aware substring match on the
   title (`EXCLUDE_KEYWORDS`).
2. **Minimum discount** — applied only when a discount is known, so a Telegram
   post without a parseable price is not discarded (`MIN_DISCOUNT_PCT`).
3. **Near-duplicate** — compared with the last 500 accepted offers. A canonical
   link match or a rapidfuzz title ratio at/above `FUZZY_THRESHOLD` (default 88)
   is a duplicate only within the same currency and a ±10% price bucket. A
   larger price drop on the same link therefore posts as a new deal.
4. **Per-source cap** — at most `MAX_PER_SOURCE_PER_HOUR` accepted offers per
   `source_channel` in the last hour (Amazon is one source).

Rejected rows reserve their exact `content_hash`, so the same bad post is not
re-ingested later. Offline checks of the rules need no `.env`:

```powershell
pytest tests/test_filters.py
```

## Data model (schema v2)

- `offers` — one row per unique deal; `content_hash` is the exact-dupe guard.
  `status` is `pending` (auto-approved queue) → `posted`, or `rejected` with the
  filter reason in the log. `image_path` points at a downloaded Telegram photo;
  `image_url` is for remote (Amazon) images.
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
- `FUZZY_THRESHOLD` for near-duplicate titles within ±10% price buckets
- `AMAZON_MIN_DISCOUNT_PCT` when the Amazon poller is enabled (also sent to
  the API so quota is not spent on non-deals)

## Amazon launch gate

Creators API access requires **10 qualifying sales in the rolling 30 days**.
Until that gate is met, keep `AMAZON_ENABLED=0` (Branch B): the poller is a
no-op and launch proceeds Telegram-only. Once approved, fill in
`AMAZON_CREDENTIAL_ID`, `AMAZON_CREDENTIAL_SECRET`, `AMAZON_ASSOCIATE_TAG`, set
`AMAZON_ENABLED=1`, and run `python amazon_poller.py --dry-run` before enabling
the loop.

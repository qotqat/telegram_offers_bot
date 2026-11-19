# Telegram Offers Bot

Automatic bilingual (Arabic/English) deal aggregator: watches Telegram source
channels for Amazon deals, copies each post into our own channel with the actual
Amazon product title, a plain product link and our brand, and keeps a deduplicated
SQLite queue. Affiliate links are opt-in. The optional Creators API poller adds
Amazon deals without a Telegram source.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| **1. Foundations** | Skeleton, `.env` config + validation, SQLite schema/access layer, self-check tooling, tests | ✅ done |
| **2. Telegram listener** | Telethon session, newest message per source at startup, live events + latest-message polling, media capture, dedupe | Implemented; bounded newest-message/polling check verified |
| **3. Amazon ingestion** | Creators API client (Branch A) + disabled-by-default poller (Branch B) | ✅ done |
| **4. Filters & dedupe** | rapidfuzz near-dup, discount/keyword/source-cap filters, auto-approve | ✅ done |
| **5. Broadcaster** | Amazon-only copy/rewrite, `all` / `route` fan-out, rate limits, per-channel error isolation | ✅ done |
| **6. Admin & hardening** | Owner-only commands, bounded crash alerts, per-process redacted logs, graceful shutdown | ✅ implemented; live verification pending |
| **7. Deploy** | Docker Compose (3 default services, shared `/data`), backups, runbook | ✅ artifacts ready; local/VPS deployment + soak pending |

## Current launch blockers

The brand is **Bosla Deals**; the current destination is **`@deal_test`**
(singular), `https://t.me/deal_test`. Set `TARGET_CHANNELS`, `BRAND_USERNAME` and
`BRAND_URL` explicitly to that destination; do not rely on older plural defaults.
The three sources are `@deals_me`, `@Sal7lyEgypt`, `@OffersEgyptofficial`.

- `OWNER_ID` needs your **positive numeric user ID**, not `@Mahmoud_Qotqat`.
  Get the `Id` value from `@userinfobot`, set it privately, and send `/start`
  to your own bot so owner alerts can reach you.
- Verify `AMAZON_MARKETPLACE` for the intended storefront. With
  `AMAZON_LINK_MODE=plain` (the default), Amazon-only listener/broadcaster need
  the marketplace but **no tag, tag confirmation or Amazon API credentials**.
  An unconfirmed tag does not prohibit plain-link publishing.
- **Only for affiliate mode or the optional Creators API poller:** your Tracking
  ID remains unverified. Find it in **your own** Associates Central account;
  a source/sample tag is not proof of ownership. Set `AMAZON_ASSOCIATE_TAG`,
  the matching marketplace, and only then `AMAZON_TAG_CONFIRMED=1`. Otherwise
  leave confirmation at `0`; affiliate/API publishing remains blocked.
- Adding the bot to `@deal_test` is not enough: give it administrator **Post
  messages** permission. Join sources with the secondary listener account.

Local checks verified listener login, earlier plain-link/photo posting, newest
message reads from all three sources, bounded periodic monitoring, and read-only
Amazon product-title lookup. At the newest-message test, the sources contained
non-Amazon posts or multi-product promotion pages, so **no new message was
published**. Live delivery of a newly title-replaced offer remains unverified
until an eligible newest product post is available. Admin commands, Docker
deployment and the soak test remain unverified.
See [RUNBOOK.md](RUNBOOK.md) for Windows-first testing, Docker and VPS migration.

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
python listener.py --limit 0 --once # login/session setup only; no initial message reads
python listener.py --once           # check newest message in EACH source, then exit
python -m scripts.list_offers       # inspect any accepted offers in SQLite
python listener.py                  # newest per source, then live events + latest polling
```

The first run asks for the **listener account's** phone number and login code
(and 2FA password if set). That account must already be a member of every
channel in `SOURCE_CHANNELS`; join them manually before starting. The session
file (`TG_SESSION_PATH`) persists; use `--non-interactive` on later unattended
runs to fail rather than prompt if authorization is missing. A non-Amazon newest
message skips that source; the listener does not scan backwards for a deal.

`check_setup` prints a per-account checklist plus config warnings and exits
non-zero while a required value is missing. `init_db` creates the schema and is
safe to re-run.

### Broadcaster

The bot from `BOT_TOKEN` must be an admin with **Post messages** rights in every
channel in `TARGET_CHANNELS`.

```powershell
python broadcaster.py --dry-run          # offline queue preview (no Telegram calls)
python broadcaster.py --once             # one post per channel, then exit
python broadcaster.py --once --limit 3   # up to 3 posts per channel
python broadcaster.py                    # continuous loop
```

Each accepted offer gets one `post_deliveries` row per target channel. A
successful post is marked `sent`; the offer becomes `posted` once every target
has either `sent` or `skipped` (route mismatch). A failed send stays `failed`
and is retried on the next pass.

### Amazon poller (optional)

`AMAZON_ENABLED=0` (the default) disables only the separate Creators API poller:
it prints a notice and exits without using the network. Telegram source copying
still resolves links and fetches product-page HTML; it needs no Amazon API
credentials. Once the Associates account has Creators API access, your own tag
and marketplace are verified, and `AMAZON_TAG_CONFIRMED=1` (required for the API
even when `AMAZON_LINK_MODE=plain`):

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
  links.py          # Amazon detection, plain/affiliate rewrite, brand cleanup
  products.py       # bounded HTML productTitle lookup; no Amazon API credentials
  filters.py        # keyword/discount/fuzzy dedupe/per-source cap + auto-approve
  amazon.py         # Creators API client + poller (SearchItems -> offers)
  listener.py       # newest per source, live events + latest polling
  broadcaster.py    # pending offers -> Telegram messages + delivery bookkeeping
  models.py         # Offer / StoredOffer / Delivery / Stats
  utils.py          # UTC timestamps, hashing, Arabic detection
  admin.py          # private-chat numeric-owner commands + UTC stats
  runtime.py        # bounded/throttled owner alerts + SIGTERM cleanup
  logging_setup.py  # per-process rotating logs + credential redaction
listener.py         # listener process entry point
broadcaster.py      # broadcaster process entry point
amazon_poller.py    # Amazon poller entry point
admin_bot.py        # sole Bot API update poller (owner commands)
scripts/
  check_setup.py    # kickoff self-check
  init_db.py        # schema creation / verification
  list_offers.py    # inspect recent rows (M1 check)
  backup.py         # consistent DB + optional session/config/media ZIP
tests/              # config, DB, ingest, links, filters, listener, broadcaster, Amazon tests
```

## Listener behavior (Phase 2)

- `Config.listener_latest_only` / `LISTENER_LATEST_ONLY` defaults to `True` / `1`:
  startup checks **only the newest message in EACH configured source**.
  A non-Amazon newest message skips that source in Amazon-only mode; there is
  **no backwards scan** for an older deal. Positive `CATCHUP_LIMIT` / `--limit`
  values are capped at **1 per source** by default, even though the legacy
  `CATCHUP_LIMIT` default is 50.
- Continuous runs register `NewMessage` handlers for all resolved sources
  **before startup reads**, then also check each source's latest message every
  `SOURCE_POLL_INTERVAL_SECONDS` (default **30**, minimum **5**). A shared
  ingestion lock serializes startup, events and polling to prevent races;
  source-message and content dedupe prevent repeat ingestion.
- The best link is picked (product shops win over social links; channel invites
  are ignored). Amazon titles always come from the product page's HTML
  `productTitle` via `app/products.py`, never the source headline. The first
  meaningful source line is used only for non-Amazon ingestion when explicitly
  allowed. `$`, `€`, `£`, `ر.س`, `د.إ`, `ج.م`, `د.ك`, `ر.ق` prices are parsed
  (including Arabic-Indic digits and `1.299,00` formats).
- Exact dedupe uses `content_hash` = normalized title + canonical link +
  price. Tracking parameters (`utm_*`, `tag=`, Amazon `/ref=`) are stripped
  before hashing, so the same deal with a different affiliate tag collapses.
- Messages already ingested (`source_channel` + `source_msg_id`) are skipped
  cheaply, so restarts do not re-download media.
- Photos are downloaded to `MEDIA_DIR` (default `data/media`) when ≤
  `MAX_IMAGE_BYTES` (default 1 MB); larger images are skipped and the offer is
  stored text-only. Downloads never crash ingestion.
- `--once` performs startup checks then exits (no live events or polling).
  `--no-catchup` skips startup reads but keeps live events and latest polling.
  `--limit 0` also skips initial reads; combine it with `--once` for login/session
  setup only. Without `--once`, events and polling still run.
- Legacy backfill is explicit opt-in: set `LISTENER_LATEST_ONLY=0`, then use
  `CATCHUP_LIMIT` or `--limit N` to read up to N messages per source, oldest first.
  Periodic polling still checks only the latest message. For example:

  ```powershell
  $env:LISTENER_LATEST_ONLY = '0'
  python listener.py --limit 10 --once
  $env:LISTENER_LATEST_ONLY = '1' # restore newest-only startup
  ```

## Amazon-only copying (current launch)

With `REQUIRE_AMAZON_LINK=1` (the default) a source post is ingested **only**
when it contains an Amazon link. The clickable link may be in the message text,
a hidden text entity, or an inline button.

1. Short links (`link.amazon`, `amzn.to`, `amzn.eu`, `a.co`, …) are resolved to
   their final product page and the ASIN is extracted.
2. `Config.amazon_link_mode` defaults to `plain`; `AMAZON_LINK_MODE` accepts
   `plain|affiliate`. In plain mode every Amazon link is rebuilt as
   `https://AMAZON_MARKETPLACE/dp/ASIN` with **all tracking removed**: no query,
   fragment, source `tag`, `linkCode`, `ascsubtag`, `/ref=` or click metadata.
   Affiliate mode instead adds only `?tag=AMAZON_ASSOCIATE_TAG`, using your own
   confirmed tag; source tracking is still discarded.
3. `app/products.py` fetches the actual title from the HTML `productTitle` span
   and replaces the copied headline while retaining inline price/coupon details.
   Promotion/search pages without an individual product ASIN are skipped, not
   replaced with an older source post. This bounded, credential-free lookup needs
   **no Amazon API credentials**, does not bypass CAPTCHAs, and never falls back
   to the source title or HTML page title. If the title is missing/blocked or
   lookup fails, the post is skipped without being stored; the next source poll
   retries it if it is still the newest message. `--once` needs another run.
4. If any Amazon link in the post cannot be rewritten, the whole post is
   skipped — the bot never publishes someone else's affiliate tag.
5. Source display names, `@usernames`, `t.me` channel links and private invite
   links are replaced with `BRAND_NAME`, `BRAND_USERNAME` and `BRAND_URL`.
6. The finished text is stored in `offers.post_text` and posted verbatim (plain
   text, so Telegram auto-links the URLs). Posts without an Amazon link are
   ignored before filtering, so they never consume the queue.

Plain Amazon-only ingestion and live publishing require a valid marketplace,
**not** `AMAZON_ASSOCIATE_TAG` or `AMAZON_TAG_CONFIRMED=1`. Affiliate mode requires
both your own tag and `AMAZON_TAG_CONFIRMED=1`; a source/sample tag is never
trusted as yours. `AMAZON_ENABLED` controls only the separate API poller.

At send time, queued Amazon URLs are checked against the selected link mode and
marketplace (and your confirmed tag in affiliate mode). Existing queued affiliate
URLs are **blocked in plain mode**, not silently rewritten. Stale/mismatching
entries stay pending with a failed delivery; review/regenerate affected offers
before sending. **No DB schema change or database reset is needed; never reset
the DB to switch modes.**

## Amazon behavior (Phase 3)

This section describes only the optional Creators API poller, not Telegram
copying or HTML title lookup. Enabling it requires API access/credentials and
your own confirmed tag, regardless of `AMAZON_LINK_MODE`.

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

## Broadcasting (Phase 5)

- Reads the oldest `pending` offer for each target channel. Offers already sent
  or skipped for that channel are excluded.
- `CHANNEL_MODE=all` sends every offer to every target. `CHANNEL_MODE=route`
  matches `ROUTE_RULES` (`ar`, `en`, `*`, or a category/keyword) and falls back
  to the first target channel when nothing matches.
- Uses `offers.post_text` when present; Amazon-poller rows fall back to a
  generated title/price/link message.
- Photos are sent with the text as a caption. Text longer than Telegram's
  1024 UTF-16-unit caption limit is sent as a separate message after the photo.
  A failed photo falls back to text-only.
- Long messages are clipped to 4096 UTF-16 units (emoji count as two), keeping
  the product link at the end (plain or affiliate according to the selected mode).
- One delivery row per (offer, channel) records `sent`, `failed` or `skipped`;
  the offer becomes `posted` only when every target channel is delivered or
  skipped.
- `MAX_POSTS_PER_HOUR` is enforced per channel; `POST_INTERVAL_SECONDS` is the
  delay between broadcaster cycles. A failed channel does not stop the others.
  Telegram flood waits persist a per-channel retry deadline across restarts;
  they do not trigger photo-to-text fallback or delay healthy channels.
- `--dry-run` is offline: no bot initialization, Telegram calls or owner alerts.
  It still opens/initializes/migrates SQLite, so back up before upgrading and
  do not run migrations while workers are active.

## Data model (schema v3)

- `offers` — one row per unique deal; `content_hash` is the exact-dupe guard.
  `status` is `pending` (auto-approved queue) → `posted`, or `rejected` with the
  filter reason in the log. `image_path` points at a downloaded Telegram photo;
  `image_url` is for remote (Amazon) images. `post_text` is the final copied
  and rebranded text (actual product title and selected plain/affiliate link).
- `post_deliveries` — one row per (offer, channel), so multi-channel fan-out
  can be tracked, retried and rate-limited per channel.
- `state` — small key/value store: `schema_version`, `paused`,
  `last_amazon_poll`, per-channel `retry_after:<channel>` deadlines.

All timestamps are UTC in `YYYY-MM-DDTHH:MM:SSZ` form, which sorts correctly as text.

## Configuration notes

- Real environment variables override `.env` values, so Docker/deploy overrides
  need no file edits.
- `REQUIRE_AMAZON_LINK=1` (default) makes the listener Amazon-only.
  `AMAZON_LINK_MODE=plain` (default; accepts `plain|affiliate`) requires
  `AMAZON_MARKETPLACE`, but no tag, confirmation or Amazon API credentials.
- `AMAZON_LINK_MODE=affiliate` requires `AMAZON_ASSOCIATE_TAG` and
  `AMAZON_TAG_CONFIRMED=1`. Set confirmation only after checking your own tag
  and matching marketplace; software cannot prove ownership. Confirmation at
  `0` blocks affiliate/API use, **not plain Telegram copying/publishing**.
- `LISTENER_LATEST_ONLY=1` (default) caps startup reads at one message per source;
  set `0` only for explicit legacy backfill. `SOURCE_POLL_INTERVAL_SECONDS`
  defaults to 30 (minimum 5) for additional latest-message checks.
- `BRAND_NAME`, `BRAND_USERNAME` and `BRAND_URL` control the replacement for
  source channel names, mentions and links.
- `AMAZON_ENABLED=0` disables only the optional Creators API poller; Telegram
  copying and HTML title lookup do not need it. Enable only after API approval,
  credentials and your own confirmed tag; API use remains tag-gated in plain mode.
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

## Admin & hardening (Phase 6)

Run `python admin_bot.py` in a third terminal. Only the configured numeric owner,
in a **private chat**, may use `/start`, `/pause`, `/resume` and `/stats`. Other
users/groups receive no stats and cannot change state. Run exactly one admin
poller for a bot token; broadcasters only send messages and do not consume updates.

- `/pause` persists to SQLite and stops publication, not Telegram collection.
  The broadcaster checks between offers/channels; a send already in flight may
  still complete. `/resume` releases queued work. The optional API poller also
  honors pause.
- `/stats` reports today's UTC collected/rejected counts, sent deliveries (one
  per target), all-time pending queue and per-source counts.
- Unexpected worker failures send a best-effort owner DM (requires bot `/start`).
  Alerts include only component/error class, never raw exceptions or tokens.
  They time out after 10 seconds and repeat at most every five minutes per
  component/error class in each process. Inspect local logs for details.
- Rotating files are separate per process (`data/bot-listener.log`,
  `data/bot-broadcaster.log`, `data/bot-admin.log`, `data/bot-amazon.log` by
  default). Explicit matching per-process filenames are preserved. Known
  credentials and token-shaped URLs are redacted, including tracebacks.
- Ctrl+C/SIGTERM runs worker cleanup. Docker uses a 30-second stop grace period;
  abrupt termination and power loss can still interrupt a send. Delivery is
  **at least once**: a crash after Telegram accepts a send but before SQLite
  records it can duplicate that post on retry. Long photo posts use two sends,
  so interrupted retries can also repeat the photo.
- `listener.py --non-interactive` requires an existing authorized session and
  fails without prompting. First authenticate interactively on a trusted PC.

## Deployment & backups (Phase 7)

[RUNBOOK.md](RUNBOOK.md) covers local Windows startup, the 1–2 hour manual soak
checklist, non-root Docker Compose, safe stop/restart, queue review, and later
single-VPS migration. Docker defaults to listener/broadcaster/admin; the optional
Amazon poller is under the `amazon` profile. No inbound ports are needed.

Before a backup, stop **all** processes, not just `/pause`:

```powershell
python -m scripts.backup --services-stopped --env-file .env --media-dir data/media
```

The CLI uses SQLite's backup API (not a raw copy of a live WAL database), adds
session files and optional config/media, and creates a new private ZIP without
overwriting earlier archives. `--services-stopped` is your acknowledgement, not
a process detector. ZIPs are **unencrypted secrets**: restrict ACLs, keep a secure
off-device copy, and never commit/share them. No backup of real data was run here.

## Amazon launch gate

Creators API access requires **10 qualifying sales in the rolling 30 days**.
Until that gate is met, keep `AMAZON_ENABLED=0` (Branch B): the poller is a
no-op and the bot copies Amazon posts from Telegram instead. Default plain-mode
copying needs a marketplace but **no affiliate tag, confirmation or API
credentials**; product titles come from bounded HTML lookup. Affiliate copying
still requires your own tag and `AMAZON_TAG_CONFIRMED=1`.
Once API access is approved, verify your own tag/marketplace, set confirmation,
fill in `AMAZON_CREDENTIAL_ID`, `AMAZON_CREDENTIAL_SECRET`, set
`AMAZON_ENABLED=1`, and run `python amazon_poller.py --dry-run` before enabling
the loop. The API dry run uses the network and remains confirmed-tag gated even
in plain link mode; old affiliate queue entries must be reviewed/regenerated
before plain-mode publication.

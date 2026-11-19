# Phase 7: deployment and recovery

These are deployment artifacts and operator instructions, **not evidence of a
live deployment, Docker build/run, or completed soak test**. The integrated runtime
includes `admin_bot.py`, listener `--non-interactive` support, and worker SIGTERM
cleanup through `app/runtime.py` and the entry points. The admin bot uses PTB's
polling shutdown lifecycle and closes its database on exit. No web server,
inbound application port, or public health endpoint is needed.

## 1. Launch gates: plain by default, affiliate opt-in

Current destination/brand configuration (edit your private `.env` locally;
`@deal_test` is singular, not the older `@deals_test` default):

```dotenv
SOURCE_CHANNELS=@deals_me,@Sal7lyEgypt,@OffersEgyptofficial
TARGET_CHANNELS=@deal_test
CHANNEL_MODE=all
BRAND_NAME=Bosla Deals
BRAND_USERNAME=@deal_test
BRAND_URL=https://t.me/deal_test
REQUIRE_AMAZON_LINK=1
AMAZON_LINK_MODE=plain
AMAZON_TAG_CONFIRMED=0
AMAZON_ENABLED=0
LISTENER_LATEST_ONLY=1
SOURCE_POLL_INTERVAL_SECONDS=30
```

**Plain mode needs a marketplace, not an affiliate tag.** Set and verify
`AMAZON_MARKETPLACE` for the intended storefront; do not rely on an unverified
regional default. `Config.amazon_link_mode` defaults to `plain`, and
`AMAZON_LINK_MODE` accepts `plain|affiliate`. With the Amazon-only configuration
above, the listener and live broadcaster require a valid marketplace but **no
`AMAZON_ASSOCIATE_TAG`, tag confirmation or Amazon API credentials**. Leave
`AMAZON_TAG_CONFIRMED=0` if ownership is unverified; this does not prohibit
plain-link publishing after the remaining permission/content checks.

**Affiliate mode only:** before setting `AMAZON_LINK_MODE=affiliate`, sign in to
**your own** Associates account for the intended region and retrieve the exact
Store ID / Tracking ID. Set `AMAZON_ASSOCIATE_TAG` to that verified ID and
`AMAZON_MARKETPLACE` to its matching host. **Only then set
`AMAZON_TAG_CONFIRMED=1`.** Never infer ownership from a brand/Telegram username
or source/sample tag. A syntactically valid tag or flipped flag proves nothing.
The separate enabled Creators API poller requires your own confirmed tag,
including its networked `--dry-run`, **even in plain mode**. API access approval
and credentials are additional requirements, not prerequisites for Telegram
source copying.

Plain mode strips all tracking and rebuilds each Amazon URL as
`https://AMAZON_MARKETPLACE/dp/ASIN` (no query, fragment or `/ref=` metadata).
Affiliate mode discards source tracking and adds only your confirmed own tag.
Both modes fetch the actual product title from the HTML `productTitle` span via
`app/products.py`, replacing the copied headline. This bounded lookup needs no
API credentials and never bypasses CAPTCHAs or falls back to the original/HTML
page title. Missing/blocked titles skip ingestion without storing the post; the
next source poll retries if that message is still latest (`--once` needs a rerun).

Before a live send, the broadcaster checks queued Amazon URLs against the
selected mode and marketplace (and your confirmed tag for affiliate mode).
**Existing queued affiliate URLs are blocked in plain mode** and delivery is
marked failed, rather than sent. This is not an automatic queue rewrite:
previously stored `post_text` may still have an old tag/brand/title. Review and
regenerate or quarantine affected offers before live broadcasting; changing
configuration alone does not repair stored posts. **There is no DB schema change
for this behavior; never reset the database to switch modes.**

`AMAZON_ENABLED=0` only disables the optional Creators API poller. **It does not
disable Telegram copying, HTML title lookup or publishing.** Plain Telegram
copying needs no API access or tag, but does contact Amazon for link resolution
and product-page HTML. Keep the poller disabled until the relevant API access,
regional credentials and own confirmed tag are ready; no approval is assumed.

Bounded local checks verified listener login, newest-message reads for every
source, periodic latest checks, and read-only Amazon product-title lookup. Earlier
plain-link/photo posting to `@deal_test` also succeeded. The newest-message run
published nothing because the latest source posts were non-Amazon posts or
promotion pages without an individual product ASIN. A newly title-replaced live
delivery is therefore still unverified. Owner DM/admin commands, explicit source
membership, Docker/VPS deployment and soak results remain unverified.

Account checklist:

- Use a secondary Telegram user account for Telethon. Join all three source
  channels manually with that account. Set `TG_API_ID` and `TG_API_HASH` privately.
- Create/use the bot from BotFather; set `BOT_TOKEN` privately. Make it an admin
  in `@deal_test` with **Post messages** rights (and in any later target).
- `OWNER_ID` must be your **numeric Telegram user ID**. `@Mahmoud_Qotqat` is a
  username, not a numeric ID. Obtain the ID from `@userinfobot`; do not guess it.
- Open the bot's DM from the owner account and send `/start`, so it can send
  owner alerts. Confirm admin commands reject other users and work for the owner.
- Never paste tokens, login codes, 2FA passwords, session files or config output
  into tickets/chat. Do not publish expanded `docker compose config` or container
  inspect output: `env_file` secrets appear there. Docker-host access is privileged.

## 2. Local Windows setup and startup

Use PowerShell in `N:\repos\telegram offers bot`, Python 3.12, and a private
`.env` created/edited locally. Install dependencies (this step needs package
registry access). Commands below are operator steps, not steps already executed.

```powershell
Set-Location 'N:\repos\telegram offers bot'
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
New-Item -ItemType Directory -Force data, data\logs, data\media | Out-Null
$env:TG_SESSION_PATH = 'data/listener_session'
$env:DB_PATH = 'data/offers.db'
$env:MEDIA_DIR = 'data/media'
$env:LOG_PATH = 'data/logs/setup.log'
python -m scripts.check_setup
python -m scripts.init_db
```

Do not continue past failed checks. Independently verify the numeric owner ID,
marketplace, bot DM and target-channel rights; verify tag ownership/region only
for affiliate/API use. A successful config check cannot prove those. Real
environment variables override `.env`.
Do not put Linux `/data/...` storage paths into your Windows process environment.

After verifying the selected mode's requirements (marketplace only for plain;
own confirmed tag additionally for affiliate), establish the listener session
**interactively on this trusted machine**, with the broadcaster still stopped:

```powershell
$env:LOG_PATH = 'data/logs/listener.log'
python listener.py --limit 0 --once # login/session setup only; skip message reads
python listener.py --once           # newest message in EACH source, then exit
python -m scripts.list_offers       # inspect any accepted offers
```

These commands authenticate with Telegram; the second also reads source
messages, resolves Amazon links and fetches product titles. They are **not
offline**. Enter phone/code/2FA only at local prompts. Expect
`data/listener_session.session` (the configured basename has no `.session`
suffix). Never run two listeners against that session, including local +
Docker/VPS copies. Later use `--non-interactive`: an absent/unauthorized session
fails instead of prompting.

By default `Config.listener_latest_only=True` / `LISTENER_LATEST_ONLY=1` checks
**only the newest message in each source**. If it is non-Amazon, that source is
skipped without scanning backwards. Positive `CATCHUP_LIMIT` / `--limit` values
are capped at 1 per source, even though legacy `CATCHUP_LIMIT` defaults to 50.
Set `LISTENER_LATEST_ONLY=0` explicitly for legacy backfill using `--limit N` or
`CATCHUP_LIMIT` (oldest first); restore `1` afterwards.

Continuous runs register `NewMessage` events for all resolved sources **before
startup reads**, then additionally check each source's latest message every
`SOURCE_POLL_INTERVAL_SECONDS` (default 30, minimum 5). A shared ingestion lock
prevents startup/event/poll races; source-message and content dedupe suppress
repeats. `--once` exits after startup without events/polling. `--no-catchup` or
`--limit 0` skips only initial reads in a continuous run: events and polling
still run. Only `--limit 0 --once` is login/session setup without message reads.

For an **offline queue preview**, with the stack stopped:

```powershell
$env:LOG_PATH = 'data/logs/broadcaster.log'
python broadcaster.py --dry-run
```

The broadcaster's normal `--dry-run` preview path is intentionally **offline**:
it does not construct or initialize a Telegram `Bot`, contact Telegram/Amazon,
send posts, or write delivery records. It does, however, open/create and migrate
SQLite and write local logs, so it is **not a read-only database inspection**.
Back up first or point `DB_PATH` at a scratch copy if migrations must not touch
the original. Preview bypasses the live link-mode gate/final-send check and does
not prove that queued links are publishable or that credentials/rights work.

Error-path caveat: the shared `app/runtime.py` worker wrapper can currently
attempt an owner DM if the preview raises an exception and owner alert credentials
are configured. Use OS-level network isolation if even error-path network access
must be impossible; this caveat does not affect the normal offline preview path.

After all launch gates and queued-content review, open **three separate activated
PowerShell terminals** in the project root. In each terminal set common paths
(the variables above do not carry into a newly opened terminal):

```powershell
.\.venv\Scripts\Activate.ps1
$env:TG_SESSION_PATH = 'data/listener_session'
$env:DB_PATH = 'data/offers.db'
$env:MEDIA_DIR = 'data/media'
```

Then run exactly one process per terminal:

```powershell
# Terminal 1
$env:LOG_PATH = 'data/logs/listener.log'
python listener.py --non-interactive

# Terminal 2 (selected mode requirements, permissions + queue reviewed; CAN publish)
$env:LOG_PATH = 'data/logs/broadcaster.log'
python broadcaster.py

# Terminal 3 (owner-only admin bot)
$env:LOG_PATH = 'data/logs/admin.log'
python admin_bot.py
```

Stop with Ctrl+C in **every** terminal and wait for exit before backup, migration,
or switching hosts. `/pause` controls publishing; it is not a process stop and
does not stop listener/session writes.

## 3. Docker Compose on one host

Use Docker Engine/Desktop with Linux containers and Compose v2. The Dockerfile
uses `python:3.12-slim`, production `requirements.txt` only, and UID:GID
**10001:10001**. Source is root-owned and the runtime is non-root. The allowlisted
`.dockerignore` excludes `.env`, `.env.*`, sessions, data, logs, virtualenvs, Git,
exports and backups from the build context. Do not add secret build args, embed
credentials in source, or broaden the allowlist to the whole repository.

All three default services (`listener`, `broadcaster`, `admin`) bind **the same
`./data:/data` on the same Docker host**. Explicit Compose environment overrides:

| Setting | Container path |
|---|---|
| `TG_SESSION_PATH` | `/data/listener_session` |
| `DB_PATH` | `/data/offers.db` |
| `MEDIA_DIR` | `/data/media` |
| `LOG_PATH` | `/data/logs/listener.log`, `broadcaster.log`, `admin.log` (per process) |

The `process_log_path()` helper in `app/logging_setup.py` preserves these explicit
per-process filenames (and `/data/logs/amazon.log` for the optional poller); it does
not add a second process suffix. A generic shared name such as `bot.log` instead
gets a component suffix so independent rotating handlers do not share one file.

`env_file: .env` injects config at runtime; the file is neither baked into the
image nor mounted into `/app`. No secrets are supplied in Compose itself. Do not
share Docker state, sessions or this DB between machines. SQLite WAL requires
local filesystem locking/shared memory: **no NFS/SMB/network drive or synced
folder** for production `data`. The Windows `N:` project path may be a mapped
network drive; if so, move the working copy/data to a local disk before running.
Use a single always-on VPS with persistent local disk, not ephemeral/free web
hosting or multiple replicas. No application port or web service is exposed.
The services need outbound Telegram/Amazon connectivity, DNS and correct time.

### Ownership (Linux VPS)

A bind mount hides image directory ownership. Provision the host data directory
and, after restoring files, set ownership and permissions while services are
stopped. From the deployed project directory on a trusted Linux host:

```sh
mkdir -p data/logs data/media
sudo chown -R 10001:10001 data
sudo chmod -R u=rwX,go= data
chmod 600 .env
```

The account running Compose must be able to read `.env`; the app only needs its
injected values, not filesystem access to that file. Run backups as a trusted
account allowed to read UID 10001's data (for example via `sudo`), then protect the
output. Do not fix permission failures with `chmod 777` or by running the app as
root. Docker Desktop handles bind ownership differently: restrict Windows ACLs
and verify the non-root container can write data/logs/media, DB and session. Do
not assume Linux chmod changes secure files on Windows.

### Build, initialize, start, stop

Only after session provisioning, marketplace/selected-mode checks, queue review,
target posting permissions and ownership setup. Your own verified Tracking ID
and `AMAZON_TAG_CONFIRMED=1` are additional requirements **only for affiliate/API
use**, not plain Telegram copying (these commands may access the network):

```sh
docker compose build
docker compose run --rm --no-deps admin python -m scripts.init_db --db /data/offers.db
docker compose up -d listener admin
docker compose logs --tail 100 listener admin
# Last publishing gate: this service sends queued posts immediately.
docker compose up -d broadcaster
docker compose ps -a
docker compose logs --tail 100 listener broadcaster admin
```

No service dependency/readiness order is implied by Compose. The one-off init
runs before startup to avoid racing initial schema creation/migrations. Do not
run schema migrations against live services. A running container is not proof
of an authorized listener, a working owner DM, or a successful post.

```sh
docker compose --profile amazon stop
# Check all profiles, including a previously enabled optional poller.
docker compose --profile amazon ps -a
```

`init: true` forwards signals/reaps children; commands use exec-form Python.
Compose sends **SIGTERM**, allows **30 seconds** for cleanup, then may SIGKILL.
`app/runtime.py` and the worker entry points handle termination so worker cleanup
blocks close Telegram/SQLite; the admin entry point uses PTB's signal handling
and polling shutdown, then closes its DB. The three core services use
`restart: unless-stopped`; explicitly stopped services stay stopped until started
again. Verify graceful shutdown during the soak rather than assuming it. Do not
use `docker kill` for normal stops or regard `docker pause` as a clean stop.

### Optional fourth Amazon service

Default `up` starts only the three core services. Leave `AMAZON_ENABLED=0` until
approved. After confirming API access, your exact Tracking ID, matching regional
marketplace, credentials and search configuration, ensure
`AMAZON_TAG_CONFIRMED=1`, then set `AMAZON_ENABLED=1` privately and run:

```sh
docker compose --profile amazon run --rm --no-deps amazon python amazon_poller.py --dry-run
docker compose --profile amazon up -d amazon
```

This dry run **calls Amazon**. The poller's separate log is
`/data/logs/amazon.log`. It uses `restart: on-failure:3`, not `unless-stopped`:
when disabled it exits successfully once and does not loop. Failed starts retry
at most three times; monitor/restart deliberately, including after a host reboot.
Do not enable the profile merely because the default stack is running. Disable
with `docker compose --profile amazon stop amazon` and restore
`AMAZON_ENABLED=0`; recreate affected services if changing their injected config.

## 4. Sensitive offline backups

`scripts/backup.py` uses only the Python standard library; it never imports app
config, automatically loads `.env`, or contacts Telegram/Amazon. **Stop every
listener, broadcaster, admin and optional poller, including local copies and
one-off commands.** Wait for exit. `/pause` and `docker pause` are insufficient.
The required `--services-stopped` is your explicit acknowledgement, **not automatic
verification or a lock detector**. Do not run against a changing session/config.

From the root, after stopping the full stack as above (or all Windows terminals):

```sh
python -m scripts.backup --services-stopped --destination data/backups --db-path data/offers.db --session-path data/listener_session --env-file .env --media-dir data/media
```

Use the activated venv on Windows (`python`), or `python3`/a host venv on Linux.
On Linux, if only Docker provides Python, the stopped stack can run this dedicated
one-off process with runtime config but without mounting the secret file:

```sh
docker compose run --rm --no-deps admin python -m scripts.backup --services-stopped --destination /data/backups --db-path /data/offers.db --session-path /data/listener_session --media-dir /data/media
```

That variant **does not include `.env`**; copy/encrypt it separately while still
stopped using a trusted host account. A one-off backup process does not start the
admin bot. Do not start the stack until the backup finishes successfully.

Options/contents:

- `--db-path` is mandatory in effect (default `data/offers.db` must exist). The
  source is opened read-only and SQLite's **backup API** captures committed WAL
  data consistently. The snapshot is integrity-checked and stored as one
  `data/offers.db`, without offer-DB `-wal`/`-shm` files. Do not substitute a raw
  copy of a running DB file or use immutable mode to ignore its WAL.
- `--session-path` accepts a basename or `.session` file. A missing session is
  allowed; available session journal/WAL/SHM companions are included, and orphan
  companions cause a failure. They are copied, not independently snapshotted,
  hence the full shutdown requirement. Backups without a session need a new
  trusted interactive login before non-interactive startup.
- `.env` is **opt-in** via `--env-file`; an explicitly requested missing file is
  an error. It is copied verbatim as `.env`, never parsed or printed. Omit the
  option if deliberately storing config separately.
- `--media-dir` is optional; if passed it must exist. Its files are stored under
  `data/media/`. Omit it if no directory exists, and record that the backup has
  no images. Symlinks/junctions and unsafe cross-platform filenames are refused.
  Unreadable media directories or other directory traversal errors abort the
  backup before SQLite snapshot/archive creation; fix access permissions or
  filesystem errors and retry. Media archive entries are ordered deterministically.
- Archive names are UTC-timestamped `offers-backup-...zip`, exclusively created:
  collisions fail, never overwrite. The output cannot be a source file or be
  inside a selected media tree. Failed partial archives are removed. ZIP member
  names are canonical relative paths, not private source/drive paths.
- Custom input names are normalized to `data/offers.db`,
  `data/listener_session.session` (plus companions), `.env`, and `data/media/...`.
  Restore to these defaults or move files/set storage config deliberately.
  Logs, code, virtualenvs and arbitrary files from `data` are not included.

ZIPs contain credentials/login sessions and **are not encrypted**. POSIX archives
are created mode 0600 and new destination directories 0700; on Windows enforce
private NTFS ACLs. Snapshot staging uses a temporary directory: use encrypted
local storage for temp and output; cleanup is not secure erasure. Keep backup
files under ignored `data/backups/` or outside the repo; never add them to Git,
email, public shares or image contexts. Use trusted encryption tooling and SSH/SFTP
with verified host keys for off-host copies. Set retention and periodically test
restores. After a successful backup, restart only the intended services; do not
start local and VPS copies together.

## 5. Restore / Windows-to-VPS migration

1. Stop **all** processes on the source and destination, including the optional
   profile. Back up the destination's current state before replacing anything.
   Never let two deployments publish from the same queue/token/session.
2. Transfer code from the intended integrated revision separately, plus
   an encrypted backup/private config over SSH/SFTP with a verified host key.
   Do not transfer `.venv`, logs or Git credentials. Install dependencies/build
   the image on the destination, not by copying a Windows venv to Linux.
3. Validate the archive checksum from a trusted channel and inspect/test the ZIP
   locally. Extract **only a backup you trust** into a new private empty staging
   directory, never directly over the running project. Normal extraction does
   not make an untrusted ZIP safe. Confirm `data/offers.db` opens and
   `PRAGMA quick_check` returns `ok` before installing it.
4. While stopped, move the old `offers.db`, **and its `offers.db-wal` and
   `offers.db-shm` companions (and any `offers.db-journal`) together** to a separate
   rollback directory. Never delete a WAL while its database might still be in
   use or before preserving the original set. Install the snapshot alone as
   `data/offers.db`; do not mix it with old sidecars. The app will enable WAL
   again at startup. Preserve/replace the session and its companions as a set
   too; never pair restored session bytes with old session sidecars.
5. Install the optional saved `.env` privately (or recreate it), session and
   media under the documented defaults. A Telethon SQLite `.session` can usually
   be transferred Windows-to-Linux while completely stopped, but may be revoked
   or require login again. If so, stop everything and provision interactively
   on a trusted machine; the production listener must still never prompt.
6. **Image paths are stored in SQLite as strings.** A Windows absolute path such
   as `N:\...\data\media\...`, or backslash-relative path, is not portable to
   Linux. Even forward-slash `data/media/...` must resolve under the destination
   working directory; in Docker it refers to `/app/data`, not `/data`. New
   Docker ingests use `/data/media/...`. Before publishing on the new host,
   review existing `offers.image_path` and migrate only that field's known old
   prefix/separators to `/data/media/...` (or a correct local relative path).
   Back up first, preserve `image_url`/other fields, and verify the files exist.
   This backup tool deliberately does not rewrite queue data or image paths.
7. Apply UID/GID ownership/ACLs and verify target/owner values (`@deal_test`,
   singular, is the current destination). Verify the restored link mode and
   marketplace; plain mode needs no tag or confirmation. For affiliate/API use,
   re-verify your own tag/marketplace before honoring `AMAZON_TAG_CONFIRMED=1`;
   a saved flag is not evidence of ownership. Run DB initialization/migrations
   **before** starting services; this link/title behavior needs no schema change
   or DB reset. Review pending/delivery state and stored `post_text`: the final-send
   guard blocks old affiliate URLs in plain mode and mismatched tags/marketplaces
   but does not regenerate old titles/posts. Review/regenerate or quarantine them,
   never reset the DB. Restoring an older snapshot can resend posts Telegram
   accepted after that snapshot, or during an unrecorded send. Do not blindly
   resume a restored queue.
8. Start listener + admin first, check the session/DM/stats, then explicitly start
   the broadcaster after review. Keep the previous host stopped. Repeat the soak
   checklist after migration; ensure a subsequent backup/restore also works.

## 6. 1–2 hour soak checklist (operator to perform)

Record the code revision, start/end time, host, errors and outcomes without
secrets. **This checklist has not been executed by creating these artifacts.**
Only run it after the selected-mode/permissions and queued-content checks.
Plain mode needs no tag; never use a guessed tag for an affiliate/API experiment.
The actual-product-title + plain-link live test is still pending.

- [ ] All three core services stay up for 1–2 hours; no restart/authentication or
  bot polling conflicts; exactly one listener and one admin poller exist.
- [ ] Listener has an authorized session and all three configured channels
  resolve. Default startup checks only the newest message in each source;
  a non-Amazon newest message skips that source without scanning history.
- [ ] Live events and latest-message polling (default 30 seconds, minimum 5)
  both ingest offers; overlapping startup/event/poll checks produce no duplicates.
  Missing/blocked HTML `productTitle` skips ingestion and retries on the next
  poll if still latest, without original-title fallback or CAPTCHA bypass.
- [ ] Confirm the selected mode/marketplace. For plain mode no tag/confirmation
  is needed; for affiliate/API use set `AMAZON_TAG_CONFIRMED=1` only after
  independent own-tag/marketplace verification. Review/regenerate queued affiliate
  URLs blocked in plain mode; do not reset the DB. Offline preview alone does
  not exercise the final-send check.
- [ ] Inspect posts in `@deal_test`: Bosla Deals / `@deal_test` branding,
  **actual Amazon product title** and correct marketplace. Plain mode URLs must
  be tracking-free `/dp/ASIN` links (no tag/query/fragment); affiliate mode must
  use only your confirmed own tag. No source tracking/branding, appropriate
  images/text, existing media paths and no unexpected targets.
- [ ] Check discount/keyword/source-cap filters, pending/post-delivery state and
  per-channel cadence/rate limits; no duplicate sends or silent queue buildup.
- [ ] Owner DM receives alerts, `/stats` is sane, `/pause` stops publishing and
  `/resume` restores it; unauthorized users cannot control the bot. Test pauses
  with enough time for any already in-flight send to finish.
- [ ] Review each process's distinct rotating log and bounded Docker logs; no
  recurring DB locks, permission errors, runaway disk growth or exposed secrets.
- [ ] Stop the whole stack: clean disconnect/DB close within 30 seconds, no
  forced-kill symptoms. Restart: existing session, no prompts, state retained,
  no unexpected catch-up floods or duplicate sends. Check host reboot policy.
- [ ] Stop again, create a backup, and restore it into an **isolated scratch
  directory only**. Verify ZIP integrity, DB quick-check, expected rows, session,
  optional config/media and permissions without starting a second live bot.
- [ ] Document rollback/recovery results and schedule encrypted off-host backups.

If any item fails, stop broadcasting, preserve logs/state privately, and resolve
it before treating the deployment as production-ready. Docker artifacts alone
are not a completed deployment or proof of live readiness.

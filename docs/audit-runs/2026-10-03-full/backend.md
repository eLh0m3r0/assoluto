# Backend & ops audit — 2026-10-03-full

Auditor: `backend-auditor` · Commit: `015496f` (`main` == `production`, image
`ghcr.io/elh0m3r0/assoluto:015496f` running on the VPS, container up 6 weeks).
Prod access: read-only `psql` SELECTs, `docker ps/stats/logs/system df/images`,
`df`, `crontab -l`, `ls` of the backup directory. Nothing was restarted or written.

## Baselines

| Check | Previous (2026-07-03-1507) | Now | Verdict |
|---|---|---|---|
| `ruff check .` | clean | **clean** | — |
| `ruff format --check .` | clean | **162 files clean** | — |
| mypy `app/` errors | 0 (89 files) | **1 (91 files)**: `app/routers/orders.py:692` | **regression** → BE-11 |
| pytest | green | **573 passed, 0 skipped, 78.5 s**, 12 warnings (slowapi `asyncio.iscoroutinefunction` DeprecationWarning on Py 3.14, local only; prod/CI are 3.12) | green; no test > 5 s |
| Coverage (`--cov=app`) | — | **76 %** total (7261 stmts / 1707 missed) | gaps → BE-12 |
| GDPR endpoint tests | present | `tests/test_gdpr.py` covers `/app/admin/profile/{export,delete}` and `/app/me/profile/{export,delete}` incl. negative paths | OK (platform Identity has no GDPR route at all — F-12, security's scope) |
| §6 core → `app.platform` imports | 11 lazy/guarded sites | same 11 sites (`templating.py:315`, `main.py:81,429,496`, `models/__init__.py:26`, `auth_service.py:304,404`, `gdpr_service.py:157`, `order_service.py:365`, `attachment_service.py:114`, `invoice_pdf_service.py:51`) | unchanged, previously accepted |
| §2 commit before `add_task` | — | every DB-writing call site commits first (`attachments.py:159`, `orders.py:533,1295,1382,1457,1533`, `tenant_admin.py:191,485`, `customers.py:329,486`, `signup.py:243`). Read-only sites that do **not** commit hold their connection through SMTP → BE-08 | rule kept; side effect found |
| §13 bare `read_session` | — | only `deps.py:263` (principal resolver, which checks tenant itself) and the two helpers in `security/session.py` | OK |
| Advisory lock IDs | unique | 42_001…42_007 all unique (`periodic.py:24-45`, `main.py:90,144`) | OK |
| Alembic chain | single head | single head `1008_order_assignment`; one intentional branch at `0008` merged by `0010`; every revision has `downgrade()` | OK |
| Prod schema vs repo migrations | — | `information_schema.columns` (233 rows) and `pg_indexes` (71 rows) **byte-identical** between prod and a local DB migrated to `1008` | **no drift** |
| ORM vs migrations (`alembic check`) | — | no missing columns; only index-name drift + billing tables invisible to `env.py` metadata | → BE-23 |
| Commits in last 14 days | — | **0** (last: notification redesign 2026-08-19, shipped with 2 new test files, +817 test lines) | advisory: nothing to flag |
| i18n extract (documented command, temp path) | — | 1171 msgids, **0 untranslated, 0 fuzzy** in cs/de; repo `.pot` in sync with source | OK; cruft → BE-22 |

Prod facts used below: DB 9.8 MB, 22 tenants, 29 orders, 46 attachments (4 MB),
subscriptions 2 active / 10 trialing / 8 canceled, `max_connections=100`,
VPS 2 vCPU / 3.8 GB RAM / **no swap** / 38 GB disk.

---

### BE-01 — There is no off-site backup at all: nightly dumps sit on the same disk as the database, and the script can't even see the off-site settings
- Severity: P0
- Status vs 2026-07-26: persisted (F-01, second half; the first-half `sync`→`copy` fix is moot because off-site sync has never run) · also F-25 (unencrypted)
- Evidence: VPS `crontab -l` → `0 3 * * * cd /opt/assoluto && PORTAL_BACKUP_DIR=/home/deploy/backups PORTAL_KEEP_DAYS=14 ./scripts/backup.sh`. `scripts/backup.sh:28-83` only reads `RCLONE_REMOTE` / `BACKUP_GPG_RECIPIENT` from the process environment and **never sources `/etc/assoluto/env`**, so `docs/BACKUP_RESTORE.md:152` ("If `RCLONE_REMOTE` is set in `/etc/assoluto/env` …") describes behaviour the code doesn't have. On the VPS: `which rclone` → nothing, `~/.config/rclone` doesn't exist, `/etc/assoluto/env` has no `RCLONE_*`/`BACKUP_*` keys, and `grep -c rclone ~/assoluto-backup.log` → **0** across every run since 2026-04-21. Every dump logs `(UNENCRYPTED — set BACKUP_GPG_RECIPIENT)`. The 14 dumps (780 KB) are in `/home/deploy/backups` on `/dev/sda1`, the same filesystem as the `postgres_data` volume. The S3 attachment bucket has no backup or versioning step anywhere in the repo.
- Dopad: If the VPS is lost (disk failure, Hetzner incident, `rm -rf`, ransomware via the deploy key) or the disk fills (BE-02), every tenant's orders, customers, audit log and billing state are gone, along with every backup. Losing the bucket also loses every drawing irrecoverably. This is total data loss for paying customers, and the docs say it's covered.
- Návrh: (1) Operator: install rclone, create a B2/R2 remote with object lock / versioning, then set `RCLONE_REMOTE` and `BACKUP_GPG_RECIPIENT`. (2) Code: make `backup.sh` load the env file (`set -a; . /etc/assoluto/env; set +a`, or pass `--env-file`), and make it **fail loudly** (exit ≠ 0 plus an alert) when `RCLONE_REMOTE` is empty and `APP_ENV=production`. (3) Add an `rclone copy` of the attachment bucket. (4) Run a restore drill (dump → scratch container → `alembic current` + row counts) and record the date in `BACKUP_RESTORE.md`. No drill evidence exists in the repo.
- Effort: S (code) + M (operator setup + drill)
- Auto-fixable: no (needs operator: remote, credentials, GPG key)

### BE-02 — Production disk is 88 % full: 97 old images (26 GB reclaimable) and no prune step, so about 9 more deploys fill the disk
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: VPS `df -h /` → `38G size, 32G used, 4.6G avail, 88%`. `docker system df` → `Images 97 total, 3 active, 27.36GB, 26.25GB reclaimable (95%)`, `Build Cache 3.562GB`. `docker images` lists one ~457 MB `ghcr.io/elh0m3r0/assoluto:<sha>` per deploy going back 5 months. `deploy-production.yml` (HEAD, lines 108-127) does `pull` + `up -d` and never `docker image prune`. `/var/log/journal` is 2.9 GB. Container logs use the `json-file` driver with no `max-size` (`LogConfig map[]` on all three containers).
- Dopad: Each deploy adds about 460 MB. When the disk fills, Postgres can't write WAL and stops (production down for every tenant), `pg_dump` fails (and BE-01 means it's the only backup), and the next `docker pull` fails mid-deploy. Nothing alerts (BE-05).
- Návrh: Add `docker image prune -af --filter "until=168h"` (keep the current tag and one rollback tag) after a successful health check in the deploy script. Operator one-off: prune now. Add `logging: {driver: json-file, options: {max-size: "20m", max-file: "5"}}` to the prod overlay and `SystemMaxUse=500M` for journald. Add a disk-usage check (> 80 %) to the uptime tripwire.
- Effort: S
- Auto-fixable: yes (workflow and compose edit); the one-off prune needs the operator

### BE-03 — Attachment upload and thumbnailing still block the single event loop on boto3, and the thumbnail task holds a DB transaction across S3 I/O
- Severity: P1
- Status vs 2026-07-26: persisted (F-10)
- Evidence: `app/routers/attachments.py:128` calls `s3_storage.upload_bytes(...)` synchronously inside `async def upload_attachment`. `:244-246` does the same with `delete_object`. `app/tasks/thumbnail_tasks.py:87` opens `async with sm() as session, session.begin()` and inside it calls sync `s3_storage.download_bytes` (:~100), `_render_thumbnail` (Pillow), and `s3_storage.upload_bytes` (:~128), all on the loop with a pool connection checked out. The only `run_in_threadpool` in `app/` is `tenant_export_service.py:249`. Dockerfile CMD has no `--workers`, so there is one process.
- Dopad: A 30–50 MB upload freezes every tenant's requests, `/healthz` and Stripe webhooks for the length of the S3 PUT, then again for the GET/PUT in the thumbnail task. A few concurrent uploads can false-fail the deploy health gate. Marketing says "Direct upload to S3" (`www/features.html:87`), but every byte is proxied through this loop.
- Návrh: Wrap every boto3 call and `_render_thumbnail` in `await run_in_threadpool(...)`. In the thumbnail task, read the row, close the transaction, do the I/O, then open a short transaction to write `thumbnail_key`. Medium term: presigned PUT direct to S3.
- Effort: S (threadpool) / L (presigned)
- Auto-fixable: yes (threadpool part)

### BE-04 — PDF thumbnails have never worked in production: `pdf2image`/poppler aren't in the image (0 of 44 PDFs have a preview), but the website advertises them
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Prod `SELECT content_type, count(*), count(thumbnail_key) FROM order_attachments GROUP BY 1` → `application/pdf 44 / 0 thumbs`, `image/jpeg 1 / 1`. `docker compose exec web python -c "import pdf2image"` → `ModuleNotFoundError`. `Dockerfile:84` comment says "poppler in M3+", and neither poppler nor pdf2image is installed or in `pyproject.toml`. `thumbnail_tasks.py` catches the `ImportError` and returns `None`, so prod logs show `thumbnail.unsupported` ×8 and no error. Marketing copy: `www/features.html:87` "Thumbnails for PDFs and images", `www/index.html:145` "instant preview".
- Dopad: The most common attachment type for job shops (PDF drawings, 96 % of prod attachments) never gets the advertised preview. Customers see a generic file icon. This is a marketing-vs-reality gap a prospect notices on the first order.
- Návrh: Add `poppler-utils` to the runtime stage and `pdf2image` to dependencies, and backfill thumbnails for existing rows with a one-off script. Add a test that runs `_render_thumbnail` on a 1-page PDF so a missing dependency fails CI. Or drop "PDF" from the claim.
- Effort: S
- Auto-fixable: yes

### BE-05 — Still no error tracking or alerting: a real production 500 on 2026-09-08 went unnoticed, and `/readyz` checks only the DB
- Severity: P2
- Status vs 2026-07-26: persisted (F-36)
- Evidence: `grep -rni sentry app/ pyproject.toml` → 0 hits. Prod `docker logs assoluto-web-1` (6 weeks) contains exactly one `"event": "unhandled"`: `{"path": "/platform/signup", "error": "UndefinedError: 'identity' is undefined", ... "2026-09-08T23:25:06Z"}` (see BE-07). Nobody acted on it and the bug is still in `main`. `app/routers/health.py:33-48` `/readyz` pings the DB only. Its docstring says it uses the owner DSN, but it calls `get_engine()` (the `portal_app` DSN). There's no S3, SMTP or scheduler liveness check. Email failures only produce `log.error("email.failed")` (`email_tasks.py:159`) to stdout.
- Dopad: A 500 at 3 am, an SMTP credential rotation, or an S3 outage is invisible until a customer complains. Password-reset and invitation mails can silently stop.
- Návrh: Add `SENTRY_DSN` to `config.py` (already documented in `DEPLOY_SAAS.md:225`) with sentry-sdk's FastAPI integration, and call `capture_message` in `_safe_send`'s final-failure branch and in `send_order_notifications`' `render_failed` branch. Extend `/readyz` with S3 `head_bucket` (cached 60 s) and fix the docstring.
- Effort: S
- Auto-fixable: no (needs a Sentry or GlitchTip account and DSN); the readyz part is auto-fixable

### BE-06 — The "every 15 minutes" uptime tripwire actually runs every 3–7 hours
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `.github/workflows/uptime.yml:16` `cron: "*/15 * * * *"`, but `gh run list --workflow uptime.yml --limit 40` shows runs at e.g. `2026-10-03 04:41, 10:04, 14:44, 18:19, 21:40` and `2026-10-02 02:29, 08:56, 15:41, 20:09, 23:48`, which is 39 gaps over ~185 h, a mean of about **4.7 h**. GitHub throttles scheduled workflows on low-activity repos. This is the project's only availability alerting.
- Dopad: Mean time to detect a full outage is hours, not 15 minutes. A night-time outage can last until morning with no signal, which contradicts any uptime claim made to customers.
- Návrh: Move the probe to an external monitor (UptimeRobot, Better Stack or Healthchecks.io free tiers) on 1–5 minute intervals for `/readyz` and one tenant login page, with e-mail/SMS alerting. Keep the GH workflow as a backup.
- Effort: S
- Auto-fixable: no (external account)

### BE-07 — The signup honeypot branch returns a 500 instead of the fake "check your e-mail" page
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `app/platform/routers/signup.py:105-115` renders `platform/verify_sent.html` with context `{"email": ..., "principal": None}`, but the template uses `{{ identity.email }}` (`templates/platform/verify_sent.html:13`) → `UndefinedError` → 500. Confirmed in prod logs at 2026-09-08T23:25:06Z, right after `signup.honeypot_tripped`. No test covers this branch (`grep -rn honeypot tests/` hits only the `/contact` form).
- Dopad: The honeypot now tells bots they were detected (500 vs 200). A human whose browser autofills the hidden field gets an error page instead of guidance. It also adds noise to the error stream once BE-05 exists.
- Návrh: Pass `identity=SimpleNamespace(email=owner_email)`, or switch the template to `email`. Add a test that posts `website=x` and expects 200.
- Effort: S
- Auto-fixable: yes

### BE-08 — Endpoints that schedule e-mail without an explicit commit hold their DB connection and open transaction through the whole SMTP send, up to 36 s per mail
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Measured on the pinned FastAPI 0.135.3 / Starlette 1.0.0 with a minimal app: event order is `['dep-enter', 'bg-run', 'dep-exit']`, so a `yield` dependency's cleanup (here `get_db`'s `async with sm() as session, session.begin()` at `deps.py:161`) runs **after** background tasks. Write paths call `await db.commit()` first, which releases the connection. These read-only paths don't, so their `portal_app` connection stays "idle in transaction" while `_safe_send` runs (`smtplib` 10 s timeout × 3 attempts + 2 s + 4 s backoff, `email_tasks.py:130-158`): `public.py:788` (tenant password reset, unauthenticated), `platform/routers/platform_auth.py:180` (platform reset, unauthenticated), `signup.py:472` (`/platform/check-email`, unauthenticated) and `signup.py:520`. Pool is SQLAlchemy's default 5 + 10 overflow (`db/session.py:26`, no pool args).
- Dopad: During an SMTP slowdown or outage, 15 concurrent reset/resend requests (rate limits are per-IP or per-email, so many addresses or IPs get through) exhaust the app pool. Every other tenant request then waits 30 s for `pool_timeout` and 500s, so one third-party outage becomes a full-portal outage.
- Návrh: Call `await db.commit()` (or `await db.close()`) before `add_task` on these four routes too, and widen the §2 rule in CLAUDE.md to "always release the session before scheduling a background task". Or give `get_db` FastAPI's `Depends(..., scope="function")` so it exits before the response is sent, and keep the explicit commits. Add a regression test that asserts no checked-out connections when the background task runs.
- Effort: S
- Auto-fixable: yes

### BE-09 — E-mail has no durable outbox: three attempts within ~6 s, then the mail is gone, and in-flight mails are lost on every deploy
- Severity: P2
- Status vs 2026-07-26: new (failure-mode extension of F-36)
- Evidence: `email_tasks.py:41-42` sets `_MAX_ATTEMPTS = 3` and `_BACKOFF_BASE_SECONDS = 2.0`. After the last attempt the only trace is `log.error("email.failed")`. Payloads exist only in the `BackgroundTasks` list of a live request. `docker compose up -d` (deploy) sends SIGTERM with the default 10 s grace, so any queued `send_order_notifications` batch (one SMTP connection per recipient, serial, `email_tasks.py:302-322`) is dropped. Prod logs show 122 `email.sent` and 0 failures so far, which only means no outage has happened yet.
- Dopad: A Brevo blip longer than ~14 s, or a deploy during a bulk transition, silently loses order-submitted / quote / invitation / password-reset mails. The notification redesign (CLAUDE.md §19) guarantees an audience, but delivery isn't guaranteed.
- Návrh: Write an `email_outbox` row (tenant_id, template, recipient, context JSON, attempts, next_attempt_at, last_error) in the same transaction as the business change. Have a scheduler job with an advisory lock drain it with exponential backoff up to ~24 h, and expose failed rows to the operator.
- Effort: M
- Auto-fixable: no (design decision; schema change)

### BE-10 — Migrations run unattended in the container entrypoint with no pre-migration backup, and the deploy gate probes a static `/healthz`
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `docker/entrypoint.sh` runs `alembic upgrade head` then `exec uvicorn`. `deploy-production.yml` (HEAD :108-127) runs `pull` → `up -d --no-deps web` → polls `http://127.0.0.1:8000/healthz`, which returns a hard-coded `{"status":"ok"}` and ignores DB state. There's no `pg_dump` before the upgrade and no automated rollback. With `restart: always`, a failing migration crash-loops the only web container. The only backup is the 03:00 local dump (BE-01).
- Dopad: A bad or irreversible migration (drop/rename) loses up to 24 h of writes with no clean rollback point. A half-applied non-transactional migration leaves prod down until someone SSHes in. Each deploy also has a short 502 window because the single container is recreated in place.
- Návrh: Add a `pg_dump` step to the deploy script before `up` (tagged with the image SHA, kept with the other dumps). Gate on `/readyz` instead of `/healthz`. On health failure, `sed` `APP_IMAGE_TAG` back to the previous SHA and `up -d` again. Document "migrations must be backward compatible with the previous image" (expand/contract).
- Effort: M
- Auto-fixable: yes (workflow changes); rollback policy needs a founder decision

### BE-11 — mypy regressed from 0 to 1 error, and CI doesn't run mypy, so nothing caught it
- Severity: P2 (the agent definition calls a mypy regression P1; on the BRIEF scale it blocks nothing for customers)
- Status vs 2026-07-26: new
- Evidence: `.venv/bin/mypy app/` → `app/routers/orders.py:692: error: Incompatible types in assignment (expression has type "Row[tuple[str]] | None", variable has type "Row[tuple[UUID, str]]")`, introduced by `9c6df4f` (reusing the name `row` for the deactivated-assignee lookup). `.github/workflows/ci.yml:69-78` runs `ruff check`, `ruff format --check` and `pytest`, with no mypy step. Every earlier audit recorded 0 errors.
- Dopad: Harmless at runtime, but the type gate everyone believed in is gone. The next type error may be a real one.
- Návrh: Rename the variable (`assignee_row`) and add `uv run mypy app/` to `ci.yml` before pytest.
- Effort: S
- Auto-fixable: yes

### BE-12 — Test coverage is thinnest on money and cross-tenant auth paths: the subscription editor, cancel, refunds, and the subdomain switch/complete-switch handoff have no tests at all
- Severity: P2
- Status vs 2026-07-26: persisted (F-38: manual subscription editor untested) + new gaps
- Evidence: `pytest --cov=app` (the modules below sit at 32–76 %): `platform/routers/platform_admin.py` 46 %, with **`subscription_edit` :471-624 entirely uncovered**, plus `tenants_edit` :378-401 and `tenants_create` :142-169. `platform/routers/platform_auth.py` 32 %: `platform_password_reset_submit` :156-199, `…_confirm_submit` :243-311 (the F-05/F-20 fix is only exercised at service level), **`switch_to_tenant` :409-491 and `complete_switch` :511-628** (cross-subdomain session minting). `platform/routers/billing.py` 74 %: `invoice_pdf` :121-152, `cancel_subscription_route` :341-386. `platform/billing/webhooks.py` 76 %: `handle_charge_refunded` :423-453. `tasks/periodic.py` 79 %: `cleanup_old_stripe_events` :176-205 and `expire_demo_trials` :222-263. By contrast `notification_service.py` is at 96 % and `notification_prefs.py` at 96 %.
- Dopad: The code that moves money, grants plan time, and mints a session on another subdomain is the code a refactor can break without a red test, which matters most right before billing goes live (F-07 still open).
- Návrh: Add route-level tests: subscription editor (Stripe-managed refusal, `extend_trial:x` → 400 not 500), cancel route, refund webhook idempotency, switch → complete-switch happy path plus a tampered or expired token, and both periodic jobs with `now=` injection. Gate CI with `--cov-fail-under` per package (e.g. `app/platform` ≥ 80 %).
- Effort: M
- Auto-fixable: yes

### BE-13 — Scheduled jobs still write no audit events; in production they have already disabled 5 tenants without a trace
- Severity: P2
- Status vs 2026-07-26: persisted (F-34)
- Evidence: `app/tasks/periodic.py` doesn't import `audit_service`, and `auto_close_delivered_orders` (:54-127), `cleanup_stale_invited_contacts` (:130-169) and `enforce_canceled_subscriptions` (:266-354) are unchanged. Prod logs: `periodic.enforce_canceled.tenant_disabled` ×5 and `periodic.auto_close.done` ×1085 (hourly). `/app/admin/audit` reads `audit_events` only.
- Dopad: A tenant whose account was cut off, or whose invited contact disappeared, finds nothing in their audit log, and the operator can only reconstruct events from stdout logs that reset on every container recreate.
- Návrh: Call `audit_service.record(actor=system, action="order.auto_closed" / "contact.invite_expired" / "tenant.disabled_for_nonpayment", tenant_id=…)` inside each job's transaction.
- Effort: S
- Auto-fixable: yes

### BE-14 — The SLA dashboard is still dead: `promised_delivery_at` has no writer, and 0 of 29 production orders have one
- Severity: P2
- Status vs 2026-07-26: persisted (F-30; deliberately deferred by the founder)
- Evidence: `grep -rn promised_delivery_at app/` shows readers only (`sla_service.py:55-173`, `pdf_service.py:253`, `orders.py:402`, `detail.html:155`, export) and no form field or service write. Prod: `count(*) FILTER (WHERE promised_delivery_at IS NOT NULL)` → **0** of 29 (21 have `delivered_at`).
- Dopad: `/app/admin/sla` shows 0 orders / 0 % for every tenant while `/features` sells deadline tracking. When this is fixed, F-31/F-32 (cancelled orders counted, jump-fabricated delivery dates) come back into effect.
- Návrh: Add a staff-editable "promised delivery" date on order detail, stamped by default at CONFIRMED, and apply the F-32 status filter in the same change, or remove the SLA claim.
- Effort: M
- Auto-fixable: no (founder chose to defer)

### BE-15 — The order-item product picker loads only the first 200 products alphabetically; product #201 onward can't be picked
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/routers/orders.py:711-713` → `search_products(db, query="", customer_id=order.customer_id, limit=200)`, and `product_service.py:53` does `order_by(Product.name).limit(limit)`. `orders/detail.html:204-214` renders them as a plain `<select>` with no search, no "showing 200 of N" and no truncation warning. A search endpoint already exists (`products.py:161 /products/search`) but the picker doesn't use it.
- Dopad: A job shop with a real catalogue (hundreds of SKUs is normal) silently loses access to everything after "M…". Staff and customers fall back to free-text lines, losing list prices and SKU linkage, so quotes go out wrong and nobody sees an error.
- Návrh: Switch the picker to an htmx type-ahead against `/products/search` (already customer-scoped), or at least show a "200 of N, refine…" notice.
- Effort: M
- Auto-fixable: yes

### BE-16 — Tenant export builds the whole ZIP, every attachment included, in RAM on a 3.8 GB box with no swap
- Severity: P2 (latent; tenants hold 4 MB today, but the Pro plan allows 20 GB)
- Status vs 2026-07-26: new
- Evidence: `app/services/tenant_export_service.py:215` uses `buf = io.BytesIO()`, `:219` `zipfile.ZipFile(buf, "w", ZIP_DEFLATED)`, and `:249` adds each attachment's full bytes via `download_bytes` before `tenant_admin.py:709-723` returns `Response(blob)`. Deflate runs on the event loop. Prod `platform_plans`: starter `max_storage_mb=2048`, pro `20480`. VPS: 3.8 GB RAM, `Swap: 0`.
- Dopad: When a Pro tenant with a few GB of drawings clicks "Export", the kernel OOM-kills the only uvicorn worker, so every tenant is down while the restart and migration run. Before that, compression blocks the loop for minutes. Any tenant admin can trigger it.
- Návrh: Stream the export (`zipstream-ng`, or write to a temp file and return a `FileResponse`), run the zip in a thread, or build it as a background job that uploads to S3 and e-mails a presigned link. Cap synchronous exports by size.
- Effort: M
- Auto-fixable: yes

### BE-17 — Uploads are still read fully into memory before the size limit is checked
- Severity: P3
- Status vs 2026-07-26: persisted (F-41)
- Evidence: `app/routers/attachments.py:97` runs `data = await file.read()`, then `create_attachment_row(... size_bytes=len(data), max_size_bytes=...)` (:110-117) raises `AttachmentTooLarge`. Caddy allows 60 MB (`docker/Caddyfile` `request_body max_size 60MB`).
- Dopad: A rejected 60 MB upload still costs 60 MB of RSS per concurrent request on a box without swap (compounds BE-16).
- Návrh: Check `file.size` (Starlette ≥ 0.35 sets it from the spooled temp file) or read in chunks with a running total before buffering. Lower Caddy's limit to `MAX_UPLOAD_SIZE_MB + 1`.
- Effort: S
- Auto-fixable: yes

### BE-18 — `LOG_LEVEL` is still shadowed by the base compose file in production
- Severity: P3
- Status vs 2026-07-26: persisted (F-37)
- Evidence: `docker-compose.yml:91` `LOG_LEVEL: INFO` in `environment:`. `docker-compose.prod.yml` doesn't re-declare it, while its own comment (:104-112) still says `LOG_LEVEL` flows through `env_file` automatically. This is the CLAUDE.md §15 trap.
- Dopad: The operator can't raise log verbosity during an incident without a repo change and redeploy, and the comment says otherwise.
- Návrh: Add `LOG_LEVEL: ${LOG_LEVEL:-INFO}` to the prod overlay's `environment:` and fix the comment.
- Effort: S
- Auto-fixable: yes

### BE-19 — No retention for audit events or S3 objects, and attachment create/delete leave S3 and the DB inconsistent on failure
- Severity: P3
- Status vs 2026-07-26: persisted (F-26, narrow version)
- Evidence: There's no purge job for `audit_events` (prod: 253 rows since 2026-04-21) or closed orders in `periodic.py`. The only `delete_object` callers in `app/` are `attachments.py:244-246` (single-attachment delete). Tenant deactivation, GDPR erasure and customer removal never touch S3. Ordering bugs: upload PUTs to S3 (`:128`) **before** the DB commit (`:159`), so a failed commit orphans the object. Delete removes the S3 object (`:244`) **before** the row (`:250`), so a failed row delete leaves a row pointing at nothing.
- Dopad: Storage cost and personal data (drawings with names) accumulate forever after a tenant leaves, which the privacy page doesn't disclose. Orphans aren't counted by `max_storage_mb`.
- Návrh: Commit first, then PUT (and delete the row on PUT failure). Delete the row, commit, then best-effort S3 delete. Add a weekly reconciliation job (S3 `list_objects` under each tenant prefix vs `order_attachments`) and a documented retention for `audit_events` and deactivated tenants.
- Effort: M
- Auto-fixable: no (retention periods need a founder/legal decision); the ordering fix is auto-fixable

### BE-20 — Versioned static assets are served with no `Cache-Control` by the single Python worker
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `curl -sI https://assoluto.eu/static/css/app.css` returns `etag` and `last-modified` but **no `cache-control`**. `app/security/headers.py:79-84` claims "Static assets keep their own long-lived caching", but nothing sets it. Templates already cache-bust with `?v={{ app_version }}` (`base.html:8-24`). StaticFiles is mounted at `main.py:563`, and Caddy only adds `encode gzip`.
- Dopad: Each page view revalidates 5 assets against uvicorn (304s still cost a request on the single worker) and mobile users on shop-floor networks pay the round-trips.
- Návrh: In Caddy, `@static path /static/*` + `header @static Cache-Control "public, max-age=31536000, immutable"`, or serve `/static` directly from Caddy. Correct the comment.
- Effort: S
- Auto-fixable: yes

### BE-21 — Bulk status change accepts an unbounded list of order IDs and runs 4–6 recipient queries per order
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `app/routers/orders.py:1190` `order_ids: list[str] = Form(default=[])` with no cap. `:1255-1284` loops over each succeeded order calling `build_order_submitted` / `build_order_status_changed`, each of which re-queries all active staff (`notification_service.py:238`), the customer, contacts and, when any contact narrowed scope, three involvement queries (`:311-351`). The staff list is identical for every order in the batch.
- Dopad: Fine at today's volume (29 orders). A staff "select all" on a large tenant runs hundreds of queries inside one transaction that holds `FOR UPDATE` locks on every order (F-33 fix) until commit.
- Návrh: Cap at the page size (or 200), resolve the staff audience once per batch, and pre-load contacts/customers per `customer_id`.
- Effort: S
- Auto-fixable: yes

### BE-22 — i18n: catalogs are healthy, but cs carries 224 dead entries and platform flows still hard-code Czech
- Severity: P3
- Status vs 2026-07-26: persisted (F-39) + new (cruft)
- Evidence: The documented `pybabel extract -k _t:2 …` into a temp dir matches the repo `.pot` (1171 msgids, cs/de 0 untranslated, 0 fuzzy, and `Invalid email or password.` is live, not `#~`). But `app/locale/cs/LC_MESSAGES/messages.po` has **224** `#~ msgid` obsolete blocks (de: 18). 37 Czech string literals sit outside `_t()` in `app/platform/routers/*.py`, e.g. `platform_admin.py:150,172,329,343,401` and `platform_auth.py` "Pokud adresa existuje, odeslali jsme odkaz…".
- Dopad: German- or English-speaking signups and the operator UI see Czech errors and flashes. The dead entries make translators' diffs noisy.
- Návrh: Wrap the platform strings in `_t(request, …)` and run the documented extract/update/compile. Drop obsolete entries with `msgattrib --no-obsolete` once.
- Effort: M
- Auto-fixable: yes

### BE-23 — Code health: dead functions, and `alembic check` can't serve as a drift guard
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `vulture app/ --min-confidence 60` reports never-called functions: `deps.require_customer_contact`, `auth_service.create_tenant_user`, `customer_service.delete_customer`, `platform/service.link_contact_to_identity`, `gdpr_service.export_for_identity` / `erase_identity` / `find_target_rows_for_email` (F-12), `db/session.session_scope`, `invoice_pdf_service._example_invoice_for_preview`. `alembic check` flags all four billing tables as "removed" (billing models aren't imported by `migrations/env.py` via `app.models`) plus index-name drift (`ix_platform_tm_*` vs ORM `ix_platform_tenant_memberships_*`, ORM `index=True` on `audit_events.action` that no migration creates, and `tenants.slug` carrying both `uq_tenants_slug` and a redundant non-unique `ix_tenants_slug`). `_templates(request)` and `_actor(principal)` helpers are copy-pasted across ~8 routers.
- Dopad: The next real ORM/migration mismatch will hide in this noise. Dead helpers mislead readers, e.g. `delete_customer` suggests a customer-deletion feature that doesn't exist.
- Návrh: Import `app.platform.billing.models` in `env.py`, align index names (a no-op migration renaming them, or `index=False` in the ORM), drop the duplicate slug index, delete or wire up the dead functions, then add `alembic check` to CI.
- Effort: M
- Auto-fixable: yes

---

## Re-verification summary (backend/ops items open since 2026-07-26)

| prior | now | here |
|---|---|---|
| F-01 (second half: bucket not backed up) | **persisted, worse than reported**: DB off-site sync never configured either | BE-01 |
| F-10 blocking boto3 | persisted | BE-03 |
| F-26 (narrow) retention / S3 orphans | persisted | BE-19 |
| F-30 SLA has no writer | persisted (0/29 in prod) | BE-14 |
| F-34 jobs write no audit | persisted (5 tenants disabled silently) | BE-13 |
| F-36 no error tracking | persisted (an unnoticed prod 500 exists) | BE-05, BE-06, BE-09 |
| F-37 LOG_LEVEL shadowed | persisted | BE-18 |
| F-38 subscription editor untested | persisted (0 % route coverage) | BE-12 |
| F-39 hard-coded Czech in platform | persisted | BE-22 |
| F-41 upload read before size check | persisted | BE-17 |

## Notification redesign (PR #7) — specific checks

* §2 commit-before-`add_task`: all new call sites (`attachments.py:159`, `orders.py:533/1295/1382/1457/1533`) build payloads under RLS and commit before scheduling. **Pass.**
* N+1: recipient resolution is O(1) queries per order (staff list, contacts, customer, ≤ 3 involvement queries only when someone narrowed scope). There's no per-recipient query. The only multiplication is per order in bulk transitions (BE-21).
* Failure handling: each payload is isolated (`send_order_notifications` catches render errors per recipient), but delivery isn't durable (BE-09) and failures aren't alerted (BE-05).
* Coverage: `notification_service.py` 96 %, `notification_prefs.py` 96 %, 2 new test files. **Good.**

## Performance and capacity notes (no separate finding)

* Single uvicorn worker on 2 vCPU (web RSS 163 MB, CPU ~0.3 %). Capacity is ample for 22 tenants. The real availability limits are loop-blocking I/O (BE-03, BE-16), not CPU. Before adding `--workers`, note that `slowapi` (`rate_limit.py:87`, in-memory) and `EmailThrottle` (`email_throttle.py:55`) are per-process, so limits would loosen by ×N. The scheduler is already safe for multiple workers via advisory locks 42_001–42_007.
* Indexes cover the hot filters: `orders(tenant_id,status)`, `(tenant_id,assigned_to_user_id)`, `(tenant_id,customer_id,created_at)`, plus FK indexes on all child tables. Order/audit search uses leading-wildcard `ILIKE` (`order_service.py:208`, `audit_service.py:280`), which is fine at this size. Add `pg_trgm` GIN when a tenant passes ~50k orders. Customers, products and assets lists are unpaginated (`customer_service.py:18`, `product_service.py:26`, `asset_service.py:36`).
* Test suite: 78.5 s, no test > 5 s. About 7.6 s is real `time.sleep` in `test_email_throttle.py:44,81` and `test_security.py:85`, which could use an injected clock.
* `.coverage` (git-ignored) was regenerated at the repo root by this audit's coverage run. No tracked file was touched.

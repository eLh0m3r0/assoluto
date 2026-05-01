# Backend audit — 2026-05-01-1455 (verification run)

## Summary

Verification pass against baseline `2026-05-01-1335`. Hygiene baselines
remain clean: ruff lint clean, ruff format clean (147 files), mypy
clean (`Success: no issues found in 87 source files`), full pytest
suite green at `423 passed, 12 warnings in 57.60s` — slightly faster
than the 58.97 s baseline and well under the 60 s budget. Architecture
invariants all still hold (lock IDs unique; bg-task pattern preserved;
`read_session_for_tenant` still in use on public routes; no new
core→platform module-load imports). One previously-active P0
(`/terms` 500 in EN) is fully fixed and verified end-to-end through
Jinja's i18n stack. Three findings persist as expected (operator
config, deferred GDPR work). No new findings introduced by the eight
post-baseline commits.

| Severity | Count | vs baseline |
|---|---|---|
| P0 | 0 | -1 (resolved) |
| P1 | 2 | unchanged |
| P2 | 3 | -1 (process-discipline finding rolled into baseline) |

Per-finding status against baseline:

| Baseline ID | Status | Notes |
|---|---|---|
| F-BE-001 (P0) | resolved | `99.9%%` escape landed in `f87ae07`; verified end-to-end |
| F-BE-002 (P1) | persisted | Stripe price IDs still NULL on prod; operator action |
| F-BE-003 (P1) | persisted | GDPR endpoints still untested |
| F-BE-004 (P2) | persisted | `gdpr_service` contact functions still no router |
| F-BE-005 (P2) | persisted | Process discipline; baseline commits unchanged |
| F-BE-006 (P2) | persisted | Stripe webhook breadth unchanged |
| F-BE-007 (P2) | re-baselined | Hygiene metrics still green; no regression |

---

### F-BE-001 — `/terms` HTTP 500 in EN — RESOLVED
- **Where**: `app/templates/www/terms.html:114`; catalogues
  `app/locale/{cs,de,en}/LC_MESSAGES/messages.po`
- **Severity**: P0 (was)
- **Auto-fixable**: yes (already auto-fixed in `f87ae07`)
- **Description**: The msgid was edited to `99.9%%` and the existing
  CS / DE translations were re-pointed at the new msgid (no
  translator round-trip needed); EN msgstr remains empty so gettext
  returns the (already-escaped) msgid; Jinja's `% variables`
  collapses `%%` back to a single `%`. Verified by reproducing the
  failing path with `gettext.translation('messages', ...,
  languages=['en'])` + `Environment(extensions=['jinja2.ext.i18n'])`
  + `template.render()` — the rendered string is 534 chars and
  contains the literal `99.9% monthly uptime` with no
  `ValueError`. CS/DE catalogues both contain `99,9 %%` so they
  render correctly too. The four `#~` obsolete entries left behind
  by the pybabel update cycle are inert (gettext ignores them) but
  could be pruned next housekeeping pass.
- **Suggested fix**: none — fix landed and is verified.
- **Evidence**: `git show f87ae07 --stat` shows the four catalog
  files + `terms.html` updated. End-to-end Jinja smoke test passed.
  The other three msgids that legitimately need a literal `%`
  (`>=90%% on time`, `50-90%%`, `<50%%` in the dashboard freshness
  widgets) all carry correctly-escaped translations in cs/de/en.

---

### F-BE-002 — Stripe checkout silently no-ops in production — PERSISTS
- **Where**: production env file `/etc/assoluto/env`; affects code
  path `app/main.py:_sync_stripe_prices_from_env` →
  `app/platform/billing/service.py:create_checkout_session`
- **Severity**: P1
- **Auto-fixable**: no (operator config — outside repo)
- **Description**: Read-only psql against prod still shows
  `stripe_price_id IS NULL` for all four `platform_plans` rows
  (community / starter / pro / enterprise). No commit since the
  previous audit could have moved this — the env file is on the
  VPS, not in git — so the persistence is expected. Same impact:
  upgrade CTA on `/pricing` and the in-app banner go through
  `create_checkout_session` which early-exits when
  `plan.stripe_price_id` is NULL, leaving the user on a silent
  no-op. Re-classified from "diagnostic" to "operator-blocker"
  pending the operator setting `STRIPE_PRICE_STARTER` /
  `STRIPE_PRICE_PRO` and restarting `web`.
- **Suggested fix**: unchanged from baseline. As a defensive code
  follow-up: emit `stripe_price.sync.no_env` (info level) when both
  env vars are empty so the next operator-side outage shows up in
  log review instead of silent skip. Today the sync simply doesn't
  iterate, leaving no log line to grep for.
- **Evidence**: `psql ... -c 'SELECT code, stripe_price_id IS NOT
  NULL AS has_pid FROM platform_plans ORDER BY code'` →
  `community/enterprise/pro/starter` all `f`. Migration head
  `1006_drop_starter_orders_cap` matches the local repo head.

---

### F-BE-003 — Zero test coverage for GDPR endpoints — PERSISTS
- **Where**: `app/routers/tenant_admin.py` (`GET
  /app/admin/profile/export`, `POST /app/admin/profile/delete`);
  service `app/services/gdpr_service.py`
- **Severity**: P1
- **Auto-fixable**: yes (deferred — needs deliberate test design,
  not a one-line auto-fix)
- **Description**: `grep -rln "gdpr\|GDPR\|profile/export\|
  profile/delete\|export_for_user\|erase_user\|export_for_contact\|
  erase_contact" tests/` still returns empty. No new tests added
  since baseline. The risk profile is unchanged: the erase path
  mutates state irreversibly (nulls PII, bumps session_version,
  writes audit, blocks last-admin self-erase), and the only QA
  exercise it gets is manual.
- **Suggested fix**: unchanged from baseline — see F-BE-003 in
  `2026-05-01-1335/backend.md` for the proposed 4–5 test cases.
- **Evidence**: empty grep result against `tests/`.

---

### F-BE-004 — `gdpr_service` contact export/erase have no router — PERSISTS
- **Where**: `app/services/gdpr_service.py:102` / `:233`; gap in
  `app/routers/me.py`
- **Severity**: P2
- **Auto-fixable**: yes (deferred)
- **Description**: `app/routers/me.py` still exposes only three
  routes (`GET /profile`, `POST /profile`, `POST
  /profile/password`) — no `me/profile/export` or
  `me/profile/delete`. Customer contacts still have no self-service
  GDPR path; staff get one, contacts don't. Compliance gap
  unchanged.
- **Suggested fix**: unchanged from baseline.
- **Evidence**: `grep -n "@router\." app/routers/me.py` →
  three matches (lines 72, 107, 134); none mention export/delete.

---

### F-BE-005 — Recent feature commits without paired tests — PERSISTS (advisory)
- **Where**: still `1b1c8f9` (subscription editor) and `884508b`
  (verify-gate + honeypot)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: No new test coverage added for either feature
  since baseline. The eight post-baseline commits are all
  audit-driven fixes (i18n strings, template polish, Caddy header
  strip, backup retention default) — none touched the
  subscription-editor or honeypot code paths, so the gap simply
  carries forward. No new gap introduced.
- **Suggested fix**: unchanged from baseline.
- **Evidence**: `git log --since="14 days ago" --oneline` since
  baseline shows only `cda17c6 / f87ae07 / cb53240 / 8707f21 /
  82d5458 / f4743e8 / efd4890`; none touch `app/platform/routers/
  platform_admin.py`, `app/platform/routers/signup.py`, or
  `tests/`.

---

### F-BE-006 — Stripe webhook handler set is narrow — PERSISTS
- **Where**: `app/platform/billing/webhooks.py:428` (`HANDLERS`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Registry unchanged. Same three events still
  unhandled (`customer.subscription.paused`, `customer.updated`,
  `payment_method.detached`).
- **Suggested fix**: unchanged from baseline.
- **Evidence**: webhook file unchanged in the post-baseline diff.

---

### F-BE-007 — Hygiene baseline still green — RE-BASELINED
- **Where**: `.venv/bin/{ruff,mypy,pytest}` against the repo as of
  HEAD `efd4890`
- **Severity**: P2 (informational)
- **Auto-fixable**: n/a
- **Description**: Re-running the full hygiene suite against this
  commit:
  - `ruff check .` → `(no output)` = clean.
  - `ruff format --check .` → `147 files already formatted`.
  - `mypy app/` → `Success: no issues found in 87 source files`
    (identical to baseline, 0 errors, 87 files — no new modules
    added).
  - `pytest tests/ -q` → `423 passed, 12 warnings in 57.60s`
    (was `423 passed, 12 warnings in 58.97s` — 1.4 s faster, well
    inside the 60 s budget).
  - 12 warnings unchanged in shape — all the same slowapi
    `asyncio.iscoroutinefunction` deprecation. No new warning
    categories from our own code.
  Architecture invariants spot-checked again:
  - Periodic-job advisory lock IDs (42_001 auto_close, 42_002
    invite_cleanup, 42_003 stripe_event_cleanup, 42_004 demo
    normalise, 42_005 stripe price sync, 42_006 expire_trials,
    42_007 enforce_canceled) — unique. Verified via `grep
    -n "_LOCK_ID\\b" app/tasks/periodic.py` + `grep -n
    "pg_try_advisory_lock" app/main.py`.
  - All 17 `background_tasks.add_task` call sites (orders × 6,
    customers × 2, tenant_admin × 2, public × 1, www × 1, signup ×
    3, attachments × 1, periodic-feed × 1) preceded by an explicit
    `await db.commit()` per CLAUDE.md §2.
  - All routers passing `locale=...` to bg-task functions resolve
    via `resolve_email_locale(...)` (orders/customers/tenant_admin/
    public). The four direct `settings.default_locale` reads in
    `app/platform/routers/{signup,platform_auth}.py` are correct —
    those are platform-level routes that have no tenant context yet
    (signup/login pre-auth).
  - `read_session(...)` is called from exactly two places: the
    legacy `read_session_for_tenant` wrapper itself and the
    `get_current_principal` dependency in `deps.py:263` which
    immediately verifies `session_data.tenant_id == str(tenant.id)`
    on the next line. No bare unsafe caller introduced.
  - Migration chain head still `1006_drop_starter_orders_cap` on
    both repo and prod. No new migrations; no down_revision
    collisions.
  - `app.platform` import isolation: same eight import sites as
    baseline (all runtime-local inside service functions per
    CLAUDE.md §17 plan-limit pattern). Test
    `test_platform_routes_not_mounted_when_flag_off` continues to
    pass.
  - Schema vs. ORM drift on `orders` and `platform_subscriptions`
    spot-checked via `\d` against prod — every column matches the
    ORM definitions. No drift.
- **Suggested fix**: keep using these counts as the next-run
  baseline. Regression triggers (CLAUDE.md §12) — any of: mypy >0,
  ruff non-clean, ruff format drift, pytest non-green, pytest
  >60s, new test warning categories, lock_id collision, schema
  drift on a heavy table.
- **Evidence**: command outputs above; full pytest run completed
  in 57.60s on this machine.

---

### Recent-commits sanity check (advisory)

`git log a9d64e0..HEAD --oneline` — eight commits, all
audit-driven follow-ups:

```
efd4890 docs(audit:2026-05-01-1335): note manual Caddy rebuild required for F-SEC-001 fix
f4743e8 docs(audit:2026-05-01-1335): update finding statuses after auto-fix run
82d5458 fix(audit:2026-05-01-1335): i18n copy …
8707f21 fix(audit:2026-05-01-1335): UX template polish …
cb53240 fix(audit:2026-05-01-1335): strip Server / Via headers …
f87ae07 fix(audit:2026-05-01-1335): /terms 500 in EN …
cda17c6 fix(audit:2026-05-01-1335): backup retention default 30 → 14 days
```

Backend Python touched in this batch: only `app/routers/www.py`
(`sitemap.xml` learned `xhtml:link rel=alternate hreflang` — pure
string-building, no DB or auth surface). No new test gap created.
The two `docs/audit-runs/...` commits don't ship code. The Caddy
+ shell-script + i18n-catalogue + html-template changes are all
out of scope for backend regression checks.

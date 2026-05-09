# Backend audit — 2026-05-09-0829

## Summary

Third audit pass against tip-of-tree `d0a5e35`. Hygiene baselines hold:
ruff lint clean, ruff format clean (147 files), mypy clean (`Success:
no issues found in 87 source files` — same count as baseline), full
pytest suite green at `427 passed, 12 warnings in 55.48s` (was 423
last audit; +4 tests, ~2s faster). Architecture invariants all hold
(advisory-lock IDs unique 42_001..42_007, no bg-task without prior
commit on write paths, `read_session` only via the safe wrapper or
the verified call site in `deps.py:263`, single Alembic head
`1006_drop_starter_orders_cap` matches prod, `app.platform` import
isolation maintained — three new sites are all runtime-local per the
CLAUDE.md §17 plan-limit pattern).

The 7 commits since the last audit ship four user-visible features
(staff item edit in SUBMITTED/QUOTED, IČO billing-details gate,
verified-identity gate on `/select-tenant` + `/switch`, comment-author
batch lookup) plus diagnostics (per-attempt email logs, S3 boot
warning) and i18n catalogues. No regression introduced. Coverage of
the new code paths is mixed — the staff edit-state expansion has
direct tests (`test_staff_can_autosave_on_submitted_and_quoted` +
`test_autosave_blocked_on_confirmed_returns_409`) but the
billing-details endpoint and the new `select-tenant`/`switch`
verify-gate path have no tests.

| Severity | Count | vs baseline (2026-05-01-1455) |
|---|---|---|
| P0 | 0 | unchanged |
| P1 | 3 | +1 (new billing-details test gap) |
| P2 | 5 | +2 (audit gap + sanitiser gap; baseline had 3) |

Per-finding status against baseline:

| Baseline ID | Status | Notes |
|---|---|---|
| F-BE-002 (P1, Stripe price IDs NULL) | persisted | unchanged on prod |
| F-BE-003 (P1, GDPR endpoint tests) | persisted | unchanged |
| F-BE-004 (P2, customer-contact GDPR routes) | persisted | unchanged |
| F-BE-005 (P2, feature commits w/o tests) | persisted | re-applies to new commits — see F-BE-009/010 |
| F-BE-006 (P2, narrow Stripe webhook set) | persisted | unchanged |
| F-BE-007 (P2, hygiene baseline) | re-baselined | numbers all green |

---

### F-BE-001 — Billing-details mutation has no audit-trail entry
- **Where**: `app/platform/routers/billing.py:422` (`billing_details_save`)
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: The new `POST /platform/billing/details` writes
  IČO / DIČ / fakturační název / fakturační adresa into
  `tenant.settings` JSONB and `db.commit()`s — but does not call
  `audit_service.record(...)`. Every other privileged tenant-settings
  mutation (e.g. `app/routers/tenant_admin.py:759` for default-locale
  changes, `app/routers/tenant_admin.py:381` for user role changes)
  goes through `audit_service` so the operator can answer "who
  changed our IČO from 12345678 to 87654321 last Tuesday?". This is
  the privacy / regulatory equivalent of changing a company's tax
  identity — the absence of any audit trail is a compliance gap, not
  cosmetic. The `tenant.settings_updated` action already exists; this
  call site just needs to use it.
- **Suggested fix**: After the `await db.commit()` (or before it,
  inside the same transaction), add an `audit_service.record(db,
  action="tenant.settings_updated", entity_type="tenant",
  entity_id=tenant.id, entity_label=tenant.name,
  actor=ActorInfo(type="user", id=user_target.id, label=identity.email),
  before={"billing_ico": old_ico, "billing_name": old_name, ...},
  after=cleaned, tenant_id=tenant.id)`. The existing tenant_admin
  call at line 759 is the canonical template.
- **Evidence**: `grep -n "audit_service\|audit\." app/platform/routers/billing.py`
  returns nothing; `tenant.settings_updated` is referenced from
  `app/routers/tenant_admin.py:761` only. The diff in `17a662d`
  introduces the route but never touches `audit_service`.

---

### F-BE-002 — Stripe checkout silently no-ops in production — PERSISTS
- **Where**: production env file `/etc/assoluto/env`; affects
  `app/main.py:_sync_stripe_prices_from_env` →
  `app/platform/billing/service.py:create_checkout_session`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **Description**: Read-only psql against prod still shows
  `stripe_price_id IS NULL` for all four `platform_plans` rows
  (community / starter / pro / enterprise). The new billing-details
  gate from `17a662d` makes this slightly worse: a tenant who fills
  in IČO + adresa now passes the local gate, hits Stripe checkout,
  and STILL silently no-ops because the price IDs aren't synced.
  Operator action remains the unblock.
- **Suggested fix**: unchanged from baseline. As a defensive code
  follow-up: emit `stripe_price.sync.no_env` (info level) when both
  env vars are empty, and surface a /platform/admin banner when any
  plan with `is_active=true` and `monthly_price_cents > 0` has no
  `stripe_price_id`. Today this is invisible until a tenant clicks
  Upgrade and the silent no-op shows up in support.
- **Evidence**: `psql ... -c 'SELECT code, stripe_price_id IS NOT
  NULL AS has_pid FROM platform_plans ORDER BY code'` →
  `community/enterprise/pro/starter` all `f`. Migration head
  `1006_drop_starter_orders_cap` matches local repo head.

---

### F-BE-003 — Zero test coverage for GDPR endpoints — PERSISTS
- **Where**: `app/routers/tenant_admin.py` (`GET
  /app/admin/profile/export`, `POST /app/admin/profile/delete`);
  service `app/services/gdpr_service.py`
- **Severity**: P1
- **Auto-fixable**: yes (deferred — needs test design, not a
  one-line auto-fix)
- **Description**: `grep -rln "gdpr\|GDPR\|profile/export\|
  profile/delete\|export_for_user\|erase_user\|export_for_contact\|
  erase_contact" tests/` still returns empty. No new tests added
  since baseline. The post-baseline commits added an `Identity`
  export path (`gdpr_service.py:150 export_for_identity`) — also
  uncovered.
- **Suggested fix**: unchanged from baseline (see F-BE-003 in
  `2026-05-01-1455/backend.md` for the proposed test cases). Add
  one for `export_for_identity` while you're in there.
- **Evidence**: empty grep result against `tests/`.

---

### F-BE-004 — `gdpr_service` contact export/erase have no router — PERSISTS
- **Where**: `app/services/gdpr_service.py:102 / :233`; gap in
  `app/routers/me.py`
- **Severity**: P2
- **Auto-fixable**: yes (deferred)
- **Description**: `app/routers/me.py` still exposes only three
  routes (`GET /profile`, `POST /profile`, `POST /profile/password`)
  — no `me/profile/export` or `me/profile/delete`. Customer
  contacts still have no self-service GDPR path; staff get one,
  contacts don't.
- **Suggested fix**: unchanged from baseline.
- **Evidence**: `grep -n "@router\." app/routers/me.py` → 3 matches
  (lines 72, 107, 134); none mention export/delete.

---

### F-BE-005 — Stripe webhook handler set is narrow — PERSISTS
- **Where**: `app/platform/billing/webhooks.py:428` (`HANDLERS`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Registry unchanged — same 8 handlers
  (`checkout.session.completed`, `customer.subscription.created /
  updated / deleted`, `invoice.paid / payment_failed`,
  `customer.subscription.trial_will_end`, `charge.refunded`). The
  three events called out last audit
  (`customer.subscription.paused`, `customer.updated`,
  `payment_method.detached`) are still silently dropped.
- **Suggested fix**: unchanged from baseline.
- **Evidence**: webhook file unchanged in the post-baseline diff
  (`git log a9d64e0..HEAD -- app/platform/billing/webhooks.py` is
  empty).

---

### F-BE-006 — `_safe_error_summary` regex misses non-URL token vectors
- **Where**: `app/tasks/email_tasks.py:60`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The new sanitiser strips URLs and `key=value` blobs
  with ≥12 char values. It does NOT redact tokens after a colon or
  whitespace (`Authorization: Bearer abcdef1234567890zzzzzzzzzz`,
  `X-Token: abcdefghij1234567890`, `Bearer bcdefghijklmnop123456`).
  The most common failure modes (URL-in-error from Brevo / SES;
  `email=... &token=...` query strings) ARE handled, so this is
  defence-in-depth rather than a hot leak — but in a stack-trace repr
  from a future SMTP library that uses the header form, a 20-char
  invite/reset token could land in the structured log. Also: the
  `=([A-Za-z0-9_-]{12,})` regex would happily redact a base64 chunk
  that happens to embed an `=` (e.g. `Content-Type: ...=charset` is
  safe but `boundary=----abcd1234efgh5678` would be redacted —
  cosmetic, not a bug).
- **Suggested fix**: add a second pattern for the header / Bearer
  form. One option: also redact any `[A-Za-z0-9_-]{20,}` whitespace-
  delimited token (over-broad but safer than under-broad), or split
  on whitespace and re-redact tokens that look like base64/jwt
  segments. Verify by running the pytest probe in
  `tests/test_email_throttle.py::*` style — none exists yet for the
  sanitiser, which is itself worth a test or two.
- **Evidence**: `.venv/bin/python -c '<the regex applied to 7 test
  strings>'` shows three vectors pass through unredacted (Bearer
  with whitespace, `X-Token:` header, `Authorization: Bearer`).

---

### F-BE-007 — No test for the verify-gate on `select-tenant` / `switch` / `complete-switch`
- **Where**: `app/platform/routers/platform_auth.py:296 / :359 / :471`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Commit `bfd1690` switched these three routes from
  `require_identity` to `require_verified_identity`. The 403→303
  HTML redirect in `app/main.py:434` routes the unverified user to
  `/platform/verify-sent` — a behavioural promise that has no
  regression test. `tests/test_platform.py::test_platform_owner_sees
  _tenant_in_select` only covers the verified-success path because
  the seeded admin/owner are pre-verified in the test setup
  (`pre_verified_identity=True` via `/platform/admin/tenants`).
  `tests/test_billing.py::test_billing_dashboard_refuses_unverified`
  proves the 403 happens for the billing route but not for these
  three. A future refactor that drops the `require_verified_identity`
  dependency from one of them would silently regress and not fail any
  test.
- **Suggested fix**: add three small tests that POST `/platform/signup`
  (which leaves the identity unverified by design), then
  - GET `/platform/select-tenant` → 303 to `/platform/verify-sent`
  - POST `/platform/switch/{slug}` → 303 to `/platform/verify-sent`
  - GET `/platform/complete-switch?token=...` → 303 to `/platform/verify-sent`
  The signup → unverified path is the same one
  `test_billing_dashboard_refuses_unverified` already uses; can be
  factored into a shared helper.
- **Evidence**: `grep -B5 "select-tenant\|switch" tests/test_platform.py
  tests/test_billing.py tests/test_signup.py` returns no negative
  test for the verify gate on these three paths.

---

### F-BE-008 — No test for the `/platform/billing/details` form
- **Where**: `app/platform/routers/billing.py:383 / :422`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Commit `17a662d` introduced both the GET form and
  the POST handler with IČO / DIČ / address validation, plus the
  gate redirect in `start_checkout` (line 234) and
  `post_verify_checkout` (line 548). None of the four code paths has
  a test:
  - `GET /platform/billing/details` (renders form prefilled from
    `tenant.settings`)
  - `POST` happy-path (writes `tenant.settings`, redirects with
    flash)
  - `POST` validation failures (4 separate `RedirectResponse(303)`
    branches for missing name / 8-digit IČO / missing address /
    malformed DIČ)
  - The two checkout gate-redirects to `/platform/billing/details`
    when `_billing_details_present(tenant)` is false
- **Suggested fix**: add `tests/test_billing_details.py` exercising
  the four POST validation branches + the happy path + the gate
  redirect from `start_checkout`. The gate test piggybacks on the
  existing demo-mode harness (`stripe_enabled=False` skips the
  gate, so we need to flip `STRIPE_SECRET_KEY` and assert the
  redirect Location header).
- **Evidence**: `grep -rln "billing/details\|billing_details\|
  REQUIRED_BILLING_KEYS\|billing_ico" tests/` is empty.

---

### F-BE-009 — Recent feature commits without paired tests (advisory)
- **Where**: `17a662d` (billing-details), `bfd1690` (verify-gate
  on switch/select-tenant), `075a957` (visible comment author batch
  lookup)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Three of the seven post-baseline commits ship
  visible behaviour with no test. F-BE-007 + F-BE-008 above cover
  the worst two. The third — comment-author batch lookup at
  `app/routers/orders.py:553` — is rendered on every order detail
  page; there's no test asserting that the rendered template
  actually shows the resolved name (vs an anonymous timestamp), so a
  template variable rename or a refactor that breaks the
  `comment_authors` dict would silently degrade UX without failing
  tests. Lower priority because it doesn't affect correctness or
  security, just visible polish.
- **Suggested fix**: add an assertion to an existing
  `test_orders_flow.py` lifecycle test that the GET on the order
  detail page contains the commenter's full name once at least one
  comment has been posted. ~5 lines.
- **Evidence**: `git log d914e3e..HEAD --stat -- tests/` shows zero
  new test files / additions in `tests/` since the previous audit
  (only doc + i18n catalogue + one `tests/test_orders_item_autosave.py`
  expansion in `17c1528`, which DID land the SUBMITTED/QUOTED
  coverage — that one is correctly tested).

---

### F-BE-010 — Hygiene baseline still green — RE-BASELINED
- **Where**: `.venv/bin/{ruff,mypy,pytest}` against `d0a5e35`
- **Severity**: P2 (informational)
- **Auto-fixable**: n/a
- **Description**: Re-running the full hygiene suite:
  - `ruff check .` → `All checks passed!`
  - `ruff format --check .` → `147 files already formatted`
  - `mypy app/` → `Success: no issues found in 87 source files`
    (identical to baseline — 0 errors, 87 files; no module count
    growth despite 7 new commits)
  - `pytest tests/ -q` → `427 passed, 12 warnings in 55.48s` (was
    `423 passed, 12 warnings in 57.60s`; +4 tests, slightly faster)
  - 12 warnings unchanged in shape — all the same slowapi
    `asyncio.iscoroutinefunction` deprecation. No new warning
    categories.
  - Slowest 10 unchanged in shape: two throttle-eviction tests
    (4.41s + 1.11s) and one token-expiry test (2.11s) dominate as
    before. Real test bodies all under 1s.
  Architecture invariants spot-checked:
  - Periodic-job advisory lock IDs (42_001 auto_close, 42_002
    invite_cleanup, 42_003 stripe_event_cleanup, 42_005 stripe price
    sync, 42_006 expire_trials, 42_007 enforce_canceled) — unique.
  - All 14 `background_tasks.add_task` call sites preceded by
    explicit `await db.commit()` on every write path
    (orders × 5, customers × 2, tenant_admin × 1, attachments × 1,
    signup × 3, public × 1, www × 1). The two read-only callers
    (`public.py` password reset, `signup.py:465` verify-sent
    re-render) don't need a commit because no DB write happens
    before the bg task.
  - `read_session(...)` only called from the legacy
    `read_session_for_tenant` wrapper itself and the
    `get_current_principal` dependency in `deps.py:263`, which
    immediately verifies `session_data.tenant_id == str(tenant.id)`
    on the next line. No bare unsafe caller introduced.
  - Migration chain head `1006_drop_starter_orders_cap` on both repo
    and prod. The two migrations sharing
    `down_revision="0008_customer_order_perms"`
    (`0009_audit_events` + `1002_identity_verification`) are an
    intentional branch that merges back at `0010_orders_delivered_at`
    via `down_revision = ('0009_audit_events',
    '1005_tenant_customer_uq')`. Alembic confirms a single head;
    `script.walk_revisions()` traverses cleanly.
  - `app.platform` import isolation: 11 import sites, all either
    runtime-local inside service functions (CLAUDE.md §17 plan-limit
    pattern) or pre-existing top-level boundary points (`app/main.py`,
    `app/templating.py`, `app/models/__init__.py`). New addition
    `app/services/gdpr_service.py:157` is runtime-local — fine.
    Test `test_platform_routes_not_mounted_when_flag_off` continues
    to pass.
  - Schema vs. ORM drift on `orders` and `platform_subscriptions`
    spot-checked via `\d` against prod — every column matches the
    ORM definition. No drift.
  - S3 boot-time `warn_if_public_endpoint_unreachable` reviewed:
    catches both `ClientError` and bare `Exception`, both branches
    log and return; the call site in `app/main.py:227` further wraps
    in its own `try/except` that logs `s3.public_endpoint_check_crashed`.
    Truly best-effort — no exception escapes app boot.
- **Suggested fix**: hold these counts as the next-run baseline.
  Regression triggers — any of: mypy >0, ruff non-clean, ruff
  format drift, pytest non-green, pytest >60s, new test warning
  categories, new `app.platform` import that's NOT runtime-local,
  schema drift on a heavy table.
- **Evidence**: command outputs above; full pytest run completed in
  55.48s on this machine.

---

### Recent-commits sanity check (advisory)

`git log a9d64e0..HEAD --oneline` — seven commits since the last
audit-fix batch:

```
d0a5e35 i18n: CS + DE translations for new billing-details, comments, status copy
0c7d468 chore(diag): per-attempt email logs + S3 public-endpoint boot check
17a662d feat(billing): require IČO + fakturační údaje before first paid checkout
17c1528 feat(orders): staff can edit items + prices in DRAFT/SUBMITTED/QUOTED
075a957 fix(ux): visible comment author + status from→to in feed + assets empty-state
d2a1f22 fix(billing): pin Enterprise plan rightmost + render 'Price on request'
bfd1690 fix(platform): require verified email for tenant switch + select-tenant
```

Backend-touching commits with paired tests:
- `17c1528` — direct test added (`test_staff_can_autosave_on_submitted_and_quoted`)
- `0c7d468` — diagnostics-only, no behaviour change worth a test;
  `_safe_error_summary` would benefit from one (see F-BE-006)

Backend-touching commits WITHOUT tests:
- `17a662d` — F-BE-008
- `bfd1690` — F-BE-007
- `075a957` — F-BE-009 (lower priority)

No new test gap on the audit/i18n commits (`d0a5e35`, `d2a1f22` —
template / catalogue changes).

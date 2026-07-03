# Backend audit — 2026-07-03-1153

5th automated audit. Tip-of-tree `39a8dfb`. Compared against the previous
backend run `2026-05-09-0931` (at `d3d911e`).

## Hygiene baseline movement

| Metric | Previous (`d3d911e`) | Now (`39a8dfb`) | Δ |
|---|---|---|---|
| ruff check | clean | clean | — |
| ruff format | 150 files | 152 files clean | +2, no drift |
| mypy `app/` errors | 0 (88 files) | **0 (89 files)** | +1 file, 0 errors — no regression |
| pytest | 457 passed | **489 passed, 0 skips** | +32 tests |
| pytest wall time | 62.75 s | 62.44 s | flat (still ~2.4 s over the 60 s budget) |
| pytest warnings | 12 (slowapi) | 12 (slowapi `iscoroutinefunction` deprecation) | — |

No new lints, no formatting drift, no mypy regression, no test failures or
skips. The only warnings remain the pre-existing slowapi
`asyncio.iscoroutinefunction` deprecations (library-side, not ours).

## Architecture invariants — all green

* **§6 platform import isolation** — 11 `from app.platform` sites, identical
  set to the previous run: module-level boundary points (`app/main.py`,
  `app/templating.py`, `app/models/__init__.py`,
  `app/services/invoice_pdf_service.py`) and runtime-local imports inside
  service functions (`attachment_service`, `auth_service` ×2,
  `order_service`, `gdpr_service`). No new cross-imports. Core→platform
  never at import time in a hot path.
* **§2 BackgroundTasks + explicit commit** — all 14 `add_task` sites checked.
  Every DB-writing path (`tenant_admin:198`, `orders:1093/1105/1199/1212/1296`,
  `customers:336/493`, `attachments:142`, `signup ×3`) has an explicit
  `await db.commit()` before the schedule. `public.py:768` (password reset)
  and `www.py:196` (contact form) pass all values as task args and perform no
  DB read in the task body, so §2 correctly does not apply.
* **§13 session-cookie tenant scoping** — bare `read_session(...)` only called
  from the `read_session_for_tenant` wrapper (`session.py:104/121`) and
  `deps.py:263`, which verifies `session_data.tenant_id == str(tenant.id)` on
  the next line. No unsafe public-route caller.
* **§11/§12 email-locale resolver** — every tenant-context email resolves via
  `resolve_email_locale`. The four direct `settings.default_locale` reads
  (`platform_auth.py:170`, `signup.py:241/464/512`) are pre-tenant identity
  flows with no tenant default to inherit — by design, unchanged. All
  `send_*` task functions accept a `locale` arg (regression check green).
* **Periodic advisory locks** — 42_001…42_007 all unique
  (`periodic.py` 001-003, 006, 007; `main.py` 004 demo-sub normaliser, 005
  stripe price sync). Reservation comments intact.
* **Alembic** — single head `1006_drop_starter_orders_cap`; prod
  `alembic_version` = `1006`. Every migration has a `downgrade`. The only
  shared `down_revision` is the documented `0008 → {0009, 1002}` branch
  merged back at `0010`. Chain walks cleanly base→head.
* **Schema vs ORM drift** — pulled prod `\d` for `orders`, `order_items`,
  `order_attachments`, `customer_contacts`, `platform_subscriptions` and
  diffed against the `Mapped[...]` declarations. Every column type,
  nullability, and default matches (incl. `customer_contacts.preferred_locale`
  from 0012). **No drift** — prod is fully migrated.

## Recent-commit sanity (advisory)

`git log --since="14 days ago"` → one commit: `8e16fcb`
(`fix(deploy): S3_PUBLIC_ENDPOINT_URL shadowed by dev default in base
compose`) — a compose/env fix following CLAUDE.md §15, no app code, no test
warranted. The contact-abuse hardening (`39a8dfb`) shipped with paired
`tests/test_contact_filter.py` (5 tests) + `tests/test_www.py` additions.
No feature landed without a paired test.

---

### F-BE-001 — GDPR endpoints have zero test coverage
- **Where**: `app/routers/tenant_admin.py:595` (`GET /app/admin/profile/export`), `:617` (`POST /app/admin/profile/delete`); `app/services/gdpr_service.py`
- **Severity**: P1
- **Auto-fixable**: yes (test authoring)
- **Description**: Carried unresolved from `2026-05-09-0931` F-BE-003 (originally `2026-05-01-1455`). These are the highest-stakes routes in the app: `profile/delete` performs an **irreversible GDPR Art. 17 PII anonymisation** guarded by (a) a password re-confirmation gate (`tenant_admin.py:638`) and (b) a last-admin lockout check that blocks the final TENANT_ADMIN from erasing themselves (`tenant_admin.py:650-676`). Neither guard, nor the export payload shape, nor the `session_version` bump, has a single regression test. A refactor that silently drops the last-admin guard would let an operator lock a tenant out of its own admin surface; a broken password gate would let a hijacked session trigger erasure. `grep -rln "gdpr|profile/export|profile/delete|export_for_user|erase_user|export_for_identity" tests/` → empty.
- **Suggested fix**: add `tests/test_gdpr.py` covering: export returns the user's data as JSON; delete with wrong password → 303 + "Password does not match" flash, no mutation; delete as last admin → 303 + "last administrator" flash, row intact; delete happy path → PII nulled, row retained, `user.gdpr_erased` audit row emitted, session cleared. ~5 test cases against `tenant_client`.
- **Evidence**: empty grep; route code reviewed at `tenant_admin.py:617-686`; `gdpr_service.export_for_user`/`erase_user`/`export_for_identity` uncovered.

---

### F-BE-002 — Stripe price IDs still NULL on all prod plans
- **Where**: production `/etc/assoluto/env`; `app/main.py:_sync_stripe_prices_from_env`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **Description**: Carried unresolved from `2026-05-09-0931` F-BE-002. All four `platform_plans` rows on prod still have `stripe_price_id IS NULL` while `is_active = t`. With no price ID, `_sync_stripe_prices_from_env` leaves the value alone and any checkout attempt for a paid plan silently no-ops — the SaaS billing path cannot complete a real subscription.
- **Suggested fix**: operator sets `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` in `/etc/assoluto/env` and redeploys. Defensive follow-up (still warranted, auto-fixable): emit `stripe_price.sync.no_env` at info level and surface an admin banner when an active paid plan has no price ID, so this can't sit silently for two more audits.
- **Evidence**: `SELECT code, stripe_price_id IS NOT NULL AS has_pid, is_active FROM platform_plans` on prod → `community/enterprise/pro/starter` all `has_pid = f`, `is_active = t`.

---

### F-BE-003 — Contact/identity GDPR export+erase have no router surface
- **Where**: `app/services/gdpr_service.py` (`export_for_identity`, contact erase paths); gap in `app/routers/me.py`
- **Severity**: P2
- **Auto-fixable**: no (needs product decision)
- **Description**: Carried from `2026-05-09-0931` F-BE-004. `me.py` still exposes only 3 routes (`GET /profile`, `POST /profile`, `POST /profile/password`) — no self-service export/delete for customer contacts, even though the service layer supports it. GDPR data-subject rights for contacts currently require an org-admin to act on their behalf out-of-band.
- **Suggested fix**: product decision — either expose `me/profile/export` + `me/profile/delete` for contacts (mirroring the staff routes with the same guards) or document that contact erasure is org-admin-mediated. Either way, wire the service methods that already exist.
- **Evidence**: `grep -n "@router\." app/routers/me.py` → 3 matches, none export/delete.

---

### F-BE-004 — Stripe webhook handler set stays narrow
- **Where**: `app/platform/billing/webhooks.py` (`HANDLERS`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Carried from `2026-05-09-0931` F-BE-005; the webhook file is untouched since. `customer.subscription.paused`, `customer.updated`, and `payment_method.detached` are still silently dropped. Not urgent while billing is dormant (see F-BE-002), but worth handling before the first live subscription so a paused sub or detached card reflects in-app.
- **Suggested fix**: add handlers (or explicit logged no-ops) for the three events; revisit against Stripe's current event catalogue when billing goes live.
- **Evidence**: `git log -- app/platform/billing/webhooks.py` shows no change since the previous audit's baseline.

---

### F-BE-005 — Billing-details audit-row test asserts existence only, not actor/diff
- **Where**: `tests/test_billing_details.py:204-215`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Carried from `2026-05-09-0931` F-BE-010, unchanged. The happy-path test asserts an `audit_events` row with `action='tenant.settings_updated'` exists for the tenant, but does not assert the `actor_*` columns point at the TENANT_ADMIN user (vs the platform Identity) nor that `before`/`after` carry the four billing keys with correct values. Production code is correct today (`ActorInfo(type="user", …)` + `before_subset`/`after_subset`), but the test is not a real regression net for the actor-attribution or diff-payload behaviour.
- **Suggested fix**: widen the `SELECT` to `actor_type, actor_id, actor_label, before_data, after_data` and assert `actor_type == 'user'`, `after_data['billing_ico'] == '12345678'`. ~8 lines.
- **Evidence**: lines 204-215 only check `len(rows) >= 1`.

---

### F-BE-006 — Order comment-author render still unasserted
- **Where**: `app/routers/orders.py:553` batch author lookup; gap in `tests/test_orders_flow.py`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Carried from `2026-05-09-0931` F-BE-009. No test asserts the rendered order-detail page includes the resolved commenter name once a comment exists. A regression in the author batch-resolve would render blank/"Unknown" author labels without any test catching it.
- **Suggested fix**: ~5 lines in the existing `test_full_order_lifecycle` — post a comment, then assert the author's `full_name` appears in the order-detail HTML.
- **Evidence**: `grep "author_name|commenter|comment_author" tests/test_orders_flow.py` → empty.

---

### F-BE-007 — Full pytest wall time over the 60 s budget (advisory)
- **Where**: full suite, current machine
- **Severity**: P2 (informational)
- **Auto-fixable**: no
- **Description**: Carried from `2026-05-09-0931` F-BE-011. Suite is 62.44 s (essentially flat vs 62.75 s; +32 tests absorbed with no per-test slowdown). Dominated by the same long-tail throttle/token tests: 4.41 s `test_stale_buckets_evicted_over_time`, 2.11 s `test_token_expiry`, 1.11 s `test_window_eviction`. Not a quality issue — flagged only because the agent contract names a 60 s threshold.
- **Suggested fix**: leave alone unless it creeps past 90 s; if CI fast-paths need it, gate the three eviction/expiry tests behind `@pytest.mark.slow`.
- **Evidence**: `pytest tests/ -q --durations=10` → `489 passed, 12 warnings in 62.44s`.

---

## Comparison with previous run (`2026-05-09-0931`)

### Resolved / held
All four previously-fixed backend findings (F-BE-001 billing audit row,
F-BE-006/F-SEC-001 `_safe_error_summary`, F-BE-007 verify-gate tests,
F-BE-008 billing-details tests) remain in place — regression checks green.

### Regressed
**None.** No architecture invariant broken; no hygiene regression.

### Persisted (renumbered this run)
- prev F-BE-002 → **F-BE-002** (Stripe price IDs NULL, P1)
- prev F-BE-003 → **F-BE-001** (GDPR test coverage, P1)
- prev F-BE-004 → **F-BE-003** (contact GDPR routes, P2)
- prev F-BE-005 → **F-BE-004** (webhook handler set, P2)
- prev F-BE-010 → **F-BE-005** (billing audit-row test depth, P2)
- prev F-BE-009 → **F-BE-006** (comment-author render assertion, P2)
- prev F-BE-011 → **F-BE-007** (pytest wall time, P2 advisory)

### New in this run
None. The codebase has been stable since the previous audit (one deploy-only
commit + one already-tested contact-abuse hardening batch).

| Severity | Count |
|---|---|
| P0 | 0 |
| P1 | 2 |
| P2 | 5 |

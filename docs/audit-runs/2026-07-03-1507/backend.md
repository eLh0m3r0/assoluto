# Backend audit — 2026-07-03-1507

6th automated audit, **verification pass**. Tip-of-tree `cbe5afa`. Compared
against the previous backend run `2026-07-03-1153` (baseline app code at
`39a8dfb`). Since that run only three fix commits touched files: `4eb457b`
(UX templates), `8a207b1` (i18n catalogs), `c674577` (new `tests/test_gdpr.py`
+ deepened `test_billing_details.py` / `test_orders_flow.py`), plus docs.

**Key result:** all three test-coverage fixes verified genuinely closed; no
hygiene or architecture-invariant regression; four carried manual findings
persist unchanged.

## Hygiene baseline movement

| Metric | Previous (`39a8dfb`) | Now (`cbe5afa`) | Δ |
|---|---|---|---|
| ruff check | clean | clean | — |
| ruff format | 152 files clean | 153 files clean | +1, no drift |
| mypy `app/` errors | 0 (89 files) | **0 (89 files)** | flat — no regression |
| pytest | 489 passed, 0 skips | **494 passed, 0 skips** | +5 (the new GDPR tests) |
| pytest wall time | 62.44 s | 61.00 s | −1.4 s, essentially flat |
| pytest warnings | 12 (slowapi) | 12 (slowapi `iscoroutinefunction`) | — |

No new lints, no formatting drift, no mypy regression, zero test failures or
skips. The only warnings remain the pre-existing library-side slowapi
`asyncio.iscoroutinefunction` deprecations.

## Verified — closed by `c674577`

* **F-BE-001 (P1, GDPR test coverage) → RESOLVED.** New `tests/test_gdpr.py`
  (5 e2e tests, all passing) covers exactly the guards flagged: export
  returns a JSON attachment payload (Art. 20); wrong-password cancels with the
  row intact (`password_hash is not None`); last-admin lockout blocks the final
  TENANT_ADMIN; happy path anonymises (`password_hash is None`,
  `session_version` bumped by 1, `_gdpr_erased_at` stamped), emits the
  `user.gdpr_erased` audit row, forces logout, and blocks re-login; plus a
  plain-STAFF self-erasure case proving the last-admin guard is role-scoped.
  `grep gdpr tests/` now returns `tests/test_gdpr.py`.
* **F-BE-005 (P2, billing audit-row depth) → RESOLVED.** `test_billing_details.py`
  now asserts `actor_type == 'user'`, `actor_id is not None`,
  `actor_label == 'o@bd2.cz'`, and the diff `before`/`after` billing keys
  (`billing_ico` None→'12345678', `billing_dic`, `billing_name`) — a real
  regression net for actor-attribution and the diff payload, not just row
  existence.
* **F-BE-006 (P2, comment-author render) → RESOLVED.** `test_orders_flow.py`
  now asserts the resolved author name `"Staff User"` renders on the
  order-detail page for **both** the staff and the contact view, guarding the
  batch author lookup against a blank/"Unknown" regression.

## Architecture invariants — all green (unchanged)

`git diff 39a8dfb..HEAD -- app/` shows **only** locale catalogs + four
templates changed — no router, service, model, task, or migration code was
touched. Every invariant therefore carries forward from the previous run's
verification; spot-re-checked and confirmed:

* **§6 platform import isolation** — 11 `from app.platform` sites, identical
  set to the previous run; no core→platform import-time cross-references.
* **§2 BackgroundTasks + explicit commit** — 11 router `add_task` sites,
  unchanged code; every DB-reading task still preceded by `await db.commit()`.
* **§13 session-cookie tenant scoping** — bare `read_session(...)` only inside
  the `read_session_for_tenant` wrapper (`session.py:104/121`) and `deps.py:263`
  (verifies tenant match on the next line). No unsafe public caller.
* **§11/§13 email-locale resolver** — unchanged; no new direct
  `settings.default_locale` bypass; all `send_*` tasks still take `locale`.
* **Periodic advisory locks** — `42_001`…`42_007` all unique; the repeated
  `42_004`/`42_005` hits are lock/unlock pairs + reservation comments, not
  collisions.
* **Alembic** — single head `1006_drop_starter_orders_cap`; the only shared
  `down_revision` is the documented `0008 → {0009, 1002}` branch merged at
  `0010`; every migration has a downgrade.
* **Schema vs ORM drift** — prod `alembic_version = 1006_drop_starter_orders_cap`
  = repo head, and no migration or model file changed since the previous run
  (which pulled `\d` for all five heavy tables and found zero drift). Prod is
  fully migrated; **no drift**.

## Recent-commit sanity (advisory)

`git log --since="14 days ago"` → the three fix commits above + docs. Every
feature-bearing change shipped with paired tests (`c674577` is itself the
test-authoring commit; `4eb457b`/`8a207b1` are template/i18n-only). No feature
landed without a test.

---

### F-BE-002 — Stripe price IDs still NULL on paid prod plans
- **Where**: production `/etc/assoluto/env`; `app/main.py:_sync_stripe_prices_from_env`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **Description**: Persists from `2026-07-03-1153` F-BE-002 (originally
  `2026-05-09-0931`). The two **paid** plans on prod — `pro`
  (149000 ¢/mo) and `starter` (49000 ¢/mo) — still have
  `stripe_price_id IS NULL` while `is_active = t`. With no price ID,
  `_sync_stripe_prices_from_env` leaves the value untouched and any checkout
  for a paid plan silently no-ops: the SaaS billing path cannot complete a
  real subscription. (`community`/`enterprise` are 0-cost and don't need one.)
- **Suggested fix**: operator sets `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO`
  in `/etc/assoluto/env` and redeploys. Defensive follow-up (auto-fixable, still
  warranted): emit `stripe_price.sync.no_env` at info level and surface an admin
  banner when an active paid plan has no price ID, so this can't sit silently
  through a fourth audit.
- **Evidence**: prod `SELECT code, stripe_price_id IS NOT NULL AS has_pid,
  is_active, monthly_price_cents FROM platform_plans` → `pro`/`starter` both
  `has_pid = f, is_active = t` with non-zero prices.

---

### F-BE-003 — Contact/identity GDPR export+erase have no router surface
- **Where**: `app/services/gdpr_service.py` (`export_for_identity`, contact erase paths); gap in `app/routers/me.py`
- **Severity**: P2
- **Auto-fixable**: no (needs product decision)
- **Description**: Persists from `2026-07-03-1153` F-BE-003. `me.py` still
  exposes only 3 routes (`GET /profile`, `POST /profile`, `POST /profile/password`)
  — no self-service export/delete for customer contacts, though the service
  layer supports it. Contact data-subject rights remain org-admin-mediated
  out-of-band.
- **Suggested fix**: product decision — expose `me/profile/export` +
  `me/profile/delete` for contacts (mirroring the now-tested staff routes with
  the same guards) or explicitly document that contact erasure is admin-mediated.
- **Evidence**: `grep "@router\." app/routers/me.py` → 3 matches, none export/delete.

---

### F-BE-004 — Stripe webhook handler set stays narrow
- **Where**: `app/platform/billing/webhooks.py` (`HANDLERS`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Persists from `2026-07-03-1153` F-BE-004; the file is
  untouched. `HANDLERS` covers 7 events (checkout.session.completed,
  subscription created/updated/deleted, invoice.paid, invoice.payment_failed,
  subscription.trial_will_end). `customer.subscription.paused`,
  `customer.updated`, and `payment_method.detached` are still silently dropped.
  Not urgent while billing is dormant (F-BE-002), but worth handling before the
  first live subscription.
- **Suggested fix**: add handlers (or logged no-ops) for the three events;
  revisit against Stripe's current event catalogue when billing goes live.
- **Evidence**: `HANDLERS` dict at `webhooks.py:428`; `git log` shows no change
  since the previous audit baseline.

---

### F-BE-007 — Full pytest wall time hovering at the 60 s budget (advisory)
- **Where**: full suite, current machine
- **Severity**: P2 (informational)
- **Auto-fixable**: no
- **Description**: Persists from `2026-07-03-1153` F-BE-007. Suite is 61.00 s
  (down from 62.44 s; the +5 GDPR tests were absorbed with no per-test
  slowdown). Dominated by the same long-tail throttle/token tests: 4.41 s
  `test_stale_buckets_evicted_over_time`, 2.11 s `test_token_expiry`, 1.11 s
  `test_window_eviction`. Not a quality issue — flagged only because the agent
  contract names a 60 s threshold.
- **Suggested fix**: leave alone unless it creeps past 90 s; otherwise gate the
  three eviction/expiry tests behind `@pytest.mark.slow`.
- **Evidence**: `pytest tests/ -q --durations=8` → `494 passed, 12 warnings in 61.00s`.

---

## Comparison with previous run (`2026-07-03-1153`)

### Resolved this run
- F-BE-001 (GDPR test coverage, was P1) — closed by `c674577`.
- F-BE-005 (billing audit-row test depth, P2) — closed by `c674577`.
- F-BE-006 (comment-author render assertion, P2) — closed by `c674577`.

### Regressed
**None.** No architecture invariant broken; no hygiene regression. All prior
fixes (billing audit row, `_safe_error_summary`, verify-gate tests,
billing-details tests) still hold.

### Persisted (renumbered stable — carried IDs kept)
- F-BE-002 (Stripe price IDs NULL, P1)
- F-BE-003 (contact GDPR routes, P2)
- F-BE-004 (webhook handler set, P2)
- F-BE-007 (pytest wall time, P2 advisory)

### New in this run
**None.**

| Severity | Count |
|---|---|
| P0 | 0 |
| P1 | 1 |
| P2 | 3 |

Verified held: 3 (F-BE-001, F-BE-005, F-BE-006). Regressed: 0.

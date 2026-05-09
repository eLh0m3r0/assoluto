# Backend audit — 2026-05-09-0931 (verification run)

## Summary

Verification pass against tip-of-tree `d3d911e`. Twelve findings from
`2026-05-09-0829` were targeted by the audit-fix cycle; the four
backend-domain fixes (F-BE-001, F-BE-006/F-SEC-001, F-BE-007,
F-BE-008) and the two cross-domain code shipments tied to backend
behaviour (HeadMethodMiddleware for F-UX-017, contact honeypot for
F-UX-019, robots.txt for F-UX-022) all hold. **No regressions, no
new findings against the four mandatory architecture invariants.**

Hygiene movement vs previous run:

| Metric | Previous (`d0a5e35`) | Now (`d3d911e`) | Δ |
|---|---|---|---|
| ruff check | clean | clean | — |
| ruff format | 147 files | 150 files | +3 (HeadMethodMiddleware module + 2 test files) |
| mypy `app/` errors | 0 (87 files) | 0 (88 files) | +1 file, 0 errors |
| pytest passed | 427 | 457 | +30 (11 billing-details + 11 head + 6 sanitiser + 1 honeypot + 1 robots) |
| pytest wall time | 55.48 s | 62.75 s | +7 s — slightly over the 60 s budget |
| pytest warnings | 12 (slowapi only) | 12 (slowapi only) | — |

Two informational notes:

1. The pytest wall-time overshoot (62.75 s vs 60 s budget) is purely
   the new tests landing — slowest 10 unchanged in shape (4.41 s
   throttle eviction, 2.11 s token expiry). The new tests are all
   sub-second. Worth a P2 advisory only because the budget threshold
   in the agent contract is "60 s"; not a real degradation.
2. mypy file count grew by 1 (`app/security/head_method.py` — the new
   middleware) and stays at 0 errors.

| Severity | Count | vs `2026-05-09-0829` |
|---|---|---|
| P0 | 0 | unchanged |
| P1 | 2 | -1 (F-BE-001 fixed) |
| P2 | 6 | -1 net (F-BE-006/-007/-008 all fixed; +F-BE-010 audit-row coverage gap, +F-BE-011 wall-time advisory) |

Per-finding status table:

| ID | Last status | Now | Notes |
|---|---|---|---|
| F-BE-001 | fixed (176c4bf) | held | audit row emits with correct actor + before/after diff |
| F-BE-002 | persisted (manual) | persisted | prod plans still all `stripe_price_id IS NULL` |
| F-BE-003 | persisted (manual) | persisted | zero GDPR test files |
| F-BE-004 | persisted (manual) | persisted | `me.py` still 3 routes, no export/delete |
| F-BE-005 | persisted (manual) | persisted | webhook HANDLERS untouched |
| F-BE-006 / F-SEC-001 | fixed (29994d0) | held | regex covers JWT, Bearer/Authorization, hex; 6 new tests pass |
| F-BE-007 | fixed (0eb0a56) | held | three verify-gate tests in `tests/test_billing_details.py` |
| F-BE-008 | fixed (0eb0a56) | held | 11 billing-details tests (form GET, POST happy + 5 validation, gate redirect) |
| F-BE-009 | persisted (advisory) | persisted | 075a957 comment-author render still has no template assertion |
| F-BE-010 | new | this run | audit-row test asserts existence only, not actor/diff |
| F-BE-011 | new | this run | pytest wall-time 62.75 s (advisory) |

Architecture invariants spot-checked, all green:

* CLAUDE.md §6 — `app.platform` import isolation: 11 import sites,
  every one is either runtime-local inside service functions
  (CLAUDE.md §17 plan-limit pattern in `auth_service`,
  `attachment_service`, `order_service`, `gdpr_service`) or
  pre-existing top-level boundary points (`app/main.py`,
  `app/templating.py`, `app/models/__init__.py`,
  `app/services/invoice_pdf_service.py`). No new cross-imports
  introduced. `test_platform_routes_not_mounted_when_flag_off`
  passes.
* CLAUDE.md §2 — every `background_tasks.add_task` writing-path
  call site is preceded by an explicit `await db.commit()`
  (orders × 5, customers × 2, attachments × 1, signup × 3,
  tenant_admin × 1, public × 1, www × 1).
* CLAUDE.md §13 — bare `read_session(...)` only called from the safe
  `read_session_for_tenant` wrapper (lines 104, 121 in
  `security/session.py`) and the `get_current_principal` dependency
  in `app/deps.py:263`, which immediately verifies
  `session_data.tenant_id == str(tenant.id)` on the next line. No
  unsafe public-route caller introduced.
* Periodic-job advisory IDs (42_001 auto_close, 42_002
  invite_cleanup, 42_003 stripe_event_cleanup, 42_004 demo-sub
  normaliser, 42_005 stripe price sync, 42_006 expire_trials, 42_007
  enforce_canceled) all unique. Comments still document the 42_004
  reservation correctly.
* Alembic — single head `1006_drop_starter_orders_cap` matches prod;
  the only duplicate `down_revision` is the documented intentional
  branch (`0008_customer_order_perms` → `0009_audit_events` +
  `1002_identity_verification`, merged back at
  `0010_orders_delivered_at`). Verified via `script.walk_revisions()`.
* Schema vs ORM — spot-checked `orders`, `order_items`,
  `order_attachments`, `customer_contacts`, `platform_subscriptions`
  on prod; every column type/nullability/default matches the
  `Mapped[...]` declarations. No drift.
* Email-locale resolver — `resolve_email_locale` is called on every
  tenant-context email (`tenant_admin.py`, `customers.py`,
  `public.py`). Three direct `settings.default_locale` accesses
  (`platform_auth.py:170`, `signup.py:241`, `signup.py:464`,
  `signup.py:512`) are pre-tenant identity flows where there's no
  tenant default to inherit — the comment at `platform_auth.py:166`
  documents this is by design. Not a regression.

---

### F-BE-001 — Billing-details audit row — RESOLVED (held)
- **Where**: `app/platform/routers/billing.py:480-506`
- **Severity**: P1 (was)
- **Auto-fixable**: yes (already done)
- **Description**: The previous-run finding ("`POST /platform/billing/details` writes IČO to `tenant.settings` without an audit row") is fully resolved by commit `176c4bf`. The implementation now: (a) re-resolves `_resolve_current_tenant` returning a TENANT_ADMIN `User` row in the target tenant; (b) builds `before_subset` and `after_subset` from the four billing keys; (c) only emits the audit row when the subsets differ (skips pure form-resubmits — sensible, keeps the log clean); (d) attributes via `ActorInfo(type="user", id=user_target.id, label=identity.email)` so the audit row is owned by the tenant-side user, with the platform identity's email as the human-readable label. Mirrors the canonical pattern at `tenant_admin.py:761`.
- **Suggested fix**: n/a — held.
- **Evidence**: code review of `app/platform/routers/billing.py:495-506`; `tests/test_billing_details.py::test_billing_details_post_happy_path_writes_settings` runs the SQL `SELECT action FROM audit_events WHERE tenant_id = (SELECT id FROM tenants WHERE slug='bd2') AND action = 'tenant.settings_updated'` and asserts ≥1 row exists. Test passes.

---

### F-BE-002 — Stripe checkout silently no-ops — PERSISTS (manual)
- **Where**: production `/etc/assoluto/env`; affects `app/main.py:_sync_stripe_prices_from_env`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **Description**: Re-checked prod via `\d`-style query — all four `platform_plans` rows still have `stripe_price_id IS NULL` (`community`, `enterprise`, `pro`, `starter` → all `f`). Tracked separately because this is operator action.
- **Suggested fix**: unchanged — set `STRIPE_PRICE_STARTER` and `STRIPE_PRICE_PRO` in `/etc/assoluto/env`. Defensive code follow-up still warranted: log `stripe_price.sync.no_env` info-level + admin banner when any active paid plan has no price ID.
- **Evidence**: `psql ... -c 'SELECT code, stripe_price_id IS NOT NULL AS has_pid FROM platform_plans ORDER BY code'` → `community/enterprise/pro/starter` all `f`.

---

### F-BE-003 — Zero GDPR endpoint test coverage — PERSISTS (manual)
- **Where**: `app/routers/tenant_admin.py` GDPR routes; `app/services/gdpr_service.py`
- **Severity**: P1
- **Auto-fixable**: yes (deferred — test design)
- **Description**: `grep -rln "gdpr|GDPR|profile/export|profile/delete|export_for_user|erase_user|export_for_contact|erase_contact" tests/` still empty. Unchanged from baseline.
- **Suggested fix**: unchanged — see `2026-05-01-1455/backend.md` F-BE-003.
- **Evidence**: empty grep result; `app/services/gdpr_service.py:150 export_for_identity` also still uncovered.

---

### F-BE-004 — `gdpr_service` contact export/erase have no router — PERSISTS (manual)
- **Where**: `app/services/gdpr_service.py:102 / :233`; gap in `app/routers/me.py`
- **Severity**: P2
- **Auto-fixable**: yes (deferred — needs product decision: self-service vs org-admin override)
- **Description**: `me.py` still exposes only three routes (`GET /profile`, `POST /profile`, `POST /profile/password`). No `me/profile/export` or `me/profile/delete`.
- **Suggested fix**: unchanged.
- **Evidence**: `grep -n "@router\." app/routers/me.py` → 3 matches; none mention export/delete.

---

### F-BE-005 — Stripe webhook handler set is narrow — PERSISTS (manual)
- **Where**: `app/platform/billing/webhooks.py:428` (`HANDLERS`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: 8 handlers unchanged. `customer.subscription.paused`, `customer.updated`, `payment_method.detached` still silently dropped.
- **Suggested fix**: unchanged.
- **Evidence**: `git log a9d64e0..HEAD -- app/platform/billing/webhooks.py` empty.

---

### F-BE-006 / F-SEC-001 — `_safe_error_summary` regex coverage — RESOLVED (held)
- **Where**: `app/tasks/email_tasks.py:60-95`; tests `tests/test_email.py:163-235`
- **Severity**: P2 (was)
- **Auto-fixable**: yes (already done)
- **Description**: Sanitiser now redacts five vector classes — full URLs, JWT-shape three-segment tokens, `key=value` blobs ≥12 chars, `Bearer|Authorization|X-Token: <token>` header forms (case-insensitive, supports `:`/`=`/space separator), and standalone hex blobs ≥32 chars. Truncation cap `cleaned[:160]` preserved. Six new pytest cases each exercise one redaction class plus one positive case asserting human-readable SMTP errors stay intact.
- **Suggested fix**: n/a — held.
- **Evidence**: `.venv/bin/python3 -m pytest tests/test_email.py -v --no-header 2>&1 | grep "safe_error"` shows 6 passing tests covering URL+query, JWT, Bearer/X-Token, hex blob, truncation cap, and human-readable preservation.

---

### F-BE-007 — Verify-gate regression tests — RESOLVED (held)
- **Where**: `tests/test_billing_details.py:105-150`
- **Severity**: P2 (was)
- **Auto-fixable**: yes (already done)
- **Description**: Three tests assert that `GET /platform/select-tenant`, `POST /platform/switch/{slug}`, and `GET /platform/complete-switch` all redirect an unverified Identity to `/platform/verify-sent` (303). Each test signs up a fresh identity (signup leaves `email_verified_at IS NULL` by design), then probes the route with `follow_redirects=False` and asserts the Location header. A future refactor that drops `require_verified_identity` from any of the three routes will now fail loudly.
- **Suggested fix**: n/a — held.
- **Evidence**: `pytest tests/test_billing_details.py::test_verify_gate_blocks_select_tenant_for_unverified tests/test_billing_details.py::test_verify_gate_blocks_switch_for_unverified tests/test_billing_details.py::test_verify_gate_blocks_complete_switch_for_unverified` → 3 passed.

---

### F-BE-008 — `/platform/billing/details` regression tests — RESOLVED (held)
- **Where**: `tests/test_billing_details.py` (286 lines, 11 tests)
- **Severity**: P2 (was)
- **Auto-fixable**: yes (already done)
- **Description**: Eight tests cover the form (1 GET render, 1 POST happy-path including the audit-row assertion, 5 parametrised validation branches, 1 checkout gate redirect when `STRIPE_SECRET_KEY` is set but `tenant.settings` is missing the four billing keys). The `billing_client` fixture is inlined locally with a comment explaining pytest's fixture-discovery limitation — clean approach.
- **Suggested fix**: n/a — held. (See F-BE-010 for one minor coverage gap inside the happy-path test.)
- **Evidence**: `pytest tests/test_billing_details.py -v --no-header` → 11 passed in 2.97 s.

---

### F-BE-009 — Recent commits without paired tests — PERSISTS (advisory)
- **Where**: still `075a957` (comment-author batch lookup at `app/routers/orders.py:553`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The two priority cases (F-BE-007, F-BE-008) are now closed. The comment-author render assertion remains uncovered — no test asserts that the rendered order detail page actually includes the resolved commenter name once a comment exists.
- **Suggested fix**: unchanged — ~5 lines in an existing `test_orders_flow.py` lifecycle test.
- **Evidence**: `git log d3d911e --stat -- tests/` since the previous baseline shows `tests/test_billing_details.py` (+286), `tests/test_email.py` (+77), `tests/test_head_method.py` (+35), `tests/test_www.py` (+33) — no addition under `tests/test_orders_flow.py`.

---

### F-BE-010 — Audit-row test asserts existence only, not actor or diff — NEW
- **Where**: `tests/test_billing_details.py:204-215`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The happy-path billing-details test correctly asserts that an `audit_events` row with `action='tenant.settings_updated'` is created for the right tenant. It does NOT assert that the `actor_*` columns point at the TENANT_ADMIN user (vs the platform Identity), nor that the `before` / `after` JSON columns carry the four billing keys with the right values. A future refactor that flips the actor (e.g. by mistake reverts to `actor=ActorInfo(type="identity", ...)`) or drops a key from the diff will land silently. Code review verifies the production code does the right thing today (`actor=ActorInfo(type="user", id=user_target.id, label=identity.email)` and `before_subset`/`after_subset` carry the four keys), but the test is the regression net.
- **Suggested fix**: extend the existing `SELECT` to `SELECT actor_type, actor_id, actor_label, before_data, after_data FROM audit_events WHERE ...` and assert `actor_type == 'user'`, `before_data['billing_ico'] == ''` (the seeded baseline) and `after_data['billing_ico'] == '12345678'`. About 8 lines.
- **Evidence**: lines 204-215 only check `len(rows) >= 1`; no actor or diff assertion.

---

### F-BE-011 — pytest wall time 62.75 s exceeds 60 s budget (advisory) — NEW
- **Where**: full suite, current machine
- **Severity**: P2 (informational)
- **Auto-fixable**: no
- **Description**: Full pytest run now takes 62.75 s vs 55.48 s previously. The slowdown is entirely the 30 new tests landing — slowest 10 unchanged in shape (4.41 s `test_stale_buckets_evicted_over_time` + 2.11 s `test_token_expiry` + 1.10 s `test_window_eviction` dominate; everything else under 1 s; the 11 new billing-details tests run in 2.97 s combined). Listed because the agent contract calls out the 60 s threshold; not a quality issue.
- **Suggested fix**: leave alone unless wall-time creeps past 90 s. If/when the long-tail throttle eviction tests need attention, gate them behind `@pytest.mark.slow` and skip in CI fast-paths.
- **Evidence**: `pytest tests/ -q --durations=10` → `457 passed, 12 warnings in 62.75s`.

---

### HeadMethodMiddleware verification (cross-domain check)

Reviewed `app/security/head_method.py` and the wiring in `app/main.py:319` against the three risks called out in the agent prompt:

* **Exception leak on non-HEAD methods.** Line 28 short-circuits with `if scope["type"] != "http" or scope.get("method") != "HEAD": await self.app(scope, receive, send); return`. Non-HTTP scopes (websockets, lifespan) and non-HEAD HTTP methods bypass entirely. No try/except needed because the inner `await self.app(...)` is the same call shape Starlette would have made.
* **Double-strip on streaming responses.** The `head_send` wrapper checks `message["type"]` and only rewrites the `http.response.body` chunks (sets `body=b""`, preserves `more_body` flag). `http.response.start` passes through verbatim, so headers (incl. `Content-Length` from the GET handler) are kept — RFC 9110 §9.3.2 explicitly permits HEAD to echo GET's Content-Length. Streaming responses with `more_body=True` get their bodies replaced chunk-by-chunk; the final chunk with `more_body=False` still terminates correctly.
* **Log context confusion.** `LogContextMiddleware` is added LAST in `app/main.py:347`, which makes it OUTERMOST (Starlette's `add_middleware` does `user_middleware.insert(0, ...)`, so first-added = innermost). `HeadMethodMiddleware` is added FIRST → INNERMOST. The original `scope["method"] == "HEAD"` is therefore visible to LogContext when it stamps the structlog request var; the rewritten `GET` only exists inside the FastAPI router's scope. Verified by reading `add_middleware` source via `inspect.getsource`. The middleware also creates a fresh `rewritten` dict via `{**scope, "method": "GET"}` rather than mutating `scope` in place, which means even a future middleware ordering change wouldn't poison the outer logger context.

11 parametrised tests in `tests/test_head_method.py` cover marketing pages, infra endpoints (`/healthz`, `/readyz`, `/sitemap.xml`, `/robots.txt`), and the platform auth pages. All pass.

---

### Honeypot extension verification (cross-domain check)

Reviewed `app/routers/www.py:92-125`:

* **Rate-limit still applied.** The `@rate_limit("5/15 minutes")` decorator wraps the handler — runs before the function body, so honeypot tripping does not bypass the limit.
* **Required-field validation runs only when honeypot is empty.** Lines 111-125 (the honeypot branch) `return` before the `_reject` validators on line 144. A real submission with empty `website` falls through to the existing required-fields / length / email-validity branches.
* **Body content not logged in the silent-success path.** The honeypot log line `get_logger("app.contact").info("contact.honeypot_tripped", length=len(website))` records only the byte length of the trip field — none of `name`, `email`, or `message` are logged. (Even the trip-field content itself is reduced to a length count.) The render call `_templates(request).render(...)` produces an HTML response but writes to the response body, not the log pipeline.

`tests/test_www.py:226 test_contact_form_honeypot_silently_drops_bot_submission` asserts the success page is rendered (`status_code == 200` + "Message sent"/"Zpráva odeslána" in body) AND that `len(sender.outbox) == 0`. Solid coverage.

---

### robots.txt verification

`/Users/vaclav/workspace/assoluto/app/routers/www.py:240-261` confirms `/platform/signup` is no longer in the disallow list. Live check `curl -s https://assoluto.eu/robots.txt` confirms the production response matches:

```
User-agent: *
Allow: /
Disallow: /app
Disallow: /app/
Disallow: /auth/
Disallow: /platform/admin
Disallow: /platform/admin/
Disallow: /platform/login
Disallow: /platform/password-reset

Sitemap: https://assoluto.eu/sitemap.xml
```

One regression test in `tests/test_www.py` asserts the negative.

---

## Comparison with previous run (`2026-05-09-0829`)

### Resolved (4 — all held)
- F-BE-001 — billing-details audit row (`176c4bf`)
- F-BE-006 / F-SEC-001 — `_safe_error_summary` regex hardening (`29994d0`)
- F-BE-007 — verify-gate regression tests (`0eb0a56`)
- F-BE-008 — `/platform/billing/details` regression tests (`0eb0a56`)

### Persisted (5 — all manual / operator action)
- F-BE-002, F-BE-003, F-BE-004, F-BE-005, F-BE-009

### Regressed
*None.* Every previously-fixed finding still passes its regression check; no architecture invariant broken.

### New in this run
- F-BE-010 (P2) — billing-details audit-row test asserts existence only, not actor/diff
- F-BE-011 (P2) — pytest wall time 62.75 s slightly over the 60 s budget (informational)

### Manual / operator action (carried over)
- F-BE-002 — set `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` in `/etc/assoluto/env`
- F-BE-003 — design GDPR endpoint test cases
- F-BE-004 — product decision on contact self-service GDPR routes vs org-admin override
- F-BE-005 — extend Stripe webhook handler set per product judgement

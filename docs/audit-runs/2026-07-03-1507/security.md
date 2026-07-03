# Security audit — run 2026-07-03-1507

6th automated pass — VERIFICATION. Regression detection + new-surface coverage.
Previous run: `docs/audit-runs/2026-07-03-1153/`.

## Built-in /security-review output

Working tree is clean (`nothing to commit`); no branch delta vs `origin/main`
and an empty diff vs `origin/production` (everything is deployed). The bundled
`/security-review` skill is diff-scoped, so with no changes to analyse it
returned **no findings**. All results below come from Phase 2 static-analysis of
the project-specific invariants + a live header probe.

## Phase 2 — project invariant results

| # | Invariant | Result |
|---|---|---|
| 1 | RLS isolation (`TenantMixin` on tenant-scoped models) | PASS — no model carries a `tenant_id` column without `TenantMixin`. The lone `tenant_id` grep hit in `models/tenant.py` is the docstring of `Tenant` (the tenant registry — the one table intentionally without a `tenant_id`). |
| 2 | CSRF coverage on every router | PASS — all 15 tenant + platform routers carry `dependencies=[Depends(verify_csrf)]`. `billing.py` splits a bare `router` (only `@router.get` reads + the Stripe webhook `POST /platform/webhooks/stripe`) from `csrf_router` (every state-changing POST: checkout, cancel, details, post-verify, portal). `health.py` is read-only GETs (`/healthz`, `/readyz`). |
| 3 | Session-cookie tenant binding | PASS — no bare `read_session(` on any public router (`public.py`, `www.py`, `platform/routers/*`). Public pages use `read_session_for_tenant`. |
| 4 | Single-use recovery tokens | PASS — `tests/test_token_replay.py` present, 4 tests (contact-invite, staff-invite, password-reset replay + unrelated session-version bump). |
| 5 | Rate-limit on email-sending public POSTs | PASS — login/invite/staff-invite/password-reset/reset-confirm (`public.py`), signup/check-email/verify-resend (`signup.py`, plus per-email limit), login/password-reset/reset-confirm (`platform_auth.py`) all `@rate_limit`-decorated. Non-email POSTs (logout, switch) correctly undecorated. |
| 6 | Secret leakage | PASS — targeted grep for `sk_live_`, real `whsec_`, embedded DSN creds, `AKIA…` across `*.py`/`*.html` (excluding tests/examples/docs) returned nothing. |
| 7 | Stripe webhook signature | PASS — `billing.py:701 stripe_webhook` calls `verify_webhook(settings, payload, sig)` and only reaches `dispatch_webhook` (line ~769) on success; a bad signature raises `HTTPException(400)` and short-circuits before any handler. |
| 8 | File-upload MIME allow-list | PASS — `ALLOWED_CONTENT_TYPES` unchanged: pdf/png/jpeg/webp + CAD (acad/dwg/dxf) + `application/octet-stream` fallback. No `text/html`, no executable types. Enforced at `attachment_service.py:105` before PUT. |
| 9 | GDPR endpoints reachable + tested | **PASS (was F-SEC-001 FAIL)** — `tests/test_gdpr.py` (5 e2e tests, added c674577) covers export payload + attachment disposition, wrong-password gate, last-admin lockout, happy-path anonymisation, staff self-erasure. |
| 10 | Email-verification gate | PASS — `auth_service.authenticate` calls `_has_unverified_identity` after the password check. No regression. |
| 11 | HTTPS-only cookies in prod | PASS — `public.py:119/373` (`secure=settings.is_production`), `session.py`/`platform/session.py` (`secure=` param threaded), `csrf.py` (Secure appended on https). Live probe confirms `csrftoken … Secure`. |
| 12 | Live config probe | PASS — see below. |

### Live header probe (single-shot)

`GET https://assoluto.eu/` and `/healthz` both **200** with:
- `strict-transport-security: max-age=31536000; includeSubDomains`
- `content-security-policy: default-src 'self'; script-src 'self'; … object-src 'none'; frame-ancestors 'none'; form-action 'self' https://*.assoluto.eu https://assoluto.eu https://checkout.stripe.com https://billing.stripe.com; base-uri 'self'`
- `x-frame-options: DENY`, `x-content-type-options: nosniff`, `referrer-policy: same-origin`, `permissions-policy: geolocation=(), microphone=(), camera=()`
- No `Server` header, no version leak. `csrftoken` cookie is `Secure; SameSite=lax`.

Nits (not filed): HSTS lacks `preload`; `/healthz` sets a `csrftoken` cookie it
doesn't need. Both optional hardening, not vulnerabilities.

---

## Findings

### F-SEC-001 — Dead `request.method == "HEAD"` guard on set-lang route (persisted)
- **Where**: `app/routers/public.py:363` (guard) vs `app/security/head_method.py:33` (middleware)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Persisted (was F-SEC-002 in run 2026-07-03-1153, and earlier). `HeadMethodMiddleware` (mounted `main.py:319`, runs before routing) rewrites `scope["method"]` from `HEAD` to `GET`, so by the time the `/set-lang` handler executes `request.method` is always `"GET"`. The `if request.method == "HEAD": return response` early-return meant to suppress `Set-Cookie` on HEAD is therefore dead code — a HEAD probe of the route still sets the locale cookie. Impact is low: the cookie is a non-security UI locale preference; the one-shot-token routes stay protected by their single-use guarantee regardless of verb. Flagged for continuity only — the guard is misleading.
- **Suggested fix**: Drop the dead guard, or have `HeadMethodMiddleware` stash the original verb (`scope["original_method"]`) for the handler to read. This was explicitly left as a manual fix by the previous run.
- **Evidence**: `head_method.py:33` `rewritten: Scope = {**scope, "method": "GET"}`; guard at `public.py:363` can never evaluate `True` for an inbound HEAD.

---

## Verified-held / regressed

- **Resolved this run**: F-SEC-001 (prev run) — GDPR export/erasure test gap, closed by `tests/test_gdpr.py` (c674577, 5 e2e tests). Verified: file present, references `/app/admin/profile/export` + `/profile/delete`, suite reported 494 passed.
- **Held (no regression)**: all 12 core invariants — RLS, CSRF, session-tenant binding, single-use tokens, rate-limits, no secret leakage, webhook signature, MIME allow-list, email-verification gate, secure cookies, live headers.
- **Persisted**: F-SEC-002 (prev) → renumbered F-SEC-001 here — dead HEAD guard, left as manual.
- **Regressed**: none.

## Summary

- **P0**: 0
- **P1**: 0
- **P2**: 1 (F-SEC-001 dead HEAD guard, persisted/manual)

No new P0/P1/P2 findings. 1 prior finding verified resolved (GDPR tests), 12
invariants held, 0 regressions. Live production headers remain strong.

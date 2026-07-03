# Security audit — run 2026-07-03-1153

5th automated pass. Focus: regression detection + new-surface coverage.
Previous run: `docs/audit-runs/2026-05-09-0931/`.

## Built-in /security-review output

Working tree is clean (`nothing to commit`, no branch diff vs `origin/main`).
The bundled `/security-review` skill is diff-scoped; with an empty diff it has
no changes to analyse and returned **no findings**. All findings below come from
Phase 2 static-analysis of the project-specific invariants.

## Phase 2 — project invariant results

| # | Invariant | Result |
|---|---|---|
| 1 | RLS isolation (`TenantMixin` on tenant-scoped models) | PASS — `asset/order/customer/user/product` all inherit `TenantMixin`; `tenant.py` (`TenantRef`) and `enums.py` are not tenant-scoped. No `tenant_id` model missing the mixin. |
| 2 | CSRF coverage on every router | PASS — all tenant + platform routers carry `dependencies=[Depends(verify_csrf)]`. `billing.py` splits a bare `router` (Stripe webhook only, documented exception) from `csrf_router` (all POSTs). `health.py` is read-only GETs. |
| 3 | Session-cookie tenant binding | PASS — bare `read_session` only appears in `deps.py:263` (authenticated `get_current_principal`, which re-validates tenant downstream) and inside `read_session_for_tenant` itself. No public route uses the bare call. |
| 4 | Single-use recovery tokens | PASS — `tests/test_token_replay.py` present, 4 tests covering contact-invite, staff-invite, password-reset replay + unrelated session-version bump. |
| 5 | Rate-limit on email-sending public POSTs | PASS — login/invite/staff-invite/password-reset/reset-confirm (public.py), signup/check-email/verify-resend (signup.py), login/password-reset/reset-confirm (platform_auth.py) all `@rate_limit`-decorated. |
| 6 | Secret leakage | PASS — only `sk_test_fake` / `whsec_test` placeholders in `tests/test_billing*.py` and `sk_live_...`/`whsec_...` doc placeholders. No live keys, DSNs, or AWS keys. |
| 7 | Stripe webhook signature | PASS — `billing.py:702 stripe_webhook` calls `verify_webhook(settings, payload, sig)` (line 728) and only reaches `dispatch_webhook` (line 769) on success; invalid sig short-circuits. |
| 8 | File-upload MIME allow-list | PASS — `ALLOWED_CONTENT_TYPES` = pdf/png/jpeg/webp + CAD (acad/dwg/dxf) + `application/octet-stream` fallback. No `text/html`, no executable types. Enforced at `attachment_service.py:105` before PUT. |
| 9 | GDPR endpoints reachable + tested | **FAIL (test gap)** — see F-SEC-001. |
| 10 | Email-verification gate | PASS — `auth_service.authenticate` (line 104) calls `_has_unverified_identity` (line 136) after the password check. No regression. |
| 11 | HTTPS-only cookies in prod | PASS — every `set_cookie` passes `secure` correctly: `public.py:367` (`secure=settings.is_production`), `session.py`/`platform/session.py` (`secure=` param), `csrf.py` (Secure appended when scheme is https). |
| 12 | Live config probe | PASS — see below. |

### Live header probe (single-shot HEAD)

`curl -sI https://assoluto.eu/` and `/healthz` both 200 with:
- `strict-transport-security: max-age=31536000; includeSubDomains`
- `content-security-policy: ... frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self' https://*.assoluto.eu ... checkout.stripe.com ...`
- `x-frame-options: DENY`, `x-content-type-options: nosniff`, `referrer-policy: same-origin`, `permissions-policy: geolocation=(), microphone=(), camera=()`
- No `Server` header, no version leak. `csrftoken` cookie has `Secure; SameSite=lax`.

Only nit (not filed): HSTS lacks `preload` — optional hardening, not a vuln.

---

## Findings

### F-SEC-001 — GDPR export/erasure endpoints have zero test coverage
- **Where**: `app/routers/tenant_admin.py:595` (`/app/admin/profile/export`), `app/routers/tenant_admin.py:617` (`/app/admin/profile/delete`); service `app/services/gdpr_service.py` (`export_for_user`, `erase_user`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Invariant #9 requires these endpoints to exist **and** have at least one test exercising the happy path. The endpoints exist and are password-gated, but no test anywhere references `profile/export`, `profile/delete`, `export_for_user`, or `erase_user` (grep across `tests/` returns nothing). `profile/delete` is a **destructive, irreversible PII-anonymization** path (Art. 17 erasure): a silent regression — e.g. the last-admin guard breaking, or `erase_user` nulling the wrong columns / clobbering order history — would ship undetected. This is a coverage regression risk on a compliance-critical destructive endpoint, not an active vulnerability. (Contact-side self-service export/erasure remains the separately-tracked `F-BE-004`.)
- **Suggested fix**: Add `tests/test_gdpr.py` covering: (a) `/app/admin/profile/export` returns JSON with the user's PII and `Content-Disposition: attachment`; (b) `/app/admin/profile/delete` with correct password anonymizes PII while preserving order/audit rows; (c) wrong password cancels; (d) last-remaining `TENANT_ADMIN` is blocked from self-erasure.
- **Evidence**: `grep -rn "export_for_user\|erase_user\|profile/export\|profile/delete" tests/` → no matches; `tests/test_admin_flow.py` only exercises `/app/admin/profile/password`.

### F-SEC-002 — Dead `request.method == "HEAD"` guard on set-lang route (persisted)
- **Where**: `app/routers/public.py:363` (guard) vs `app/security/head_method.py:33` (middleware)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Persisted from run `2026-05-09-0931` (was F-SEC-002, unfixed). `HeadMethodMiddleware` rewrites `scope["method"]` from `HEAD` to `GET` **before** routing, so by the time the set-lang handler runs `request.method` is always `"GET"`. The `if request.method == "HEAD": return response` early-return that was meant to suppress the `Set-Cookie` on HEAD requests is therefore dead code — a HEAD probe of the set-lang route now sets the locale cookie. Impact is low (the cookie is a non-security UI locale preference; the one-shot-token routes remain protected by their token single-use guarantee regardless of verb). Flagged for continuity: the guard is misleading and should either be removed or made effective.
- **Suggested fix**: Either drop the now-dead HEAD guard, or teach `HeadMethodMiddleware` to record the original verb in `scope` (e.g. `scope["original_method"]`) and have the handler check that. Prefer removing the guard and relying on the middleware's body-stripping if the Set-Cookie on HEAD is acceptable.
- **Evidence**: `head_method.py:33` `rewritten: Scope = {**scope, "method": "GET"}`; the guard at `public.py:363` can never be `True` for an inbound HEAD.

---

## Summary

- **P0**: 0
- **P1**: 0
- **P2**: 2 (F-SEC-001 GDPR test gap; F-SEC-002 dead HEAD guard, persisted)

No new P0/P1 regressions. All 12 core security invariants hold except the GDPR
test-coverage requirement. Live production headers are strong (full CSP + HSTS
includeSubDomains + XFO DENY + nosniff, no version leak).

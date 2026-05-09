# Security audit — verification run 2026-05-09-0931

**Tip-of-tree**: `d3d911e`
**Previous run**: `2026-05-09-0829` (1 SEC finding, F-SEC-001 = F-BE-006, marked fixed in `29994d0`)
**Diff range**: `production..main` (9 commits)
**Scope**: regression check on previously-fixed item + per-commit review of new surface.

## Executive summary

| Bucket | Count |
|---|---|
| Resolved (held since `2026-05-09-0829`) | 1 (F-SEC-001) |
| New P0 | 0 |
| New P1 | 0 |
| New P2 | 1 (F-SEC-002 — HEAD-middleware nullifies `set_lang` HEAD guard) |
| Informational confirmations | 5 |

## Built-in `/security-review` output

The `security-review` skill ran against `production..main` but received an empty diff (the harness piped no content). Substituted with manual per-commit walkthrough below — every commit identified by the user prompt was inspected at source level.

## Resolved since previous run

### F-SEC-001 (= F-BE-006) — `_safe_error_summary` regex misses bare token shapes — **FIXED, HELDS**
- **Fix commit**: `29994d0`
- **Verification**: read `app/tasks/email_tasks.py:60-95`. Three new patterns added (JWT-shape, Bearer/Authorization header form, hex blob ≥32 chars) plus the pre-existing URL + `key=value` patterns. New tests in `tests/test_email.py` exercise each branch and assert the 160-char truncation cap.
- **Regex safety check**: each pattern is a single character class (no nested quantifiers, no overlapping alternations). No ReDoS risk on adversarial input. The four alternatives in the Bearer-form group are literal keywords, not nested quantifiers — linear time.
- **Order check**: regex substitutions happen BEFORE truncation (`return cleaned[:160]` is the last line), so a token that would straddle the truncation breakpoint is redacted to `[jwt]` / `[redacted]` / `[hex]` first. No mid-token leakage at the 160 boundary.
- **Under-redaction note**: a JWT with header/payload/signature segments each <20 chars would not match. Real-world JWTs always have payload >20 chars, so this is a theoretical gap only. Not worth a separate finding.

## New findings

### F-SEC-002 — `HeadMethodMiddleware` rewrites `scope["method"]` BEFORE the route runs, neutering the explicit `request.method == "HEAD"` guard on `/set-lang`
- **Where**: `app/security/head_method.py:33` (the `{**scope, "method": "GET"}` rewrite) ↔ `app/routers/public.py:363` (the now-dead `if request.method == "HEAD": return response` guard).
- **Severity**: **P2**
- **Auto-fixable**: yes
- **Description**: The HEAD-from-GET middleware mutates `scope["method"]` to `"GET"` before dispatching to the inner ASGI app. Starlette's `Request.method` reads `scope["method"]`, so by the time `set_language()` runs, `request.method == "HEAD"` evaluates to `False` — even when the original probe was HEAD. The defensive `if request.method == "HEAD": return response` early-return at `public.py:363` is now unreachable code. The intended behaviour was: "HEAD probes don't mutate state — return the redirect headers without `Set-Cookie`." Actual behaviour today: a HEAD probe to `/set-lang?lang=de&next=/` flips the `sme_locale` cookie of any client that accepts cookies on HEAD responses (most uptime monitors don't, but link-checkers and email-scanner previewers do). Impact is bounded (locale preference, not a security context), but it's a behavioural regression that the old `set-lang` author explicitly tried to prevent.
- **Threat model**: low. The locale cookie is non-security-critical and the only mutation here is a single cookie write. No CSRF impact (HEAD doesn't transmit a body, but the route doesn't read one either). No authn/authz impact. Worth fixing because (a) the dead-code comment misleads future readers about runtime behaviour, (b) it normalises silently neutralising existing HEAD guards if more state-mutating GET endpoints get one in the future.
- **Suggested fix**: stash the original method on `scope` so handlers that care can still see it. In `head_method.py`:
  ```python
  rewritten: Scope = {**scope, "method": "GET", "_original_method": "HEAD"}
  ```
  And add a helper `original_method(request: Request) -> str` that returns `scope.get("_original_method") or request.method`. Update the `set-lang` guard to use it. Alternative: scope HeadMethodMiddleware to skip routes that explicitly declare HEAD via `methods=["GET", "HEAD"]` — `/set-lang` already has its own HEAD handling registered.
- **Evidence**: `app/security/head_method.py:33` shows `rewritten: Scope = {**scope, "method": "GET"}`. Starlette `Request.method` returns `self.scope["method"]` — verified in `starlette/requests.py`. The `set-lang` guard at `app/routers/public.py:363` therefore branches on `"GET" == "HEAD"`, never True under HEAD probes.

## Project invariants — re-verified PASS

| # | Invariant | Result |
|---|---|---|
| 1 | RLS isolation — every tenant-scoped model inherits `TenantMixin`, migrations include policy block | PASS — 8 models inherit `TenantMixin`; `tenant.py` is the parent table itself. No drift introduced this cycle. |
| 2 | CSRF coverage — every router has `dependencies=[Depends(verify_csrf)]` (or per-route) | PASS — `app/platform/routers/billing.py` continues to split into a non-CSRF GET router (`router`) for the Stripe webhook + a CSRF-protected `csrf_router` for everything else. The new `billing_details_save` is on `csrf_router`. |
| 3 | Public routes use `read_session_for_tenant`, not bare `read_session` | PASS — `read_session()` direct callers limited to `app/deps.py:263` (which manually re-checks `session_data.tenant_id != str(tenant.id)` immediately after) and `app/security/session.py` itself. Public router uses the `_for_tenant` helper. |
| 4 | Single-use recovery tokens (`reset_password_with_token`, `accept_invitation`, `accept_staff_invite`) | PASS — `tests/test_token_replay.py` still present and exercises all three. |
| 5 | Rate-limit decorator covers public POSTs that send mail | PASS — `contact_submit` keeps `@rate_limit("5/15 minutes")` ABOVE the function definition; the new honeypot check inside the function is gated by the rate-limit (rate-limit fires first → bot can't probe the limit threshold via honeypot). |
| 6 | Secret leakage scan | PASS — only `sk_test_fake` / `whsec_test` placeholders in `tests/test_billing*.py`. No live keys. |
| 7 | Stripe webhook signature verified before dispatch | PASS — `app/platform/routers/billing.py:728` calls `verify_webhook(...)` and only continues on success; `dispatch_webhook` invocation at `:750` is guarded by the verify outcome. |
| 8 | File-upload MIME allow-list | PASS — `app/services/attachment_service.py:26` set unchanged: `application/pdf`, `image/png`, `image/jpeg`, `image/webp`, CAD types, `application/octet-stream` (pre-existing fallback for DWG/DXF — known weakening, not new). No `text/html` / `application/x-msdownload`. |
| 9 | GDPR endpoints reachable + tested | PASS for staff (`/profile/export`, `/profile/delete` live at `app/routers/tenant_admin.py:595/617`). Contact-side coverage gap is **F-BE-004** (deferred manual, unchanged). |
| 10 | Email-verification gate after password check | PASS — `authenticate()` still calls `_has_unverified_identity` post-password (verified by `0eb0a56` regression tests). |
| 11 | HTTPS-only cookies in production | PASS — every `set_cookie` call passes `secure=settings.is_production` (or `secure=secure` from a parameter ultimately driven by it). Verified at `session.py:146`, `platform/session.py:76`, `public.py:373`, `csrf.py:88` (Secure attribute conditional on `scheme_is_https`). |
| 12 | Live HTTPS headers (HSTS, CSP, X-Frame-Options, no Server leak, X-Content-Type-Options) | PASS — `curl -sI https://assoluto.eu/` returns: `strict-transport-security: max-age=31536000; includeSubDomains`, full CSP with `frame-ancestors 'none'` + `form-action 'self' https://*.assoluto.eu ...`, `x-frame-options: DENY`, `x-content-type-options: nosniff`, `referrer-policy: same-origin`, `permissions-policy: geolocation=(), microphone=(), camera=()`. No `Server` or `Via` header. CSRF cookie has `Secure` and `SameSite=lax`. `/healthz` returns 200 with the same headers, no leakage. |

## New surface-area review (per-commit notes)

### `29994d0` — `_safe_error_summary` regex hardening
See **Resolved** section above. Verified.

### `db45cf5` — `HeadMethodMiddleware`
Walked through `app/security/head_method.py` and `app/main.py` lines 313-319.

(a) **Authenticated headers leak on HEAD?** The middleware forwards the `http.response.start` message verbatim, which includes any `Set-Cookie` headers the GET handler attached. Most authenticated GETs do not write cookies on every request — the session cookie is only re-issued on `/auth/login` (POST), `/platform/login` (POST), `/platform/complete-switch` (GET, but only after one-shot token verification). The `csrftoken` is set on first response by `CsrfCookieMiddleware` regardless of method, which is acceptable (CSRF tokens are not secrets and the cookie is per-session). Result: **no new authenticated-header leak**.

(b) **CSP attached on HEAD responses?** YES — `SecurityHeadersMiddleware` sits OUTSIDE `HeadMethodMiddleware` (added later in `create_app`, so it wraps it on the response side). The `http.response.start` from the inner GET handler bubbles up through SecurityHeadersMiddleware which appends the CSP header. Confirmed via the live `curl -sI` probe above — CSP is present.

(c) **State-mutating GETs invokable via HEAD?** Yes — `/platform/verify-email` and `/platform/complete-switch` are GETs that consume one-shot tokens and commit to DB. **This is pre-existing**: an email-scanner GETting these URLs already triggers the same mutation; HEAD is just another way to get the same effect. Not a regression introduced by the middleware. The single-use token guarantee remains the safety boundary. The only behavioural change introduced by this middleware that I could find is the dead `request.method == "HEAD"` guard at `public.py:363`, filed as **F-SEC-002** above.

### `0782f01` — Contact form honeypot
Walked through `app/routers/www.py:92-126`.

(a) **Bot payload stored / logged?** No — the honeypot path logs only `length=len(website)` (an integer); the body is never persisted, never emailed, never appears in audit. Safe.

(b) **Rate-limit fires before honeypot?** YES — `@rate_limit("5/15 minutes")` is the outermost decorator and runs before the function body. A bot tripping the honeypot still consumes a rate-limit token, so the honeypot can't be used as a side-channel to learn the rate-limit threshold. Confirmed.

(c) **Honeypot value reflected to template?** No — the honeypot path renders `submitted=True` with no `form` context, so the bot's payload is not echoed back. No XSS vector.

### `176c4bf` — billing-details audit row
Walked through `app/platform/routers/billing.py:422-517`.

(a) **Tenant binding correct?** YES — `_resolve_current_tenant` returns `(tenant, user_target)` filtered by membership of the verified identity, with role check `target.role != UserRole.TENANT_ADMIN` skipping non-admin memberships. The audit row passes both `entity_id=tenant.id` and `tenant_id=tenant.id` from that resolved tenant — no cross-tenant leak.

(b) **Diff payload exposes old IČO**: confirmed informational only — the `before_subset` dict contains the old billing identifiers. The audit log table is RLS-scoped to the same tenant (`tenant_id=tenant.id`), so only that tenant's admins (and platform support sessions opt-in via `support-access`) can read it. No exposure outside the trust boundary.

(c) **Resubmit no-op skipped**: confirmed — the `if before_subset != after_subset:` guard avoids spamming the audit table on pure form-resubmits. Good hygiene.

### `de17b9a` — robots.txt drops `/platform/signup`
Walked through `app/templates/platform/signup.html` and `app/platform/routers/signup.py:59-83`.

The signup page now-crawlable contains: form fields (company name, owner email, full name, password, ToS checkbox, hidden honeypot), marketing copy, links to /terms and /privacy. Nothing tenant-specific, nothing authenticated, nothing that requires a CSRF guard outside the form itself. The page is intentionally indexable — disallowing it was the regression. No info leak from removing the disallow line.

## Don'ts compliance

- No exploit / brute-force probes against prod. Only single-shot HEAD probes to `/` and `/healthz`.
- No file edits outside `docs/audit-runs/2026-05-09-0931/`.
- No commits made.


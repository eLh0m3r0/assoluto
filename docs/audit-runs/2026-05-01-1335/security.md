# Security audit — 2026-05-01-1335

Scope: regression detection + new-surface coverage for commit `884508b`
(verify gate, honeypot, billing editor, status-page URL, gpg backups).
This is the first run of the new `/audit` pipeline; no previous run
to diff against.

## Built-in /security-review output

The bundled `/security-review` skill is diff-focused. The repo is on
`main` with a clean working tree (no PR diff to scrutinise), so
Phase 1 produced no findings. All real coverage in this report
comes from Phase 2 invariant checks against the merged surface.

## Phase 2 — project-specific invariants

### Pass/fail summary

| Invariant | Status |
|---|---|
| RLS on every tenant model | PASS — every model in `app/models/` (User, Customer, CustomerContact, Order, OrderItem, OrderStatusHistory, OrderComment, Product, Asset, AssetMovement, OrderAttachment, AuditEvent) inherits `TenantMixin`; migrations 0003/0004/0005/0006/0007/0009 enable RLS + `tenant_isolation` policy. Platform tables (`platform_*`) carry `tenant_id` columns but are intentionally accessed via the platform-owner DSN behind `require_platform_admin` — not RLS-scoped, by design. |
| CSRF coverage on all routers | PASS — every router declares `dependencies=[Depends(verify_csrf)]` except `app/routers/health.py` (read-only healthz, no state mutation), `app/platform/routers/platform_admin.py` (declared on the `APIRouter(...)` constructor, line 54), and the Stripe webhook (which is mounted on the bare `router` and uses signature verification instead — the documented exception). |
| Session cookie tenant binding | PASS — public routes use `read_session_for_tenant` + `cookie_mismatches_tenant` (see `public.py:158, 187, 205`). The bare `read_session` call in `deps.py:263` is immediately followed by `tenant_id != str(tenant.id)` rejection on line 268, so it's safe. |
| Single-use recovery tokens | PASS — `tests/test_token_replay.py` covers all three flows (contact invite at line 79, staff invite at line 101, password reset at line 120, plus session-version bump regression at line 171). |
| Rate limits on email-sending POSTs | PASS — `/auth/login` (20/15min), `/auth/password-reset` (5/15min), `/invite/accept` (10/15min), `/invite/staff` (10/15min), `/auth/password-reset/confirm` (10/15min), `/platform/signup` (10/15min), `/platform/login` (20/15min), `/platform/password-reset` (5/15min), `/platform/check-email` (5/15min), `/platform/verify-resend` (3/5min). The signup also has a per-email throttle at the service layer. |
| Secret leakage scan | PASS — only `sk_test_fake` placeholders in `tests/test_billing.py` and `tests/test_stripe_webhooks.py`, and pattern documentation in `docs/`. No live secrets in source. |
| Stripe webhook signature | PASS — `app/platform/routers/billing.py:549` calls `verify_webhook` and raises 400 on `BillingError`; `dispatch_webhook` is only called inside the success branch. The webhook is intentionally not under `csrf_router`. |
| File-upload MIME allow-list | PASS — `attachment_service.py:26` lists pdf/png/jpeg/webp + CAD types and `application/octet-stream` for DWG/DXF. No `text/html`, `application/x-msdownload`, `image/svg+xml` (which can carry script). The `_detect_kind` helper does not bypass the allow-list. |
| GDPR endpoints reachable | PARTIAL — endpoints exist (`tenant_admin.py:595, 617`) and `me.py` companion routes for non-staff. **No tests exercise them** (see F-SEC-002). |
| Email-verification gate | PASS — `auth_service.py:136` calls `_has_unverified_identity` after the password check. Documented fail-open is acceptable per the threat model. |
| HTTPS-only cookies in production | PASS — every `set_cookie` in `public.py:119, 355`, `signup.py:263`, `platform_auth.py:103, 456, 595` uses `secure=settings.is_production`. The session/platform-session writers also take `secure=` as a parameter. |
| Live config probe | PASS WITH MINOR NIT — `curl -sI https://assoluto.eu/` returns `Strict-Transport-Security: max-age=31536000; includeSubDomains`, full CSP, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Permissions-Policy` set. Two minor info-leak headers (`server: uvicorn`, `via: 1.1 Caddy`) — see F-SEC-003. |
| Honeypot field invisible | PASS — `templates/platform/signup.html:26` uses `position:absolute;left:-10000px` (safer than `display:none`), `aria-hidden="true"`, `tabindex="-1"`, `autocomplete="off"`. Keyboard nav and screen readers will not reach it. Naive bots that fill every input will trip it. |
| Subscription editor admin-only | PASS — every route in `platform_admin.py` carries `Depends(require_platform_admin)` (lines 67, 125, 178, 287, 335, 350, 375, 413, 465, 634, 682). The editor refuses Stripe-managed subs via the explicit check at line 511. `pin_internal` and `set_active` are inside the same gated handler. |
| Email enumeration via /platform/check-email | PASS — `signup.py:476` always renders the same success template regardless of whether an email actually got sent. The template (`check_email.html`) does not branch on `_actually_sent`. The echoed email is only what the visitor typed, so no information is disclosed. |
| Backup runbook does not import secret key on prod | PASS — `docs/BACKUP_RESTORE.md:115` explicitly says "you need the secret key — never import it on the production server". The decrypt step happens off-VPS. |
| `STATUS_PAGE_URL` open-redirect / injection | PASS — interpolated as `href="{{ status_page_url }}"` with Jinja2 auto-escaping into an HTML attribute. The value is operator-controlled (env var, trusted per CLAUDE.md), not user-controlled. |

### Findings

### F-SEC-001 — `Server: uvicorn` and `Via: 1.1 Caddy` headers leak internals

- **Where**: production reverse proxy (Caddy → uvicorn), surfaces on every response from `https://assoluto.eu/`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Both response headers are returned on every request, including from logged-out callers. `Server: uvicorn` tells an attacker the app is FastAPI/Starlette without their having to fingerprint behaviour. `Via: 1.1 Caddy` tells them the reverse-proxy software. Neither is exploitable on its own — but standard hardening hides them so vuln scanners can't trivially auto-target known CVEs in the stack. Removing both is a one-line Caddy config change.
- **Suggested fix**: In the Caddyfile that fronts the app, add `header -Server` and `header -Via` to the site block (or `header_down` in a reverse-proxy directive). uvicorn itself can also be launched with `--no-server-header` for defence-in-depth.
- **Evidence**: `curl -sI https://assoluto.eu/` returns `server: uvicorn` and `via: 1.1 Caddy` (verified live during the audit).

### F-SEC-002 — GDPR export/erase endpoints have zero test coverage

- **Where**: `app/routers/tenant_admin.py:595` (`/app/admin/profile/export`), `app/routers/tenant_admin.py:617` (`/app/admin/profile/delete`), plus the `/app/me/profile/...` companion routes in `app/routers/me.py`
- **Severity**: P1
- **Auto-fixable**: no (requires writing tests, not auto-mechanical)
- **Description**: Both GDPR Art. 17 (erasure) and Art. 20 (portability) endpoints exist and are functional, but `grep -rln "profile/export|profile/delete|gdpr_service" tests/` returns no results. These are **the** legally-mandated endpoints — if a refactor silently breaks the export JSON shape, the password-confirm gate, or the "last admin can't delete themselves" guard at `tenant_admin.py:650`, nothing will catch it before a regulator does. The CLAUDE.md audit checklist explicitly calls out "GDPR endpoints reachable + tested" — this is the only invariant that fails today.
- **Suggested fix**: Add `tests/test_gdpr.py` covering: (a) export returns 200 + JSON download for the authed user, (b) export does NOT include other tenants' data (RLS smoke), (c) delete with wrong password redirects with `?error=`, (d) delete with right password anonymises the row and clears the session, (e) the last-admin guard blocks self-erasure when the operator is the only `TENANT_ADMIN`. Estimated 30 mins of test code.
- **Evidence**: `grep -rln "profile/export\|profile/delete\|gdpr_service" tests/` → empty.

### F-SEC-003 — `_has_unverified_identity` opens a fresh engine per failed login

- **Where**: `app/services/auth_service.py:85` (`create_async_engine(settings.database_owner_url, future=True)` inside the function body)
- **Severity**: P2
- **Auto-fixable**: no (refactor — needs lifespan-managed singleton)
- **Description**: Every wrong-password login attempt against an account whose Identity exists triggers a brand-new `create_async_engine` + connect + dispose cycle against the platform-owner DSN. asyncpg engines are cheap-ish but not free; under a credential-stuffing burst this creates a fan-out of owner-DSN connections that the connection-pool ceiling and `pg_hba.conf` are not sized for. Worse, the `database_owner_url` is the **portal owner** role (RLS bypass) — keeping it touched on the hot login path means a bug here could expose owner-level connection state to the tenant request scope. The function correctly fails open on exception, so this is not a confidentiality bug today, but the architecture is fragile. Consider hoisting a single owner engine into app state at lifespan startup and reusing it.
- **Suggested fix**: Create the platform-lookup engine once in `app.main.lifespan` (or in `app/platform/__init__.py:install`), stash on `app.state.platform_lookup_engine`, and have `_has_unverified_identity(request, email)` pull from there. Disposal moves to lifespan shutdown.
- **Evidence**: `auth_service.py:85` — `engine = create_async_engine(...)` then `engine.dispose()` per call.

## Summary

Strong baseline. Headers, CSRF, RLS, rate limits, single-use tokens,
honeypot, and webhook signing are all in shape. One real gap (no GDPR
test coverage), one architectural papercut (per-call engine creation),
one cosmetic header leak.

security: 0 P0, 1 P1, 2 P2

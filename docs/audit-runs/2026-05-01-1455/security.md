# Security audit — 2026-05-01-1455 (verification run)

Scope: regression detection + verification that the fixes shipped
between previous-run HEAD `a9d64e0` and current HEAD `efd4890` actually
landed in production. Previous run:
[`docs/audit-runs/2026-05-01-1335/security.md`](../2026-05-01-1335/security.md).

Commits since the last audit (security-relevant):

* `cb53240` strip `Server` / `Via` headers from prod responses
  (F-SEC-001)
* `f87ae07` /terms 500 fix — `99.9%` → `99.9%%` msgid escape
* `8707f21` hreflang alternates added to www_base.html
* `82d5458` i18n CS / DE copy edits
* `f4743e8`, `efd4890` audit-run status notes (docs only)

## Built-in /security-review output

The bundled `/security-review` skill is diff-focused. The branch is on
`main` with a clean working tree (the prior fixes were already merged
+ pushed before this audit started), so there is no unmerged PR diff
for the skill to scrutinise. All real coverage in this report comes
from re-running Phase 2 invariant checks against `efd4890` plus a
live header probe to confirm the deploy.

## Phase 2 — project-specific invariants

### Pass/fail summary

| Invariant | Status |
|---|---|
| RLS on every tenant model | PASS — unchanged since 1335 run; no new tenant-scoped models added in the diff. |
| CSRF coverage on all routers | PASS — every `APIRouter(...)` declaration in `app/routers/*.py` and `app/platform/routers/*.py` carries `dependencies=[Depends(verify_csrf)]` except `health.py` (read-only `/healthz`, no state mutation), `platform_admin.py` (same dep declared on the multi-line constructor), and the Stripe webhook `router` in `billing.py` (signature-verified instead — documented exception). |
| Session cookie tenant binding | PASS — unchanged; `read_session_for_tenant` + `cookie_mismatches_tenant` still wired in `public.py`. |
| Single-use recovery tokens | PASS — `tests/test_token_replay.py` still present with 4 test functions covering contact-invite / staff-invite / password-reset / session-version bump. |
| Rate limits on email-sending POSTs | PASS — `/auth/login` (20/15min), `/auth/password-reset` (5/15min), `/auth/password-reset/confirm` (10/15min), `/invite/accept` (10/15min), `/invite/staff` (10/15min), `/contact` (5/15min), `/platform/signup` (10/15min), `/platform/login` (20/15min), `/platform/password-reset` (5/15min), `/platform/check-email` (5/15min), `/platform/verify-resend` (3/5min). |
| Secret leakage scan | PASS — only `sk_live_…` / `sk_live_xxx` placeholder strings in `docs/{ENV,OPERATOR_PLAYBOOK,DEPLOY_SAAS,DEPLOY_HETZNER,AUDIT_PIPELINE}.md`. No live secret bytes in source. |
| Stripe webhook signature | PASS — `app/platform/routers/billing.py:549` calls `verify_webhook(settings, payload, sig)` and the `dispatch_webhook` call lives only inside the verified branch. |
| File-upload MIME allow-list | PASS — `attachment_service.py:26-37` unchanged, still pdf/png/jpeg/webp + CAD types + `application/octet-stream` fallback. No `text/html`, `image/svg+xml`, or `application/x-msdownload`. |
| GDPR endpoints reachable | PARTIAL — endpoints still exist; **F-SEC-002 persists** (no test coverage, expected). |
| Email-verification gate | PASS — `auth_service.py:136` still calls `_has_unverified_identity` after the password check; raises `UnverifiedIdentity`. |
| HTTPS-only cookies in production | PASS — three `set_cookie` call sites total (`public.py:349` lang cookie, `security/session.py:140` session cookie, `platform/session.py:70` platform-session cookie). All take `secure=settings.is_production` (or pass `secure=` from the caller). |
| Live config probe — landing page | PASS — `curl -sI https://assoluto.eu/` returns HSTS / CSP / X-Frame-Options DENY / X-Content-Type-Options nosniff / Referrer-Policy / Permissions-Policy. **`Server` and `Via` headers are GONE** (F-SEC-001 fix verified live). |
| Live config probe — /healthz | PASS — same security headers, no leaky `server` / `via` / `x-powered-by`. |
| /terms %% fix | PASS — `curl -s https://assoluto.eu/terms` returns 200 in the default locale and EN; rendered text contains literal `99.9%` (not `99.9%%`), so Jinja's `% v` substitution collapses it correctly. CS msgstr renders `99,9 %`. |
| hreflang `_path` interpolation safe | PASS — `_path = request.url._url.split('?')[0]` is interpolated through Jinja auto-escape into `href` attributes; uvicorn rejects requests with arbitrary `Host:` headers (Caddy gates on the configured site). Live probe with a forged `Host:` header returned an empty body. |
| New i18n msgstrs free of XSS payloads | PASS — 79 new `msgstr` lines added across CS/DE; `grep -iE '<script\|<iframe\|javascript:\|<img\|onerror\|onclick\|<svg'` returns no matches. All Jinja sites that emit msgstr output use auto-escape anyway. |

### Findings

### F-SEC-002 — GDPR export/erase endpoints have zero test coverage *(persisted from 1335)*

- **Where**: `app/routers/tenant_admin.py:595` (`/app/admin/profile/export`), `app/routers/tenant_admin.py:617` (`/app/admin/profile/delete`), and the `/app/me/profile/...` companion routes in `app/routers/me.py`
- **Severity**: P1
- **Auto-fixable**: no (requires writing tests)
- **Description**: Endpoints still exist and function, but `grep -rln "profile/export\|profile/delete\|gdpr_service" tests/` returns no results — same state as the previous run. These are the legally-mandated GDPR Art. 17 / Art. 20 routes; a silent regression in the password-confirm gate, the export JSON shape, or the "last admin can't delete themselves" guard at `tenant_admin.py:650` would not be caught before a regulator notices. CLAUDE.md §12 ("audit pipeline") explicitly lists "GDPR endpoints reachable + tested" as an invariant.
- **Suggested fix**: Add `tests/test_gdpr.py` with five cases: (a) export returns 200 + JSON for the authed user; (b) export does not include other tenants' rows (RLS smoke); (c) delete with wrong password redirects with `?error=`; (d) delete with right password anonymises the row and clears the session; (e) the last-admin guard blocks self-erasure when the operator is the only `TENANT_ADMIN`. ~30 minutes of test code. Paired with F-BE-003 in the backend report.
- **Evidence**: `grep -rln "profile/export\|profile/delete\|gdpr_service" tests/` → empty.

### F-SEC-003 — `_has_unverified_identity` opens a fresh engine per failed login *(persisted from 1335)*

- **Where**: `app/services/auth_service.py:85` (`engine = create_async_engine(settings.database_owner_url, future=True)` inside the function body)
- **Severity**: P2
- **Auto-fixable**: no (refactor — needs a lifespan-managed singleton)
- **Description**: Unchanged since 1335. Every wrong-password login attempt against an account whose Identity exists triggers a fresh `create_async_engine` + connect + dispose cycle against the platform-owner DSN. The `database_owner_url` is the **portal owner** role (RLS bypass) — keeping it touched on the hot login path makes the architecture fragile under credential-stuffing bursts. Not exploitable today (the function fails open on exception so confidentiality is preserved), but worth fixing before scale.
- **Suggested fix**: Stand up a single owner-DSN engine in `app.main.lifespan` (or `app/platform/__init__.py:install`), stash on `app.state.platform_lookup_engine`, have `_has_unverified_identity(request, email)` pull from there. Disposal moves to lifespan shutdown.
- **Evidence**: `auth_service.py:85` — `engine = create_async_engine(...)` then `engine.dispose()` per call.

### Diff vs previous run

| Finding | 1335 status | 1455 status |
|---|---|---|
| F-SEC-001 (Server/Via headers leaked) | open P2 | **resolved** — verified absent on `https://assoluto.eu/` and `https://assoluto.eu/healthz` after the manual Caddy rebuild on prod |
| F-SEC-002 (no GDPR test coverage) | open P1 | **persisted** — expected, paired with F-BE-003 |
| F-SEC-003 (per-call engine for verify lookup) | open P2 | **persisted** — expected, refactor not yet scheduled |
| (none) | — | **no new findings** |

## Summary

F-SEC-001 confirmed resolved at the edge. F-SEC-002 and F-SEC-003 carry
forward as expected — both flagged manual / deferred in the run brief.
The new diff (i18n copy, hreflang, `%%` escape, header strip) introduced
no security regressions. Header probe shows clean prod surface.

security: 0 P0, 1 P1, 1 P2 (unchanged net count vs 1335 minus the
resolved P2)

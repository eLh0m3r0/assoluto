# Security audit — 2026-05-09-0829

Scope: 7 head commits (`bf05aff..d0a5e35`), with extra focus on the
five surfaces called out by the founder (verify-email gate, IČO/DIČ
form, email diagnostics redaction, staff item-edit expansion,
S3 boot-time check). Project-specific invariants (RLS, CSRF,
session-cookie tenant binding, single-use recovery tokens, rate
limits, secret leakage, Stripe webhook signature, file-upload MIME
allow-list, GDPR endpoints, HTTPS-only cookies, live header probe)
were re-checked at the level of the previous audits.

## Built-in /security-review output

The bundled `/security-review` skill ran with an empty diff payload
(its `git diff` invocation returned no output because `production`
is not the active comparison base in this checkout). Findings below
came from the manual phase 2 sweep.

No high-confidence vulnerabilities were identified by the skill.

## Phase 2 — project-specific invariants

| Invariant | Status |
|---|---|
| RLS isolation: every tenant-scoped model has `TenantMixin` | PASS — only `tenant.py` lacks it (correct, it IS the tenant) |
| CSRF coverage on every router | PASS — `health.py` is the only router without `verify_csrf` (no mutations); `billing.py` splits GET from `csrf_router` POSTs; Stripe webhook is the documented exception |
| Session-cookie tenant binding (`read_session_for_tenant`) | PASS — no bare `read_session(` in public/platform routers |
| Single-use recovery tokens | PASS — `tests/test_token_replay.py` still has 4 tests (contact invite, staff invite, reset replay, reset invalidated by bump) |
| Rate limit coverage on email-sending POSTs | PASS — `/auth/login`, `/auth/password-reset`, `/invite/accept`, `/invite/staff`, `/platform/signup`, `/platform/login`, `/platform/password-reset`, `/platform/check-email`, `/platform/verify-resend` all decorated |
| Secret leakage scan | PASS — only `sk_test_` / `whsec_test` placeholders in test files; no live keys in repo |
| Stripe webhook signature path | PASS — `verify_webhook(settings, payload, sig)` runs **before** `dispatch_webhook(...)` inside the same transaction (`app/platform/routers/billing.py:703-744`); only entry point |
| File-upload MIME allow-list | PASS — `ALLOWED_CONTENT_TYPES` unchanged: PDF, PNG/JPEG/WebP, CAD blob types, octet-stream fallback. No `text/html`, no `image/svg+xml`, no executables |
| GDPR endpoints reachable + tested | PASS — `/app/admin/profile/export` + `/app/admin/profile/delete` exist (`tenant_admin.py:595/617`); `/app/me/profile` exists (`me.py`); `tests/test_orders_export.py` covers the happy path |
| Email-verification gate after auth | PASS — promoted onto `select_tenant`, `switch_to_tenant`, `complete_switch` in `bfd1690`. `post_verify_checkout` and `billing_details_*` also use `require_verified_identity`. No remaining handoff path takes plain `require_identity` to reach a tenant. |
| HTTPS-only cookies in prod | PASS — every `set_cookie` (`session.py:140`, `platform/session.py:70`, `public.py:367`) passes `secure=settings.is_production` |
| Live header probe (assoluto.eu, healthz) | PASS — STS, CSP w/ `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, no `Server`/`Via` leak. `/healthz` 200 with the same hardened headers |

## Findings

### F-SEC-001 — `_safe_error_summary` regex misses bare token shapes
- **Where**: `app/tasks/email_tasks.py:60-77`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The new redaction (commit `0c7d468`) handles two
  specific token shapes — full URLs (`https?://\S+`) and `=value`
  query-param suffixes (`=([A-Za-z0-9_\-]{12,})`). It does **not**
  catch:
    1. **Bare itsdangerous tokens** outside a URL/query context, e.g.
       an SMTP server quoting back a header line like
       `X-Reset-Token: eyJhbGciOi.payload.signature` or a body
       fragment like `Token is eyJhbGc... please copy`. The dot-
       separated payload also breaks the contiguous-char regex —
       only individual segments would partially trigger if
       prefixed with `=`.
    2. **Long hex/base64 secrets without a leading `=`**, e.g. a
       traceback that prints `"failed token=ey..."` versus
       `"failed: ey..."`.
    3. **Multi-part bodies** if an SMTP library ever echoes a
       MIME boundary line containing an embedded token.
  Realistic risk is bounded — SMTP libraries generally echo only
  the SMTP response code+text (e.g. `554 5.7.1 message rejected`),
  not body content. But this is the redaction layer the comment
  promises ("must never leak"), so the bar should match the
  comment. Defense-in-depth fix.
- **Suggested fix**: Add a third pattern that strips any
  contiguous run of ≥20 base64-url-safe characters regardless of
  context (`re.sub(r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}", "[jwt]", cleaned)`
  for JWT-shape, plus
  `re.sub(r"\b[A-Fa-f0-9]{32,}\b", "[hex]", cleaned)` for raw
  hex). Keep both for layered safety. Then truncate.
- **Evidence**:
  ```python
  cleaned = re.sub(r"https?://\S+", "[url]", raw)
  cleaned = re.sub(r"=([A-Za-z0-9_\-]{12,})", "=[redacted]", cleaned)
  return cleaned[:160]
  ```

### F-SEC-002 — `s3.public_endpoint_check_failed` log embeds bucket + endpoint but not credentials (informational)
- **Where**: `app/storage/s3.py:81-126`
- **Severity**: informational (no action required)
- **Auto-fixable**: n/a
- **Description**: The founder asked to confirm that the new
  boot-time S3 probe doesn't leak credentials in its warning log.
  Confirmed clean — only `endpoint` (URL with no auth), `bucket`
  (name only), and either `error_code` (e.g. `NoSuchBucket`) or
  `error_class` (Python exception class name) are logged. No
  access-key / secret-key reference; boto3's `ClientError.response`
  also does not carry credentials.
- **Suggested fix**: none.
- **Evidence**:
  ```python
  log.warning(
      "s3.public_endpoint_check_failed",
      endpoint=public_endpoint,
      bucket=settings.s3_bucket,
      error_code=code,
      hint="...",
  )
  ```

### F-SEC-003 — Staff item-edit expansion (informational)
- **Where**: `app/services/order_service.py:446-542`
- **Severity**: informational (no action required)
- **Auto-fixable**: n/a
- **Description**: Founder asked to confirm there's no escalation
  path. Verified:
    1. `update_item` / `add_item` / `remove_item` all run the
       `_ensure_item_editable(order, actor)` gate when an actor is
       supplied.
    2. The "actor=None" branch (background-task path) clamps to
       the same `STAFF_ITEM_EDIT_STATES` set, so it cannot be
       weaker than the staff-actor path.
    3. The router (`app/routers/orders.py:888-897`) always passes
       `actor=_actor(principal)` and `audit_actor=
       actor_from_principal(principal)`. There is no router code
       path that reaches `update_item` without supplying both.
    4. Audit trail: `order.item_updated` is recorded in every
       branch (`order_service.py:505-515`) with the resolved
       `actor` (falling back to `SYSTEM_ACTOR` only for the
       no-actor background-task case). No audit-trail gap.
    5. Contact-vs-staff scope check: `actor.type == "contact"`
       branch keeps the DRAFT-only restriction *and* the
       per-customer ID check. Staff cannot impersonate a contact
       (different actor type), so the new looser staff window
       cannot be reached from a contact session.
- **Suggested fix**: none.
- **Evidence**: see commit `17c1528` diff and `app/routers/orders.py:888-897`.

### F-SEC-004 — Verify-email gate now covers all platform handoff paths (informational)
- **Where**: `app/platform/routers/platform_auth.py:296,359,471` + `app/platform/routers/billing.py` (post_verify_checkout)
- **Severity**: informational (no action required)
- **Auto-fixable**: n/a
- **Description**: Founder asked to confirm there are no remaining
  handoff paths that let an unverified Identity reach a tenant.
  Walked every `Identity = Depends(...)` callsite under
  `app/platform/routers/`:
    - `select_tenant`, `switch_to_tenant`, `complete_switch` — all
      `require_verified_identity` (commit `bfd1690`).
    - `billing.py` — every Identity dep is `require_verified_identity`
      (already was; confirmed unchanged).
    - `signup.py` — verify-flow routes use `get_current_identity` /
      `require_identity` deliberately (the user IS still
      unverified at this stage).
    - `platform_admin.py` — its own `require_platform_admin`
      dependency, separate from this gate.
  No regression; no remaining path lets an unverified Identity
  reach `/app`.
- **Suggested fix**: none.
- **Evidence**: see `bfd1690` diff and `billing.py:152,209,310,389,430,502,635`.

### F-SEC-005 — IČO/DIČ form: input validation + `?next=` open-redirect coverage (informational)
- **Where**: `app/platform/routers/billing.py:383-492`
- **Severity**: informational (no action required)
- **Auto-fixable**: n/a
- **Description**: Founder asked for verification on the new
  `/platform/billing/details` form. Verified:
    1. **CSRF**: registered on `csrf_router` (line 422), which
       carries `dependencies=[Depends(verify_csrf)]`. POST is
       protected.
    2. **Authorisation**: `_resolve_current_tenant` filters
       memberships to `UserRole.TENANT_ADMIN` only — tenant_staff
       and customer contacts cannot reach the form even with a
       valid Identity cookie (`billing.py:75-88`).
    3. **Input validation**:
       - `billing_name` non-empty (after strip).
       - `billing_ico` exactly 8 digits (`isdigit()` + length).
       - `billing_dic` optional; if present, must be `CZ` prefix +
         8–10 trailing digits.
       - `billing_address` non-empty.
    4. **`?next=` open redirect**: `_safe_next_path` is invoked on
       line 485, returns `"/"` for non-same-origin candidates, and
       the post-save redirect short-circuits `"/"` to
       `/platform/billing` so the user always lands somewhere
       sensible. The hidden form input echo (`<input ... value="{{ next }}">`
       in the template) is Jinja-autoescaped; no XSS surface.
    5. **Stored-XSS surface**: form values land in `tenant.settings`
       JSONB and are later rendered (a) on the same form
       (autoescaped), (b) in the invoice PDF (text-only context).
       Self-tenant only — there is no cross-tenant view of these
       fields.
- **Suggested fix**: none.
- **Evidence**: see commit `17a662d` and `_safe_next_path` in
  `app/routers/public.py:62-94`.

## Regression check vs 2026-05-01-1455 audit

The previous audit logged 0 P0 / 0 P1 / 1 P2 (the SMTP-error
class-only log message — which **2026-05-09-0829's commit `0c7d468`
addressed**, although F-SEC-001 above notes the redaction can be
strengthened). All other invariants from the previous run still
hold. No regressions.

## Summary

* P0: 0
* P1: 0
* P2: 1 (`F-SEC-001`)
* informational: 4 (`F-SEC-002` through `F-SEC-005`)

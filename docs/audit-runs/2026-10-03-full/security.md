# Security audit — 2026-10-03-full

Auditor: `security-auditor` · Commit: `015496f` (main == production) · Prefix `SEC-`
Scope: regression detection against 2026-07-26-e2e, the new notification surface (PR #7),
public-repo secret history, project invariants, and a live header probe (single HEAD/GET
requests only; no mutating POSTs against production).

## Summary

| ID | Sev | Status | Title |
|---|---|---|---|
| SEC-1 | P1 | new | Stripe webhook accepts payloads signed with an empty secret when `STRIPE_WEBHOOK_SECRET` is unset |
| SEC-2 | P1 | persisted (F-12) | Platform `Identity` still has no GDPR export or erasure route |
| SEC-3 | P1 | persisted (F-10, F-41) | Attachment upload blocks the single event loop and reads the full body before the size check |
| SEC-4 | P2 | persisted (F-25), narrowed | `privacy.html` now promises encrypted B2 backups; the script and playbook still ship them unencrypted by default |
| SEC-5 | P2 | persisted (F-27) | Art. 15/20 exports are still incomplete for both principal types |
| SEC-6 | P3 | new | Platform logout does not bump `Identity.session_version` (F-40 was fixed on the tenant side only) |
| SEC-7 | P3 | new | Production honours a client-supplied `X-Tenant-Slug` header (confirmed live) |
| SEC-8 | P3 | new | Order emails (comment excerpts, file names) reach invited-but-never-accepted addresses |
| SEC-9 | P3 | new | No test pins that one customer's contacts never get mail about another customer's order |
| SEC-10 | P3 | persisted (F-28) | A contact can erase themselves and the controller (the tenant) is not told |
| SEC-11 | P3 | persisted (F-38) | The manual subscription editor still has no tests and still 500s on a malformed `quick_action` |
| SEC-12 | P3 | persisted (F-43) | The cookies policy still doesn't match the cookies the app sets |

Counts: **P0 0 · P1 3 · P2 2 · P3 7**.

## Built-in /security-review output

`Skill(security-review)` ran against the working tree. `main` is equal to `origin/main`, so the
diff was **empty**: no files modified, no commits ahead. So that the phase still meant
something, I applied the skill's method and its false-positive rules (confidence ≥ 8 only,
no DoS, no rate-limit, no hardening-only items) by hand to the most recent shipped change,
PR #7 (`git diff 015496f^1 015496f`: notification redesign, 45 files). No sub-task tool was
available in this session, so the find and filter passes were done inline.

> **Result: no vulnerabilities at or above confidence 8 in PR #7.**
>
> Checked and found clean:
> - **Cross-customer audience.** `resolve_contact_audience` filters on
>   `CustomerContact.customer_id == order.customer_id` (`app/services/notification_service.py:373`).
>   The query runs inside the RLS-scoped request session, so tenant isolation also holds.
> - **Internal comments.** `orders_add_comment` skips the builder when `internal_flag` is set.
>   `add_comment` refuses `is_internal` from a contact (`app/services/order_service.py:867`).
> - **Header injection.** Every subject template interpolates only the server-generated
>   `order_number` or a count (`app/email/templates/*.subject.txt`). User text (title,
>   comment, filename) only goes into bodies, which use an autoescaping env
>   (`app/email/sender.py:47`, `select_autoescape(["html"])`).
> - **Preference tampering.** `parse_form` keeps only events in the side's allow-list
>   (`app/services/notification_prefs.py`, `parse_form`), so a contact cannot subscribe to
>   `order_assigned`. Self-edit routes key on `principal.id`. Editing another user's prefs
>   requires `_require_tenant_admin` (`app/routers/tenant_admin.py:330`).
> - **Assignment IDOR.** `POST /app/orders/{id}/assign` is `require_tenant_staff` with
>   `get_order_for_principal`. The assignee UUID resolves under RLS, and the `assigned=`
>   list filter is ignored for contacts (`orders.py` `_parse_assigned_filter`).
> - **Migration 1008.** It only adds a column, an FK and an index to `orders`, which already has
>   a tenant policy, so no new table needs RLS.
>
> Below the confidence 8 threshold, carried into Phase 2 as SEC-8 and SEC-9: email content
> going to pending invitees, and the missing cross-customer regression test.

## Phase 2 — project invariants

| # | Invariant | Result | Evidence |
|---|---|---|---|
| 1 | RLS / TenantMixin | pass | Every `app/models/*.py` class with `tenant_id` inherits `TenantMixin`. Platform and billing models are owner-scoped by design. 1008 creates no table |
| 2 | CSRF on every router | pass | All 15 routers carry `dependencies=[Depends(verify_csrf)]`. `billing.py:38` `router` holds only GETs plus the Stripe webhook (`:701`), the documented exception. `health.py` has no POSTs |
| 3 | `read_session_for_tenant` on public routes | pass | The only bare `read_session` call is `app/deps.py:263`, which checks `tenant_id` at `:268` |
| 4 | Single-use recovery tokens | pass | `tests/test_token_replay.py:79,101,120,171` exist and cover all three flows plus the unrelated-bump case |
| 5 | Rate limits on public email-sending POSTs | pass | login, invite/accept, invite/staff, password-reset (+confirm), `/contact`, platform signup, check-email, verify-resend, platform login, platform password-reset (+confirm) are all decorated |
| 6 | Secret leakage (tree + history) | pass | See "Git history scan" below |
| 7 | Stripe webhook verify before dispatch | pass with a caveat | `billing.py:727` verifies, `:754-769` dedups, then dispatches. `dispatch_webhook` has no other caller. Caveat: **SEC-1** |
| 8 | Upload MIME allow-list | pass | `attachment_service.py:26-37`, unchanged: no `text/html` or executables. `octet-stream` is accepted, but every download is forced to `attachment` via `ResponseContentDisposition` (`app/storage/s3.py:180`) |
| 9 | GDPR endpoints and tests | pass for tenant principals | `me.py:223,251` and `tenant_admin.py:665,730`, tested in `tests/test_gdpr.py`. Platform Identity missing: **SEC-2** |
| 10 | Email-verification gate | pass | `auth_service.py:160` runs after `verify_password` (`:143`), scoped by membership (`:66-84`) |
| 11 | Secure cookies in prod | pass | `write_session` calls (`public.py:119`, `platform_auth.py:487,626`, `billing.py:647`), `write_platform_session` calls (`platform_auth.py:108`, `signup.py:270`) and the locale cookie (`public.py:393`) all pass `secure=settings.is_production`. The CSRF cookie carries `Secure` live |
| 12 | Live headers | pass | `/` and `/healthz` return HSTS `max-age=31536000; includeSubDomains`, a strict CSP with `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy`. No `Server` header. `/healthz` returns 200 with a 15-byte JSON body |

Other areas checked, with no finding:
- **Open redirects.** `_safe_next_path` (`public.py:62-94`) rejects `//`, `\`, `..`, and any
  scheme or netloc after normalisation. The platform and billing callers reuse it.
- **Session fixation.** Sessions are stateless signed cookies, written fresh on login, with a
  `session_version` check on every request.
- **Platform admin support-access invariant (CLAUDE.md §6).** `/platform/switch` and
  `/complete-switch` (`platform_auth.py:381-600`) require an active `TenantMembership`, re-check
  it server-side, use a 60 s handoff token bound to the identity cookie, and write a
  tenant-side audit event for `support` grants. `platform_admin.py` imports only `Tenant` from
  core models, so it has no direct read path to business data.

### Git history scan (public repo)

I searched all 261 commits on every ref:
- `git log --all -p` for `sk_live_`, `sk_test_<20+>`, `rk_live_`, `whsec_<20+>`, `AKIA…`, Brevo
  `xkeysib-` / `xsmtpsib-`, PEM private keys, `ghp_` / `github_pat_` / `glpat-`.
- Every assignment ever made to `SMTP_PASSWORD`, `APP_SECRET_KEY`, `S3_*`, `STRIPE_*` and
  `PORTAL_*_PASSWORD`.
- Every committed filename matching `.env`, `*.pem`, `*.key`, `id_rsa`, `rclone.conf`, `.netrc`.

**No real secrets were found.** The only `.env` file ever committed is `.env.example`. Every
credential assignment is a placeholder (`<…>`, `...`, `xxx`), a `${VAR:?}` assertion, a test
fake (`sk_test_fake`, `whsec_test`), or the CI-only `APP_SECRET_KEY: ci-secret-key`. The
DSNs in `docker-compose.yml`, `.env.example` and `app/config.py` hold dev-only `portal` /
`portal_app` passwords; production overrides them via `${PORTAL_*_PASSWORD:?}` in
`docker-compose.prod.yml:67-69`. Tracked files contain no personal email addresses or public
server IPs, beyond test fixtures and a `you@gmail.com` playbook placeholder.

## Re-verification of 2026-07-26 items

| Prior | Status now | Evidence |
|---|---|---|
| F-04 (billing tenant resolver / support grant) | still fixed | `app/platform/routers/billing.py:92` skips `MEMBERSHIP_ACCESS_SUPPORT`; `service.py:258` sets a deterministic order |
| F-05 (platform reset replay) | still fixed | `platform_auth.py:292` `token_session_version=token_sv` |
| F-06 (signup lock-out via email) | still fixed | `auth_service.py:66-84`: lookup scoped to a membership on this tenant |
| F-18 (cross-customer product) | still fixed | `orders.py:951-954`: `can_use_catalog` enforced, product scoped to `customer_id IS NULL OR = order.customer_id` |
| F-20 (platform session version) | still fixed | `app/platform/deps.py:77` |
| F-23 (login oracle) | still fixed | `auth_service.py:143-145` and `:189-194`: password verified before state |
| F-40 (logout invalidation) | fixed for tenant, **gap on platform** | `public.py:336` bumps; `platform_auth.py:113-119` does not. See SEC-6 |
| F-12 | persisted | SEC-2 |
| F-10, F-41 | persisted | SEC-3 |
| F-25 | persisted, narrowed | SEC-4 |
| F-27 | persisted | SEC-5 |
| F-28 | persisted | SEC-10 |
| F-38 | persisted | SEC-11 |
| F-43 | persisted | SEC-12 |
| F-24 (invoice VAT back-computation) | persisted, latent; billing, not security | `invoice_pdf_service.py:330` still keys only on `supplier_is_vat and currency == "CZK"`. Inert while `PLATFORM_OPERATOR_DIC` is empty (neplátce). Left to logic/codex |

---

## Findings

### SEC-1 — Stripe webhook accepts payloads signed with an empty secret when `STRIPE_WEBHOOK_SECRET` is unset
- Severity: P1 (conditional on config; whether production is affected is `unverified`)
- Status vs 2026-07-26: new
- Evidence:
  - `app/config.py:128`: `stripe_webhook_secret` defaults to `""`.
  - `app/config.py:193-195`: `stripe_enabled` returns `bool(self.stripe_secret_key)` and never looks at the webhook secret.
  - `app/platform/routers/billing.py:721-727`: when `stripe_enabled`, the request goes straight to `verify_webhook`.
  - `app/platform/billing/service.py:474-479` passes `secret=settings.stripe_webhook_secret` to `stripe.Webhook.construct_event`.
  - In the installed stripe 15.0.1, `.venv/.../stripe/_webhook.py:47-49` computes `hmac.new(secret.encode("utf-8"), …)` and never rejects an empty key.
  - No boot guard, compose `:?` assertion or `.env.example` warning covers this variable (grep returns only `config.py:128`, `service.py:477` and `.env.example:60`, which is blank).
- Dopad: If the operator sets `STRIPE_SECRET_KEY` but forgets `STRIPE_WEBHOOK_SECRET` (the
  playbook configures them in separate steps), anyone can compute a valid `Stripe-Signature`
  with HMAC-SHA256 under the key `""`. They can then POST forged `checkout.session.completed` or
  `customer.subscription.*` events and activate a paid plan for their own tenant without paying,
  or forge `deleted` / `canceled` events. This is the money path, and it fails open with no log
  signal.
- Návrh: In `verify_webhook`, raise `BillingError("webhook secret not configured")` when
  `not settings.stripe_webhook_secret`. Also refuse to boot in production when
  `stripe_secret_key` is set and the webhook secret is empty, using the same pattern as the
  F-15 `APP_SECRET_KEY` guard. Optionally add `STRIPE_WEBHOOK_SECRET: ${STRIPE_WEBHOOK_SECRET:?}`
  to the prod overlay once billing goes live. Add a test that a payload signed with `""` is
  rejected.
- Effort: S
- Auto-fixable: yes

### SEC-2 — Platform `Identity` still has no GDPR export or erasure route
- Severity: P1
- Status vs 2026-07-26: persisted (F-12)
- Evidence: `grep -rn "erase_identity\|export_for_identity" app tests scripts` finds nothing
  outside `app/services/gdpr_service.py`. `app/platform/routers/platform_auth.py` routes are
  login, logout, password-reset (+confirm), select-tenant, switch and complete-switch, with no
  `/platform/profile*`.
- Dopad: The paying tenant owner is Assoluto's direct data subject (email, name, Argon2 hash,
  `terms_accepted_ip`). After `/app/admin/profile/delete` their Identity stays live and can
  still log in. That is an Art. 15/17/20 gap, and a B2B buyer's DPO can show it from the
  public repo.
- Návrh: As recommended in F-12: add `GET /platform/profile/export` and
  `POST /platform/profile/delete` behind `require_identity`, wired to the existing service
  functions, with password re-confirmation and `clear_platform_session`. Also null
  `terms_accepted_ip` in `erase_identity`.
- Effort: M
- Auto-fixable: yes

### SEC-3 — Attachment upload blocks the single event loop and reads the full body before the size check
- Severity: P1
- Status vs 2026-07-26: persisted (F-10, F-41)
- Evidence:
  - `app/routers/attachments.py:97` runs `data = await file.read()` before
    `create_attachment_row(... max_size_bytes=...)` (`:106-121`).
  - `:128` calls the synchronous `s3_storage.upload_bytes(...)` directly in an `async def`.
  - The only `run_in_threadpool` in `app/` is `tenant_export_service.py:249`.
  - The thumbnail task is still synchronous S3 plus Pillow on the loop.
- Dopad: Any logged-in contact can freeze the portal for every tenant, including `/healthz`
  and Stripe webhooks, for as long as one 50 MB S3 PUT takes. Oversized uploads cost their full
  size in RSS before they are rejected. Availability, not confidentiality. Shared with backend;
  listed here because it is also an authenticated-DoS primitive.
- Návrh: Check `request.headers["content-length"]` or `file.size` before reading. Wrap
  `upload_bytes`, `download_bytes` and the Pillow work in `run_in_threadpool`.
- Effort: S
- Auto-fixable: yes

### SEC-4 — `privacy.html` now promises encrypted B2 backups; the script and playbook still ship them unencrypted by default
- Severity: P2
- Status vs 2026-07-26: persisted (F-25), narrowed
- Evidence:
  - Fixed half: `app/templates/www/privacy.html:96-97` now lists Backblaze B2 as a subprocessor
    for "uploaded files and encrypted database backups".
  - `:175` claims "encrypted backups".
  - But `scripts/backup.sh:42-53` still writes a plain `.sql.gz` when `BACKUP_GPG_RECIPIENT`
    is unset (it prints "UNENCRYPTED"), and `rclone copy`-ies it off-site (`:72-83`).
  - `docs/OPERATOR_PLAYBOOK.md:226-230` (§3.4) tells the operator to set only `RCLONE_REMOTE`;
    the playbook never mentions `BACKUP_GPG_RECIPIENT`.
  - Whether the VPS env sets it: `unverified`.
- Dopad: If the playbook was followed, every tenant's full DB sits unencrypted at a US-owned
  subprocessor while the privacy policy says it is encrypted. That is a misrepresentation, and
  the exposure on a B2 credential leak is the whole dataset.
- Návrh: Operator: check `/etc/assoluto/env` for `BACKUP_GPG_RECIPIENT`. Code: make
  `backup.sh` refuse to upload off-site when `RCLONE_REMOTE` is set and `BACKUP_GPG_RECIPIENT`
  is empty, and add the GPG step to playbook §3.4.
- Effort: S
- Auto-fixable: yes (script and docs); operator check needed

### SEC-5 — Art. 15/20 exports are still incomplete for both principal types
- Severity: P2
- Status vs 2026-07-26: persisted (F-27)
- Evidence: `app/services/gdpr_service.py:49-99` (`export_for_user`) has `orders_created` and
  `audit_events_authored` but no comments. `:102-147` (`export_for_contact`) has only
  `comments_authored`: no `orders_created` (`Order.created_by_contact_id`), no attachments,
  status changes or audit events. Neither export includes the new `notification_prefs` or
  `orders.assigned_to_user_id`.
- Dopad: A contact who exercises Art. 15 gets a visibly partial export, while the supplier's
  order and audit pages attribute dozens of actions to them. It is easy to prove and a common
  complaint.
- Návrh: Add the missing selects to both exports (about 30 lines; see F-27), including
  `notification_prefs`.
- Effort: S
- Auto-fixable: yes

### SEC-6 — Platform logout does not bump `Identity.session_version`
- Severity: P3
- Status vs 2026-07-26: new (gap in the F-40 fix)
- Evidence: `app/platform/routers/platform_auth.py:113-119`: `platform_logout` only calls
  `clear_platform_session`. Tenant logout bumps the version (`app/routers/public.py:336`), and
  `app/platform/deps.py:77` already enforces `session_version`, so the check exists and is
  never triggered on logout. The platform cookie lasts 14 days (`app/platform/session.py:22`)
  and is scoped to the apex domain, so it rides to every tenant subdomain.
- Dopad: A copied platform cookie (shared machine, malware, a proxy log) stays valid for up to
  14 days after "Log out", and it can mint tenant sessions via `/platform/switch` for every
  membership the identity holds, including platform-admin and support grants.
- Návrh: Mirror `public.py:329-338`. Load the identity from the cookie if it is present and
  valid, increment `session_version`, commit, then clear. Add a test: logout, then replay the
  old cookie → 401.
- Effort: S
- Auto-fixable: yes

### SEC-7 — Production honours a client-supplied `X-Tenant-Slug` header
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `app/deps.py:86-88` gives the header priority over `Host` ("used by tests and
  internal tools") in every environment. `docker/Caddyfile:33-36` does not strip it.
  Live probe: `curl -H "X-Tenant-Slug: demo" https://assoluto.eu/auth/login` returns `200`,
  and a nonexistent slug returns `404`. So any tenant's app can be addressed from the apex,
  and slugs can be enumerated.
- Dopad: No direct breach. Browsers cannot attach the header cross-origin (no CORS
  middleware), and sessions are checked against `tenant.id`. But it breaks the "tenant ==
  host" assumption that host-only cookies, CSP `form-action` and per-host reasoning rely on.
  It also lets a script enumerate tenant slugs and run login attempts against any tenant
  through one hostname. Defence in depth.
- Návrh: Honour the header only when `not settings.is_production` (or behind an explicit
  `TRUST_TENANT_HEADER` flag), and/or add `header_up -X-Tenant-Slug` in the Caddyfile.
- Effort: S
- Auto-fixable: yes

### SEC-8 — Order emails (comment excerpts, file names) reach invited-but-never-accepted addresses
- Severity: P3
- Status vs 2026-07-26: new (comments already behaved this way before PR #7; PR #7 adds
  attachment and order-created mails to the same audience)
- Evidence:
  - `app/services/notification_service.py:200`: the reachability filter yields to pending
    contacts when no accepted contact exists.
  - `:407`: `accepted=contact.accepted_at is not None`.
  - `:613`: the payload carries `body_excerpt` (up to 300 chars).
  - `:660`: the payload carries the uploaded `filename`.
  - `resolve_contact_audience` deliberately keeps pending invitees (`:367-371`).
- Dopad: If staff mistype a contact's address at invite time, a stranger keeps receiving the
  customer's comment text and drawing file names until somebody notices. This is a deliberate
  CLAUDE.md §19 trade-off (silence is the worse failure). The privacy cost is that content,
  not just "something happened", goes to an address that never proved ownership.
- Návrh: Keep the yield, but strip `body_excerpt` and `filename` from payloads for recipients
  with `accepted=False`, so the mail just says "there is activity on order X, accept your
  invitation to see it". `Recipient` needs an `accepted` flag.
- Effort: S
- Auto-fixable: no (touches the §19 design; founder decision)

### SEC-9 — No test pins that one customer's contacts never get mail about another customer's order
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: The code is correct today (`notification_service.py:373`). But both new suites
  (`tests/test_notification_routing.py:66`, `tests/test_notifications_flow.py:70`) seed a
  single `Customer` ("ACME"), so no test fails if that filter is dropped or loosened. For
  example, a future "notify all contacts in the tenant" scope would pass CI. The fallback and
  yield logic in `_select` is exactly the kind of code where such a regression would hide.
- Dopad: The worst-case regression is a cross-customer data leak (customer B's contacts read
  customer A's order titles and comments) with green CI.
- Návrh: Add a test with two customers in one tenant, where customer B has `scope=all` and
  every event enabled. Assert that none of B's addresses appear in any builder's output for an
  order of customer A, for all five events, including the digest path.
- Effort: S
- Auto-fixable: yes

### SEC-10 — A contact can erase themselves and the controller (the tenant) is not told
- Severity: P3 (down from P2: the tenant audit log now records the event)
- Status vs 2026-07-26: persisted (F-28)
- Evidence: `app/routers/me.py:299-308` runs `erase_contact` and then writes a
  `contact.gdpr_erased` audit row into the tenant log. That is the only signal; no email or
  notification reaches tenant staff. After PR #7, a customer whose only contact erased
  themselves silently stops receiving order mail (`resolve_contact_audience` returns `[]`).
- Dopad: The supplier, who is controller per `privacy.html` §1, finds `erased-contact-…@erased.invalid`
  on the next order and has lost its only channel to that customer without warning.
- Návrh: The founder decides whether the processor may act on a direct Art. 17 request. At
  minimum, email the tenant admins on `contact.gdpr_erased`, and show a "customer has no
  reachable contact" banner on the customer page.
- Effort: S
- Auto-fixable: no

### SEC-11 — The manual subscription editor still has no tests and still 500s on a malformed `quick_action`
- Severity: P3
- Status vs 2026-07-26: persisted (F-38)
- Evidence: `app/platform/routers/platform_admin.py:535-536` runs
  `days = int(quick_action.split(":", 1)[1])` without a guard. `grep -rln "subscription_edit\|quick_action" tests/`
  returns nothing.
- Dopad: The impact is limited to platform admins. Nothing pins the "refuse to hand-edit a
  Stripe-managed subscription" guard on the endpoint that grants free plan time.
- Návrh: As in F-38: wrap `int()` and redirect with an error flash, and add
  `tests/test_platform_admin_subscription.py`.
- Effort: S
- Auto-fixable: yes

### SEC-12 — The cookies policy still doesn't match the cookies the app sets
- Severity: P3
- Status vs 2026-07-26: persisted (F-43)
- Evidence: `app/templates/www/cookies.html:61-64` still lists an `sme_theme` cookie that no
  code sets (the theme lives in `localStorage`). `:55-58` calls `sme_locale` "Host-only", but
  `app/routers/public.py:394` sets `domain=cookie_domain`, i.e. apex plus all subdomains.
- Dopad: A DPO doing due diligence will spot that the policy is wrong. No harm to users.
- Návrh: Delete the `sme_theme` row, mention the `localStorage` key `theme`, and change the
  `sme_locale` scope to "assoluto.eu and subdomains".
- Effort: S
- Auto-fixable: yes

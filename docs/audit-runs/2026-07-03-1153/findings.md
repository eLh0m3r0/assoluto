# Audit run 2026-07-03-1153

**Started**: 2026-07-03T10:01:54Z
**Tip-of-tree commit**: `8e16fcb`
**Previous run**: [2026-05-09-0931](../2026-05-09-0931/)

Per-perspective reports: [ux.md](ux.md) · [backend.md](backend.md) ·
[security.md](security.md) · [business.md](business.md)

## Counts

| Perspective | P0 | P1 | P2 |
|---|---|---|---|
| UX        | 0 | 0 | 8 |
| Backend   | 0 | 2 | 5 |
| Security  | 0 | 0 | 2 |
| Business  | 1 | 2 | 3 |
| **Total** | **1** | **4** | **18** |

Note: F-BE-001 and F-SEC-001 are the same underlying gap (GDPR endpoint
test coverage) reported from two perspectives — treat as one work item.

## P0 — must fix before next deploy

### [BIZ] F-BIZ-001 — Fabricated testimonials presented as real customer quotes
- **Where**: `app/templates/www/index.html:358-401` (Testimonials section)
- **Severity**: P0
- **Auto-fixable**: no
- **status**: manual
- **Description**: Header admits early access with no published customers
  ("Logos will appear here as soon as clients agree to be published"), yet
  directly below, three metric-laden quote cards with plausible attributions
  ("Owner, metalwork shop — Morava, 12 employees") render under "What our
  first customers say". Invented endorsements presented as real reviews are
  a banned unfair commercial practice under the EU Omnibus Directive
  (2005/29/EC, transposed into CZ Act 634/1992) — fine-enforceable, and a
  direct contradiction of the operator's honesty stance.
- **Suggested fix**: Remove the three quote cards and keep the honest
  "early access" placeholder, or relabel them unambiguously as illustrative
  scenarios and drop the "What our first customers say" heading until a
  real consented customer exists.

## P1 — fix this sprint

### [BE] F-BE-001 — GDPR endpoints have zero test coverage
- **Where**: `app/routers/tenant_admin.py:595` (`GET /app/admin/profile/export`), `:617` (`POST /app/admin/profile/delete`); `app/services/gdpr_service.py`
- **Severity**: P1 (also filed as [SEC] F-SEC-001 at P2 — same work item)
- **Auto-fixable**: yes (test authoring)
- **status**: fixed (c674577)
- **Description**: Carried unresolved since 2026-05-01. `profile/delete`
  performs irreversible GDPR Art. 17 PII anonymisation guarded by a password
  re-confirmation gate and a last-admin lockout check — neither guard, nor
  the export payload shape, nor the `session_version` bump has a single
  regression test (`grep -rln "profile/export|profile/delete|export_for_user|erase_user" tests/` → empty).
- **Suggested fix**: Add `tests/test_gdpr.py` (~5 cases against
  `tenant_client`): export returns user data as JSON + attachment
  disposition; delete with wrong password → flash, no mutation; delete as
  last admin → blocked; happy path → PII nulled, row retained,
  `user.gdpr_erased` audit row, session cleared.

### [BE] F-BE-002 — Stripe price IDs still NULL on all prod plans
- **Where**: production `/etc/assoluto/env`; `app/main.py:_sync_stripe_prices_from_env`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **status**: manual
- **Description**: Carried unresolved from the previous run. All four
  `platform_plans` rows on prod have `stripe_price_id IS NULL` while
  `is_active = t` — any paid-plan checkout silently no-ops; the billing
  path cannot complete a real subscription.
- **Suggested fix**: Operator sets `STRIPE_PRICE_STARTER` /
  `STRIPE_PRICE_PRO` in `/etc/assoluto/env` and redeploys. Defensive
  follow-up (auto-fixable): log `stripe_price.sync.no_env` and surface an
  admin banner when an active paid plan has no price ID.

### [BIZ] F-BIZ-002 — Support-time promises with no autoresponder or SLA tracker (solo founder)
- **Where**: `app/templates/www/pricing.html:54,78,96`; `app/templates/www/contact.html:32,58,77`
- **Severity**: P1
- **Auto-fixable**: no
- **status**: manual
- **Description**: Pricing promises "Email support (48 h)" / "Priority
  email support (12 h)" / "4 h business hours, written SLA" while the
  contact page says "reply within 1 working day" — internal drift plus
  commitments a solo founder cannot reliably honour (no autoresponder, no
  SLA tracking).
- **Suggested fix**: Standardise on "1 working day" (Starter) / "same
  working day" (Pro) phrased as targets; add a mailbox autoresponder on
  team@assoluto.eu; keep the hard 4 h SLA inside Enterprise contracts only.

### [BIZ] F-BIZ-003 — No trial-nurture email cadence (only expiry job exists)
- **Where**: `app/tasks/periodic.py` (`expire_demo_trials:208`), `app/tasks/email_tasks.py`
- **Severity**: P1
- **Auto-fixable**: no
- **status**: manual
- **Description**: The only trial automation flips lapsed trials to
  canceled. No day-1 onboarding, day-7 activation, or day-25 "trial ending"
  emails exist — for a 30-day no-card trial this is the biggest free→paid
  conversion lever and it is absent.
- **Suggested fix**: Add a small scheduled cadence reusing the SMTP sender
  + Jinja email templates (day 1 / day 7 / day 25), gated on the tenant
  still being on a trial subscription.

## P2 — backlog

### [UX] F-UX-001 — hreflang alternates for all three locales point to the same URL
- **Where**: apex marketing pages `<head>`; locale is Accept-Language negotiated, `?lang=` does not switch
- **Auto-fixable**: no
- **status**: manual
- **Description**: `hreflang="cs|en|de|x-default"` all point at
  `https://assoluto.eu/` — Google treats the cluster as misconfigured,
  losing DE/EN SEO benefit.
- **Suggested fix**: Give each locale a stable URL (`?lang=` or path
  prefix) and point hreflang at it; otherwise drop the hreflang links and
  keep only the canonical.

### [UX] F-UX-002 — Signup form inputs inconsistent dark-mode border (4 of 5 miss `dark:border-*`)
- **Where**: `app/templates/platform/signup.html`
- **Auto-fixable**: yes
- **status**: fixed (4eb457b)
- **Description**: Only `owner_full_name` carries `dark:border-slate-700`;
  `company_name`, `slug`, `owner_email`, `password` keep the light border in
  dark mode, visibly mismatched within one form.
- **Suggested fix**: Add `dark:border-slate-700` to the four missing inputs.

### [UX] F-UX-003 — Tenant auth shell and platform auth shell use divergent input styling
- **Where**: `4mex.assoluto.eu/auth/login` vs `assoluto.eu/platform/login`
- **Auto-fixable**: no
- **status**: manual
- **Description**: Two different input treatments (`rounded-lg`/`ring-2`
  vs `rounded-md`/`ring-1`, different dark backgrounds) for the same
  product.
- **Suggested fix**: Extract one shared input macro/component used by both
  auth shells.

### [UX] F-UX-004 — `text-blue-*` link leak in auth language switcher (persisted, was F-UX-023)
- **Where**: shared auth-shell language-switcher partial (4 auth pages)
- **Auto-fixable**: yes
- **status**: fixed (4eb457b)
- **Description**: Switcher links render `text-blue-600 dark:text-blue-400`
  instead of the brand palette; 8 stranded references.
- **Suggested fix**: Replace with `text-brand-600 dark:text-brand-400` in
  the shared partial.

### [UX] F-UX-005 — Homepage FAQ spells "percent / procent / Prozent" (persisted, was F-UX-024)
- **Where**: `app/templates/www/index.html` FAQ, all three locales (body + JSON-LD)
- **Auto-fixable**: yes
- **status**: fixed (8a207b1)
- **Description**: FAQ writes `99,9 procent` / `99.9 percent` / `99,9
  Prozent` long-form while the pricing card uses the `%` glyph.
- **Suggested fix**: Split-form pattern per CLAUDE.md §7 — render `99,9 %`
  outside the gettext call; update body + JSON-LD; re-extract/compile.

### [UX] F-UX-006 — Czech word "doklad" leaks into EN pricing FAQ (persisted, was F-UX-025)
- **Where**: `app/templates/www/pricing.html` EN msgstr
- **Auto-fixable**: yes
- **status**: fixed (8a207b1)
- **Description**: EN answer ends "...to issue a valid Czech tax doklad."
- **Suggested fix**: `tax doklad` → `tax invoice` in the EN msgstr;
  `pybabel compile`.

### [UX] F-UX-007 — EN pricing Enterprise card uses comma decimal `SLA 99,9 %` (persisted, was F-UX-026)
- **Where**: `app/templates/www/pricing.html` Enterprise card
- **Auto-fixable**: yes
- **status**: fixed (8a207b1)
- **Description**: Literal `SLA 99,9 %` in all locales; English convention
  is `99.9 %`.
- **Suggested fix**: Localize the decimal separator (mind the `%%` trap,
  CLAUDE.md §7).

### [UX] F-UX-008 — DE contact microcopy mixes German opening quote with English closing quote (partial fix of F-UX-027)
- **Where**: `app/templates/www/contact.html` DE msgstr
- **Auto-fixable**: yes
- **status**: fixed (8a207b1)
- **Description**: Renders `„Demo"` ending in U+201D (English right quote)
  instead of German closing U+201C; correct German pair is `„…“`.
- **Suggested fix**: Change the closing character in the DE msgstr from
  U+201D to U+201C.

### [BE] F-BE-003 — Contact/identity GDPR export+erase have no router surface
- **Where**: `app/services/gdpr_service.py` (`export_for_identity`); gap in `app/routers/me.py`
- **Auto-fixable**: no (product decision)
- **status**: manual
- **Description**: Customer contacts have no self-service export/delete
  routes even though the service layer supports it.
- **Suggested fix**: Expose `me/profile/export` + `me/profile/delete` for
  contacts, or document org-admin-mediated erasure.

### [BE] F-BE-004 — Stripe webhook handler set stays narrow
- **Where**: `app/platform/billing/webhooks.py` (`HANDLERS`)
- **Auto-fixable**: no
- **status**: manual
- **Description**: `customer.subscription.paused`, `customer.updated`,
  `payment_method.detached` still silently dropped.
- **Suggested fix**: Add handlers or explicit logged no-ops before billing
  goes live.

### [BE] F-BE-005 — Billing-details audit-row test asserts existence only, not actor/diff
- **Where**: `tests/test_billing_details.py:204-215`
- **Auto-fixable**: yes
- **status**: fixed (c674577)
- **Description**: Test checks a `tenant.settings_updated` row exists but
  not `actor_*` attribution or `before`/`after` payload.
- **Suggested fix**: Widen the SELECT and assert `actor_type == 'user'` and
  `after_data['billing_ico']`. ~8 lines.

### [BE] F-BE-006 — Order comment-author render still unasserted
- **Where**: `app/routers/orders.py:553`; gap in `tests/test_orders_flow.py`
- **Auto-fixable**: yes
- **status**: fixed (c674577)
- **Description**: No test asserts the order-detail page renders the
  resolved commenter name.
- **Suggested fix**: ~5 lines in `test_full_order_lifecycle` — post a
  comment, assert the author's `full_name` in the HTML.

### [BE] F-BE-007 — Full pytest wall time over the 60 s budget (advisory)
- **Where**: full suite (62.44 s, 489 tests)
- **Auto-fixable**: no
- **status**: manual
- **Description**: Flat vs previous run; dominated by three long
  eviction/expiry tests.
- **Suggested fix**: Leave alone unless it creeps past 90 s; optionally
  gate the three slow tests behind `@pytest.mark.slow`.

### [SEC] F-SEC-001 — GDPR export/erasure endpoints have zero test coverage
- **Where**: `app/routers/tenant_admin.py:595,617`; `app/services/gdpr_service.py`
- **Auto-fixable**: no
- **status**: fixed (c674577)
- **Description**: Same gap as F-BE-001 (filed P1 there) — compliance-
  critical destructive endpoint with no regression net. One work item.
- **Suggested fix**: See F-BE-001.

### [SEC] F-SEC-002 — Dead `request.method == "HEAD"` guard on set-lang route (persisted)
- **Where**: `app/routers/public.py:363` vs `app/security/head_method.py:33`
- **Auto-fixable**: no
- **status**: manual
- **Description**: `HeadMethodMiddleware` rewrites HEAD→GET before routing,
  so the handler's HEAD guard is dead code; a HEAD probe sets the locale
  cookie. Low impact, misleading code.
- **Suggested fix**: Remove the dead guard, or record the original verb in
  scope and check that.

### [BIZ] F-BIZ-004 — Founder name lives only on the legal imprint, not where prospects look
- **Where**: `app/templates/www/imprint.html:15`; contact + index templates
- **Auto-fixable**: no
- **status**: manual
- **Description**: "Václav Mudra" appears only on /imprint; homepage and
  contact page have no human name — a trust lever for conservative Czech
  B2B buyers.
- **Suggested fix**: Add a short "Who's behind Assoluto" line (name + one
  sentence + direct email) to the contact page and/or homepage final CTA.

### [BIZ] F-BIZ-005 — "Book a 15-min demo" CTA goes to a contact form, not a booking flow
- **Where**: `app/templates/www/index.html:42-46` → `/contact`
- **Auto-fixable**: no
- **status**: manual
- **Description**: "Book" sets a self-serve scheduling expectation; the
  destination is an async contact form ("write 'demo' in the message").
- **Suggested fix**: Soften copy to "Request a demo", or wire a real
  booking link (Cal.com/Calendly).

### [BIZ] F-BIZ-006 — No stated refund / withdrawal policy despite refund-capable code
- **Where**: `app/templates/www/pricing.html` FAQ; `terms.html`; `app/platform/billing/webhooks.py:373`
- **Auto-fixable**: no
- **status**: manual
- **Description**: `handle_charge_refunded` fully supports refunds but no
  marketing/legal surface states a refund position.
- **Suggested fix**: One FAQ/terms line: monthly non-refundable mid-period
  (access to period end); annual/manual pro-rata at operator discretion.

## Comparison with previous run (2026-05-09-0931)

**Resolved / held**: All previously-fixed findings held — no regression in
any perspective. Backend hygiene improved (+32 tests, mypy still 0 errors,
prod schema at head 1006 with zero ORM drift). All 12 security invariants
except the GDPR test gap pass; live prod headers strong.

**Regressed**: none.

**Persisted (carried forward)**:
- UX: F-UX-004…008 (were F-UX-023…027) — five P2 copy/styling items,
  untouched since the previous run.
- Backend: all seven findings carry over (renumbered; see backend.md
  mapping table). The two P1s (GDPR tests, Stripe price IDs) are now two
  audits old.
- Security: F-SEC-002 (dead HEAD guard) persisted.

**New in this run**:
- [BIZ] F-BIZ-001 (P0 — fabricated testimonials), F-BIZ-002, F-BIZ-003,
  F-BIZ-004, F-BIZ-005, F-BIZ-006 — the business perspective ran deeper
  marketing-honesty checks this round.
- [UX] F-UX-001 (hreflang), F-UX-002 (signup dark border), F-UX-003 (auth
  shell divergence).

**Audit-infrastructure gaps** (not product findings): seeded test tenants
`test-a`/`test-b`/`testfirma` return 404, and no tenant/operator
credentials were available, so the authenticated `/app/*` and
`/platform/admin/*` walkthroughs were skipped; Chrome MCP was unavailable
(no live console/network/dark-mode/mobile checks). Re-seed test tenants or
provide credentials before the next run to restore that coverage.

## Status legend

Each finding starts as `status: open`. The `/audit-fix` command updates
this in place to `fixed`, `wontfix`, or `manual` (operator action
required, not auto-fixable).

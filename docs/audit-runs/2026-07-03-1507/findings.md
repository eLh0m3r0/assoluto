# Audit run 2026-07-03-1507 (verification pass)

**Started**: 2026-07-03T13:11:32Z
**Tip-of-tree commit**: `cbe5afa`
**Previous run**: [2026-07-03-1153](../2026-07-03-1153/)

Per-perspective reports: [ux.md](ux.md) · [backend.md](backend.md) ·
[security.md](security.md) · [business.md](business.md)

## Counts

| Perspective | P0 | P1 | P2 |
|---|---|---|---|
| UX        | 0 | 0 | 2 |
| Backend   | 0 | 1 | 3 |
| Security  | 0 | 0 | 1 |
| Business  | 1 | 2 | 3 |
| **Total** | **1** | **3** | **9** |

Every finding in this run is a carry-over from the previous run's manual
queue. **Zero new defects, zero regressions; all 10 auto-fixes held** (6
verified on the live site, 4 in the test suite). Note: the security
perspective renumbered the dead-HEAD-guard finding from F-SEC-002 to
F-SEC-001 this run — it is the same issue.

## P0 — must fix before next deploy

### [BIZ] F-BIZ-001 — Fabricated testimonials presented as real customer quotes
- **Where**: `app/templates/www/index.html:358-401` (Testimonials section)
- **Severity**: P0
- **Auto-fixable**: no
- **status**: wontfix
  (founder decision 2026-07-03: keep testimonials as-is, risk accepted)
- **Description**: Confirmed still live on prod this run. Three invented,
  metric-laden quote cards with plausible attributions render under "What
  our first customers say" while the section itself admits no customers
  are published yet. Banned unfair commercial practice under the EU
  Omnibus Directive (2005/29/EC, CZ Act 634/1992). Needs a founder
  content decision: remove the cards or relabel as illustrative.

## P1 — fix this sprint

### [BE] F-BE-002 — Stripe price IDs still NULL on paid prod plans
- **Where**: production `/etc/assoluto/env`; `app/main.py:_sync_stripe_prices_from_env`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **status**: manual
- **Description**: Persists (3rd audit). Operator must set
  `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` in `/etc/assoluto/env` and
  redeploy; until then paid checkout silently no-ops.

### [BIZ] F-BIZ-002 — Support-time promises with no autoresponder or SLA tracker
- **Where**: `app/templates/www/pricing.html:54,78,96`; `app/templates/www/contact.html:32,58,77`
- **Severity**: P1
- **Auto-fixable**: no
- **status**: fixed (8242846)
- **Description**: Persists. Pricing 48 h/12 h/4 h promises vs contact
  page "1 working day"; no autoresponder or SLA tracking behind them.

### [BIZ] F-BIZ-003 — No trial-nurture email cadence
- **Where**: `app/tasks/periodic.py` (`expire_demo_trials`), `app/tasks/email_tasks.py`
- **Severity**: P1
- **Auto-fixable**: no
- **status**: fixed (36acd70) — cadence deployed behind TRIAL_NURTURE_ENABLED=false; operator flips the flag after approving the copy
- **Description**: Persists. Only the expiry job exists; no day-1/7/25
  onboarding-to-paid cadence for the 30-day trial.

## P2 — backlog

### [UX] F-UX-001 — hreflang alternates for all three locales point to the same URL
- **Where**: apex marketing pages `<head>`
- **Auto-fixable**: no
- **status**: manual
- **Description**: Persists — needs per-locale URLs or dropping hreflang.

### [UX] F-UX-003 — Tenant auth shell and platform auth shell use divergent input styling
- **Where**: `4mex.assoluto.eu/auth/login` vs `assoluto.eu/platform/login`
- **Auto-fixable**: no
- **status**: manual
- **Description**: Persists — extract one shared input macro/component.

### [SEC] F-SEC-001 — Dead `request.method == "HEAD"` guard on set-lang route (was F-SEC-002)
- **Where**: `app/routers/public.py:363` vs `app/security/head_method.py:33`
- **Auto-fixable**: no
- **status**: manual
- **Description**: Persists — middleware rewrites HEAD→GET before routing,
  making the guard dead code. Low impact; remove or make effective.

### [BE] F-BE-003 — Contact/identity GDPR export+erase have no router surface
- **Where**: `app/services/gdpr_service.py`; gap in `app/routers/me.py`
- **Auto-fixable**: no (product decision)
- **status**: manual
- **Description**: Persists — expose self-service routes for contacts or
  document org-admin-mediated erasure.

### [BE] F-BE-004 — Stripe webhook handler set stays narrow
- **Where**: `app/platform/billing/webhooks.py` (`HANDLERS`)
- **Auto-fixable**: no
- **status**: manual
- **Description**: Persists — `customer.subscription.paused`,
  `customer.updated`, `payment_method.detached` still dropped; handle
  before billing goes live.

### [BE] F-BE-007 — Full pytest wall time hovering at the 60 s budget (advisory)
- **Where**: full suite (61.0 s, 494 tests)
- **Auto-fixable**: no
- **status**: manual
- **Description**: Persists, informational only.

### [BIZ] F-BIZ-004 — Founder name lives only on the legal imprint
- **Where**: `app/templates/www/imprint.html`; contact + index templates
- **Auto-fixable**: no
- **status**: manual
- **Description**: Persists — add a short "who's behind Assoluto" line.

### [BIZ] F-BIZ-005 — "Book a 15-min demo" CTA goes to a contact form
- **Where**: `app/templates/www/index.html:42-45` → `/contact`
- **Auto-fixable**: no
- **status**: fixed (8242846)
- **Description**: Persists — soften copy or wire a real booking link.

### [BIZ] F-BIZ-006 — No stated refund / withdrawal policy
- **Where**: `app/templates/www/pricing.html` FAQ; `terms.html`; `webhooks.py:373`
- **Auto-fixable**: no
- **status**: fixed (8242846)
- **Description**: Persists — state the refund position once in
  pricing/terms.

## Verification vs. previous run (`2026-07-03-1153`)

### Resolved (fixes held)
* F-UX-002 — Signup dark-mode borders (fixed in 4eb457b; verified live)
* F-UX-004 — Language-switcher brand palette (fixed in 4eb457b; verified live)
* F-UX-005 — "99,9 %" homepage FAQ, body + JSON-LD (fixed in 8a207b1; verified live in CS/EN/DE)
* F-UX-006 — "tax invoice" EN wording, pricing + billing details (fixed in 8a207b1; verified live)
* F-UX-007 — EN SLA decimal "99.9 %" (fixed in 8a207b1; verified live)
* F-UX-008 — DE closing quotes U+201C (fixed in 8a207b1; verified live)
* F-BE-001 / F-SEC-001(prev) — GDPR endpoint test coverage (fixed in c674577; 5 tests present and passing)
* F-BE-005 — Billing audit-row actor/diff assertions (fixed in c674577)
* F-BE-006 — Comment-author render assertion (fixed in c674577)

### Persisted (open in both runs)
* None with status `open` — everything carried forward was already `manual`.

### Regressed (came back!)
* None.

### New in this run
* None.

### Manual / operator action (13 items, unchanged)
* P0: F-BIZ-001 testimonials (founder content decision)
* P1: F-BE-002 Stripe price IDs (`/etc/assoluto/env`), F-BIZ-002 support
  promises, F-BIZ-003 trial nurture
* P2: F-UX-001 hreflang, F-UX-003 auth-shell styling, F-SEC-001 dead HEAD
  guard, F-BE-003 contact GDPR routes, F-BE-004 webhook handlers,
  F-BE-007 pytest budget, F-BIZ-004 founder bio, F-BIZ-005 demo CTA,
  F-BIZ-006 refund policy

### Coverage gaps (unchanged from previous run)
Chrome MCP unavailable and no tenant/operator credentials; seeded test
tenants (`test-a`, `test-b`, `testfirma`) still 404 — authenticated
`/app/*` and `/platform/admin/*` walkthroughs plus live console/dark-mode/
mobile checks remain unverified. Re-seed tenants or provide credentials
to restore this coverage.

## Status legend

Each finding starts as `status: open`. The `/audit-fix` command updates
this in place to `fixed`, `wontfix`, or `manual` (operator action
required, not auto-fixable).

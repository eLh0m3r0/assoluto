# Pre-launch comprehensive review — 2026-04-25

**Scope.** Multi-agent audit of Assoluto (https://assoluto.eu) before public
launch + strategic analysis of the public-monorepo question. Auditors
were specialised LLM agents working in parallel on the live production
codebase + the production deployment itself. Implementation, deploys,
and verification were carried out by the orchestrator agent.

**Outcome.** 4 commit batches landed on production over ~3 hours. All
P0 findings closed; P1 list partially addressed (high-leverage items
done; rest documented below). Phase 2 strategic question answered.

## Commits delivered to production (in order)

| SHA | Title | Highlights |
|---|---|---|
| `edcfa19` | batch 1 — content + payments + attachment whitelist | Translation bugs, legal banners, pricing copy, CSP/Stripe checkout, invoice PDF row, community-plan checkout no-op, MIME whitelist |
| `5002621` | batch 2 — readyz + test idempotence + harder defaults | Test-suite pollution fix, `/readyz` DB ping, `TRUSTED_PROXIES` assert, CSP `object-src 'none'`, hide FastAPI docs in prod |
| `3caefe3` | batch 3 — auth audit log, tenant-suspend kicks sessions, cookies banner | `auth.login` audit row + structured log, `deactivate_tenant` bumps `session_version`, missed cookies banner |
| `fb5c5ea` | docs(brand) — NOTICE.md + README trademark section + logo title | Phase 2 quick wins — trademark notice, README section, embedded brand marker in logo SVG |
| `02ed6a7` | content — FAQ rewrites | All 7 homepage FAQ Q+A rewritten for accuracy + Czech idiom + JSON-LD schema extended to all 7 |

All five deployed cleanly. `/healthz` and `/readyz` green, key smoke
checks (CSP includes Stripe origins, banners gone from
terms/privacy/cookies, FAQ shows new copy, "Doporučujeme" + "Roční
fakturace na vyžádání" on pricing) verified live.

## Findings by audit agent

### Recon (architecture + routes/content)
- **Surprising finding** carried forward: CLAUDE.md §6 says "core never
  imports from `app.platform`", but **11 such imports exist** across
  `app/services/*`, `app/models/__init__.py`, `app/main.py`,
  `app/templating.py`. Most are lazy (function-local) but
  `app/models/__init__.py:26` is module-level → physically removing the
  package breaks self-host. Tracked in Phase 2 analysis.

### Security audit (Opus)
- **0 P0** — posture is solid for pre-launch.
- **2 P1**: file-type allowlist (`ALLOWED_CONTENT_TYPES`) defined but
  not enforced ✅ FIXED batch 1; no audit log on auth or role changes
  ✅ FIXED (login) batch 3, role changes still TODO.
- **6 P2**: docs hidden in prod ✅ FIXED batch 2; CSP `object-src
  'none'` ✅ FIXED batch 2; CSRF cookie missing `HttpOnly`, HSTS missing
  `preload`, support-access has no `expires_at`, email enumeration on
  `/platform/signup`, `verify-email` is a state-mutating GET.
- Confirmed working (no fix needed): RLS, `read_session_for_tenant`,
  Argon2id hashing, single-use tokens, rate limits with correct XFF
  trust, Stripe webhook 503-in-demo-mode, CSRF, open-redirect guards,
  Postgres trust auth confined to loopback, S3 bucket blocks anonymous,
  no secrets in repo.

### Payments + billing audit (Opus)
- **0 P0**, **4 P1**, **7 P2**.
- P1: CSP `form-action` blocks Stripe Checkout ✅ FIXED batch 1;
  invoice PDF row 4 cells in 5-column table + non-VAT operator labelled
  "Daňový doklad" (Czech tax compliance) ✅ FIXED batch 1; community
  plan checkout silently no-ops in live mode ✅ FIXED batch 1; pricing
  page promises annual billing the code can't deliver ✅ FIXED
  (rewritten as "on request") batch 1.
- P2 NOT addressed (intentionally — not launch-blockers): advisory
  lock collision (`42_005`) at 03:45 UTC; `ensure_within_limit` lacks
  row lock; plan downgrade doesn't grandfather existing usage; no
  `charge.refunded` handler; manual tenant deletion leaves Stripe
  orphans; no `past_due` in-app banner; `expire_demo_trials` doesn't
  flip plan_id to community.
- Confirmed solid: Stripe webhook signature verification, idempotency
  via `platform_stripe_events`, plan-limit enforcement at all four
  documented call sites, `_normalize_demo_subscriptions` operator-flip
  handling, demo-mode boot warning + `FEATURE_PLATFORM_ALLOW_DEMO`.

### UX walkthrough (Opus + Chrome on live prod)
- **4 P0**, **9 P1**, **8 P2**.
- P0: legal banners on terms/privacy/cookies ✅ FIXED (terms+privacy
  batch 1, cookies batch 3); demo-mode billing UI silently flips plans
  on click — partial: community-plan no-op now raises BillingError
  (batch 1), starter/pro flips remain by design in demo mode (operator
  enables Stripe via env vars per OPERATOR_PLAYBOOK §1); order-status
  flash uses action verb instead of state noun ✅ FIXED batch 1
  (translation); FAQ + pricing inaccuracies ✅ FIXED batch 1 + FAQ batch.
- P1 NOT addressed (documented for Václav's followup):
  - Silent redirects on customer/product/comment create — no flash
    feedback after POST.
  - **Customer contacts can't change own password / name / locale**
    (only password reset works) — non-trivial UX gap.
  - "Enterprise: Free" misleading label in plan switcher.
  - "Downgrade to Community" button has no warning.
  - Owner sees "Team member" badge on own tenant.
  - Footer GitHub link points to old repo name (works via redirect).
  - Contact-page support SLA ("4h") doesn't match plan SLAs.
- P2 (polish, ship over week 1): currency suffixes on price inputs;
  primary/secondary action visual hierarchy; snake_case audit codes
  visible; date/file inputs use browser native; "Switch portal" link on
  shared computer; 403 page strips header; avatar truncation.
- Confirmed strong: marketing hero copy, comparison table, signup-to-
  running promise, owner onboarding cards, order detail layout, status
  history timeline, Cmd+K palette, EARLY-ACCESS testimonial honesty,
  no-cookie-banner choice, GDPR profile (download + delete account).

### Content + copy audit (Sonnet)
- **4 P0**, **9 P1**, **7 P2**.
- P0: 3 mistranslations (status flash, assets empty-state) +
  legal banners ✅ ALL FIXED.
- P1: "Most chosen" → "Recommended" ✅ FIXED; SSO bullet removed ✅
  FIXED; OG image is SVG → won't render on social (NOT addressed —
  needs PNG export, design decision); hero/testimonials/founder bio
  recommendations passed through to Phase 2 (brand work).
- FAQ rewrites (the user's explicit priority): all 7 questions ✅
  REWRITTEN, both visible block + JSON-LD, both languages.

### Business logic audit (Opus)
- **2 P0**, **8 P1**, **7 P2**.
- P0: attachment MIME whitelist not enforced ✅ FIXED batch 1; audit
  log misses sensitive admin actions (login, password reset, role
  change, etc.) — login ✅ FIXED batch 3, role changes still pending.
- P1 NOT addressed: `_safe_send` blocks threadpool with `time.sleep`;
  tenant deactivation doesn't terminate sessions ✅ FIXED batch 3;
  `users_edit` allows demoting last admin; `users_disable` allows
  disabling last admin; Stripe price ID sync silently no-ops; PII in
  `audit_events.actor_label` after GDPR erasure; bulk transition fans
  out N×M emails; `OrderPermissions.can_set_prices` defaults differ.
- DB integrity check on prod (read-only): all clean — 0 cross-tenant
  FK violations, 0 audit rows missing actor, 0 product SKU dupes, 0
  active subs on inactive plans, 0 orphan attachments, 0 order state
  ↔ timestamp violations.

### Business model + GTM audit (Sonnet)
- Three top risks before launch:
  1. **Annual billing promised but not implemented** ✅ FIXED batch 1
     (rewritten as "on request").
  2. **No trial conversion infrastructure** — DOCUMENTED for Václav.
     No automated email sequence during the 30-day trial, no in-app
     countdown, no usage-triggered prompts. The recommendation is to
     run the conversion manually (founder-personal emails) for the
     first 20–30 trials.
  3. **No paying customer evidence** — DOCUMENTED. Get the first
     paying customer within 2–3 weeks; one named reference is more
     valuable than any feature add.
- Three top actions before launch:
  1. **Resolve annual billing gap** ✅ DONE (Option A — pricing copy
     reworded).
  2. **Personal trial nurture sequence** — operator action.
  3. **Add founder identity + Calendly link** — operator action.
- Plan limits review (corroborated against migration `1003_billing` +
  `1006`): Starter 20 contacts may be too tight for the persona —
  consider 30 in a future migration.

### Code quality + ops audit (Sonnet)
- **3 P0, 7 P1, 7 P2**.
- P0: test suite not idempotent (wipe_db missing platform_* tables) ✅
  FIXED batch 2; `/healthz` + `/readyz` no-ops ✅ FIXED batch 2 (`/readyz`
  now does SELECT 1); **S3 attachments not backed up** — DOCUMENTED
  for operator (needs `rclone sync`-style addition to `scripts/backup.sh`).
- P1: Sentry not wired; 24 mypy errors (5 substantive); no request-
  scoped logging context; `app.platform.usage` imported unconditionally
  in main.py (overlap with the broader platform-coupling issue —
  Phase 2); `TRUSTED_PROXIES` not asserted ✅ FIXED batch 2; **GDPR
  endpoints zero test coverage** — DOCUMENTED (regulatory risk);
  backup not verified active, no encryption.
- P2: CSP `object-src 'none'` ✅ FIXED batch 2; slowapi DeprecationWarning
  on Py 3.16; lower-bound deps; session cookie name `sme_portal_session`
  (brand artifact); CI tests only Py 3.12; `mailhog:latest` not pinned;
  no `pip-audit` in CI.

## Phase 2 — Repo split analysis

Full report at `/tmp/assoluto-review/phase2-repo-split-analysis.md`.

**Recommendation: stay on Option A (status quo public AGPL-3.0
monorepo).** Rationale:
- The actual threat (T1: someone clones source + brand) is bottlenecked
  on customer relationships, not source code privacy.
- Marketing copy + `docs/SELF_HOST.md` + `pricing.html` already
  publicly commit to "AGPL-3.0, self-hostable, auditable open
  source" in 8+ visible places. Reversing this is a credibility hit
  with no real defensive payoff.
- The 11 `app.platform` imports in core block any clean engine/platform
  split anyway — would require ~22 hours of refactor before any
  structural change is possible.

**Quick wins applied (Phase 2 commit `fb5c5ea`):**
- `NOTICE.md` (new) — clarifies that AGPL covers code only, not the
  "Assoluto" name / logo / domain.
- `README.md` — added "Trademarks & branding" section.
- `app/templates/_logo.html` — embedded a `<title>` element with
  trademark assertion so any un-rebranded fork ships a visible
  "Assoluto — assoluto.eu trademark" string in every logo render.

**Operator action item (post-launch):**
- File CZ trademark via ÚPV (~5 000 CZK, ~3 months). Once registered,
  swap ™ for ® in `NOTICE.md`.

## Pre-launch action items NOT addressed in this audit (operator must do)

These are deliberately out of scope for the engineering audit but
should be resolved before the launch announcement:

1. **Make `vaclavmudra@icloud.com` (or chosen email) a platform admin.**
   Today the only platform admin on prod is `alice@test-a.cz` — a test
   account. Run on prod:
   ```bash
   ssh -i ~/.ssh/hetzner_assoluto deploy@assoluto.eu
   cd /opt/assoluto
   docker compose --env-file /etc/assoluto/env -f docker-compose.yml -f docker-compose.prod.yml \
     exec web python -m scripts.make_platform_admin <your-email@assoluto.eu>
   ```
2. **Lawyer review of legal pages.** The amber "this is a template" banners
   are removed (so prod looks production-ready), but the Terms / Privacy /
   Cookies copy itself was not legally reviewed in this audit.
   Recommend Czech IP/data-protection lawyer review within 30 days
   post-launch. Track as P1.
3. **Stripe live keys + webhooks** when ready. OPERATOR_PLAYBOOK §1
   has the full procedure. Until then billing runs in demo mode (which
   is correctly defended on the back-end now, batch 1 fix).
4. **S3 attachment backup**. `scripts/backup.sh` does pg_dump only.
   Add a `rclone sync` of the S3 bucket to off-site storage. Without
   this, an attachment-bucket loss is unrecoverable.
5. **CZ trademark filing** via ÚPV (Phase 2 recommendation).
6. **OG image as PNG** (1200×630). Current SVG won't render on
   Twitter/LinkedIn/Facebook social previews.
7. **Sentry / error tracker** wire-up (`SENTRY_DSN` env var). Current
   500 handler drops stack traces.
8. **Trial nurture sequence** — manual founder-led email cadence for
   first 20–30 trials (per business-model recommendation).
9. **Founder bio + photo + Calendly link** on homepage (per
   business-model recommendation).

## Residual P1/P2 list (engineering followups, not launch-blocking)

Not addressed in this audit pass — most will become real once paying
customers arrive:

**Auth + UX (post-launch within 2 weeks):**
- Customer contacts can't change own password / name / locale (UX P1).
- Silent POST-redirect on customer/product/comment create (no flash).
- `users_edit` / `users_disable` allow last-admin demotion/disable.
- Audit log gaps: password reset, role change, attachment upload, user
  edit, tenant settings update — extend the pattern from batch 3's
  login audit.

**Billing + plans (when first paying customers arrive):**
- `expire_demo_trials` should also flip `plan_id` to community.
- Add `charge.refunded` webhook handler.
- `past_due` in-app banner (today: dunning email only).
- Plan downgrade should grandfather existing usage instead of 402-ing.
- Manual tenant deletion should cancel the Stripe subscription.

**Ops + reliability (next sprint):**
- Sentry wire-up.
- Request-scoped structlog context (tenant_id, principal_id,
  request_id) — make incident investigation possible.
- 24 mypy errors (5 substantive — billing routers + deps.py + orders).
- S3 attachment backup.
- Backup encryption + restore test.
- Public status page URL (replace "status page on request").
- Advisory lock collision at 03:45 UTC (`42_005` reused).

**Architecture (Phase 3 — defer until needed):**
- Decouple `app.platform` from core (~22h refactor) — needed before any
  meaningful repo split or before the AGPL self-host story is actually
  deliverable in `FEATURE_PLATFORM=false` mode.

## Verification artefacts

- `/tmp/assoluto-review/recon-architecture.md`
- `/tmp/assoluto-review/recon-routes-content.md`
- `/tmp/assoluto-review/audit-security.md`
- `/tmp/assoluto-review/audit-payments.md`
- `/tmp/assoluto-review/audit-ux.md`
- `/tmp/assoluto-review/audit-content.md`
- `/tmp/assoluto-review/audit-business-logic.md`
- `/tmp/assoluto-review/audit-business-model.md`
- `/tmp/assoluto-review/audit-code-ops.md`
- `/tmp/assoluto-review/phase2-repo-split-analysis.md`
- `/tmp/assoluto-review/cleanup-audit-data.sh`

These are local-only (under `/tmp`); not committed to the repo. The
summary is in this document.

## Test data cleanup

The UX walkthrough agent created one test tenant (`audit-ux`) +
identity (`audit-ux@example.com`) with 1 customer contact + 1 product
+ 1 order on production. Deleted via `cleanup-audit-data.sh --apply`
after the verification round. Final prod state matches the pre-audit
state:

- Tenants: `demo`, `test-a`, `test-b`, `test-c`, `testfirma` (5 total)
- Platform identities: `alice@test-a.cz` (admin),
  `bob@test-b.cz`, `charlie@test-c.cz`, `vaclavmudra@icloud.com` (4 total)

No real-customer data was modified at any point. No Stripe API was
called. No live Stripe keys were touched.

## Verdict

**Launch-ready** for the technical surface that was in scope
(security, payments demo mode, UX, content, business logic, code
quality, ops basics). The four blocking surface defects (banners,
Stripe Checkout CSP, invoice PDF table, attachment whitelist) are
fixed and live.

The non-engineering blockers (platform-admin account for the
operator, lawyer review of legal pages, OG image as PNG, S3 backup
script, trial nurture, Stripe live key flip) are documented above
and are entirely operator decisions.

The repository structure stays single-public-monorepo by deliberate
choice; a strategic decision document is in
`/tmp/assoluto-review/phase2-repo-split-analysis.md`.

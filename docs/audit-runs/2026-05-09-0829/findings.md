# Audit run 2026-05-09-0829

**Started**: 2026-05-09T08:29:00+02:00
**Tip-of-tree commit**: `d0a5e35`
**Previous run**: [`2026-05-01-1455`](../2026-05-01-1455/)

## Counts

| Perspective | P0 | P1 | P2 |
|---|---|---|---|
| UX        | 0 | 1 | 6 |
| Backend   | 0 | 3 | 5 |
| Security  | 0 | 0 | 1 |
| Business  | 1 | 2 | 0 |
| **Total** | 1 | 6 | 12 |

> Status legend (per finding): `open`, `fixed`, `wontfix`, `manual`. The `/audit-fix` skill flips `open` → `fixed` (with the commit SHA) for every `Auto-fixable: yes` finding it processes; everything else stays `open` until the operator handles it (then becomes `manual`).

---

## P0 — must fix before next deploy

### [BIZ] F-BIZ-012 — Homepage step 1 promises "IČO/Company ID" at signup but form does not collect it
- **Where**: `app/templates/www/index.html:204` (and CS msgstr at `app/locale/cs/LC_MESSAGES/messages.po:3796-3800`); contradicted by `app/templates/platform/signup.html:30-110`.
- **Live (CS)**: `E-mail, heslo, IČO. Za 30 vteřin máte vlastní adresu — třeba vasefirma.assoluto.eu.`
- **Live (EN)**: `Email, password, Company ID. You'll get your own address — e.g. yourfirm.assoluto.eu — in 30 seconds.`
- **Auto-fixable**: yes
- **status**: open
- **Description**: The "How it works" step-1 panel promises signup needs IČO. After commit `17a662d`, signup actually collects only `company_name + owner_email + owner_full_name + password + terms_accepted`. IČO/DIČ/billing address is now JIT-collected at first paid Stripe checkout via `/platform/billing/details` (gated by `_billing_details_present`). Promise gap — the operator's own homepage tells the prospect to have IČO ready; the form silently doesn't ask; the IČO requirement only resurfaces 30 days later at Upgrade.
- **Suggested fix**: Lowest-friction = drop "IČO/Company ID" from homepage step 1 copy across CS/EN/DE. New copy: *"Email, password, company name. You'll get your own address — e.g. yourfirm.assoluto.eu — in 30 seconds."* + add a Pricing FAQ entry about what's required at first paid checkout (covered by F-BIZ-013). Higher-friction alternative = add an optional IČO field to signup and seed `tenant.settings`.

---

## P1 — fix this sprint

### [UX] F-UX-016 — hreflang alternates all point to the same URL → invalid per Google's spec
- **Where**: `https://assoluto.eu/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `sitemap.xml`
- **Auto-fixable**: no
- **status**: open
- **Description**: The hreflang fix from F-UX-005 added `<link rel="alternate" hreflang="cs|en|de|x-default">` but every alternate `href` is the canonical URL itself. Site uses cookie + `Accept-Language` content negotiation, so there's no separate URL to point to. Google's hreflang docs require distinct URLs per language. Search Console will flag as invalid hreflang; CS/EN/DE versions never get separately indexed for the right markets.
- **Suggested fix**: Two viable shapes — (a) subpath locales `/cs/pricing`, `/en/pricing`, `/de/pricing`; (b) query-string locales `/pricing?lang=en`. (a) is the cleaner long-term shape; (b) is faster if you keep a single handler per page and key the rendered locale off the query string when present.

### [BE] F-BE-001 — Billing-details mutation has no audit-trail entry
- **Where**: `app/platform/routers/billing.py:422` (`billing_details_save`)
- **Auto-fixable**: yes
- **status**: open
- **Description**: `POST /platform/billing/details` writes IČO / DIČ / fakturační název / fakturační adresa into `tenant.settings` JSONB and `db.commit()`s — but never calls `audit_service.record(...)`. Other privileged tenant-settings mutations (default-locale changes at `tenant_admin.py:759`, role changes at `tenant_admin.py:381`) do. Compliance gap — answering "who changed our IČO last Tuesday?" needs the audit row.
- **Suggested fix**: After the mutation, call `audit_service.record(db, action="tenant.settings_updated", entity_type="tenant", entity_id=tenant.id, entity_label=tenant.name, actor=ActorInfo(type="user", id=user_target.id, label=identity.email), before={"billing_ico": old_ico, ...}, after=cleaned, tenant_id=tenant.id)`. Mirror `tenant_admin.py:759`.

### [BE] F-BE-002 — Stripe checkout silently no-ops in production — PERSISTS
- **Where**: production env file `/etc/assoluto/env`; affects `app/main.py:_sync_stripe_prices_from_env` → `app/platform/billing/service.py:create_checkout_session`
- **Auto-fixable**: no (operator config)
- **status**: open
- **Description**: Read-only psql against prod still shows `stripe_price_id IS NULL` for all four `platform_plans` rows. The new billing-details gate slightly worsens this — a tenant who fills in IČO + adresa now passes the local gate, hits Stripe checkout, and STILL silently no-ops because price IDs aren't synced.
- **Suggested fix**: Operator unblock = set `STRIPE_PRICE_STARTER` and `STRIPE_PRICE_PRO` in `/etc/assoluto/env`. Defensive code follow-up: emit `stripe_price.sync.no_env` info-level log when both env vars are empty + surface a `/platform/admin` banner when any active paid plan has no `stripe_price_id`.

### [BE] F-BE-003 — Zero test coverage for GDPR endpoints — PERSISTS
- **Where**: `app/routers/tenant_admin.py` (`GET /app/admin/profile/export`, `POST /app/admin/profile/delete`); service `app/services/gdpr_service.py`
- **Auto-fixable**: yes (deferred — needs test design, not a one-line auto-fix)
- **status**: open
- **Description**: `grep -rln "gdpr\|GDPR\|profile/export\|profile/delete\|export_for_user\|erase_user\|export_for_contact\|erase_contact" tests/` still empty. The post-baseline commits added an `Identity` export path (`gdpr_service.py:150 export_for_identity`) — also uncovered.
- **Suggested fix**: see `2026-05-01-1455/backend.md` F-BE-003 for the proposed test cases. Add one for `export_for_identity` while you're in there.

### [BIZ] F-BIZ-013 — Pricing FAQ does not disclose the IČO/DIČ checkout gate
- **Where**: `app/templates/www/pricing.html:164-170` (Pricing FAQ macro invocations); `app/templates/www/index.html:425` (FAQ).
- **Auto-fixable**: yes
- **status**: open
- **Description**: Trial users hit a hard stop at first paid checkout if `tenant.settings` is missing IČO / billing_name / billing_address. Marketing copy never warns about this. The dashboard surfaces an amber banner (`dashboard.html:14-25`) but a prospect comparison-shopping on /pricing has no way to know what they'll need. Friction surprise at the moment of highest commercial intent (clicking Upgrade) = trust gap.
- **Suggested fix**: Add one Pricing FAQ entry (CS): *"Co potřebuju zadat při prvním placeném checkoutu? Fakturační název, IČO (8 číslic) a fakturační adresu. DIČ pokud jste plátce DPH. Bez nich nemůžeme vystavit platný daňový doklad. Vyplníte jednou — uložíme."* EN/DE mirrors. Bonus: tooltip on the pricing card CTA: "Karta + IČO se zadávají až na konci 30 dní."

### [BIZ] F-BIZ-014 — SLA tier inversion: Pro promises 12 h, Enterprise promises 24 h
- **Where**: `app/templates/www/pricing.html:78` (Pro) and `:96` (Enterprise)
- **Auto-fixable**: yes
- **status**: open
- **Description**: Pricing card SLA is inverted. Starter = 48 h, Pro = 12 h, Enterprise = 24 h. A Pro customer paying 1 490 Kč/mo gets a faster guaranteed response than Enterprise — copy-paste bug or a number Enterprise prospects will negotiate out of the contract immediately. Either way it makes Enterprise visibly weaker and undermines the contact CTA. Pre-launch fix.
- **Suggested fix**: Make Enterprise strictly tighter than Pro. Two clean options — (a) Enterprise = "Priority support (4 h business hours, written SLA)" matching the "SLA 99.9 percent" claim already on the same card; (b) Enterprise = "SLA on request" without a number (drop the redundant "Priority support" line). Update CS/DE msgstrs in the same commit.

---

## P2 — backlog

### [UX] F-UX-017 — HEAD returns 405 on every public GET endpoint except `/set-lang`
- **Where**: `https://assoluto.eu/`, `/pricing`, `/features`, `/contact`, `/terms`, `/sitemap.xml`, tenant `/auth/login`, `/auth/password-reset`, `/platform/login`, `/platform/signup`
- **Auto-fixable**: yes
- **status**: open
- **Description**: F-UX-014 patched `/set-lang` for HEAD; the underlying issue (FastAPI/Starlette doesn't auto-derive HEAD from GET) is not framework-wide. RFC 9110 requires HEAD wherever GET works. UptimeRobot defaults to HEAD for HTTP/2; security scanners use HEAD as cheap reachability test. Marketing pages (most-monitored) get the noise.
- **Suggested fix**: Add a small ASGI middleware in `app/main.py` that intercepts HEAD, dispatches as GET internally, strips body before send. ~15 lines fixes every current and future GET route. Mount **after** Starlette routing so it falls back to per-route registration for endpoints that genuinely don't support HEAD.

### [UX] F-UX-018 — Tenant `/auth/login` still uses `bg-blue-600` while marketing + platform login use `bg-brand-600`
- **Where**: `app/templates/auth/login.html`, `auth/password_reset.html`
- **Auto-fixable**: yes
- **status**: open
- **Description**: Marketing + platform-apex auth migrated to `brand-*`. Tenant subdomain `/auth/login` still uses `bg-blue-600/700/800`. A prospect who clicks "Vyzkoušet" on marketing → signs up → lands on `acme.assoluto.eu/auth/login` sees the brand colour shift. Small but a brand-consistency drift on the primary CTA.
- **Suggested fix**: Replace `bg-blue-600/700/800` → `bg-brand-600/700`, `text-blue-600/400` → `text-brand-600/400`, `focus:ring-blue-500/20` → `focus:ring-brand-500/20`. About a dozen swaps per file.

### [UX] F-UX-019 — Contact form lacks the honeypot the platform signup uses
- **Where**: `app/routers/www.py:92-176`, `app/templates/www/contact.html`
- **Auto-fixable**: yes
- **status**: open
- **Description**: `/platform/signup` ships hidden `name="website"` honeypot + backend reject on non-empty. Contact form has no equivalent — only defence is `@rate_limit("5/15 minutes")`, which doesn't help against distributed cheap bots. Contact submissions email the founder directly = wasted time.
- **Suggested fix**: Mirror the signup honeypot in `contact.html` (hidden div with `<input type="text" name="website" tabindex="-1" autocomplete="off">`). In `contact_submit`, accept `website: str = Form("")` and silently 200/204 without sending email when non-empty. Keep the rate-limit; layered defence.

### [UX] F-UX-020 — `/pricing` Enterprise card spells out "percent" / "procent" / "Prozent" instead of `%` (artifact of the `%%` Jinja trap)
- **Where**: `app/templates/www/pricing.html:97`
- **Auto-fixable**: yes
- **status**: open
- **Description**: Enterprise card last bullet reads `SLA 99,9 procent` (CS), `SLA 99.9 percent` (EN), `SLA 99,9 Prozent` (DE). Awkward in all three languages. Shape suggests the developer hit the documented `%%` trap and avoided it by spelling out the unit.
- **Suggested fix**: Use the split-form pattern: `{{ _("SLA") }} 99,9 %` (literal `%` outside gettext). Re-extract + recompile. One msgid in each of three locales.

### [UX] F-UX-021 — Pricing in CZK only on EN + DE pages; no FX hint
- **Where**: `app/templates/www/pricing.html` EN+DE; ld+json `priceCurrency: "CZK"` on EN+DE homepage `Offer` blocks
- **Auto-fixable**: no
- **status**: open
- **Description**: An English/German prospect sees `490 CZK / month`. Has to mentally convert before deciding. Same for Google rich-result snippets — the structured data declares CZK in every locale. Editorial decision (founder bills CZK only); for first-impression UX a parenthetical EUR equivalent under each price card on EN+DE would dramatically reduce friction. Existing copy already references "with a Czech invoice" so CZK-only invoicing is communicated separately.
- **Suggested fix**: Add `<p class="text-xs text-slate-500">≈ €X / month (rate as of YYYY-MM-DD)</p>` under each priced card on EN+DE pages. Headline price stays CZK to avoid implying EUR invoicing. Skip changing the ld+json — Google's offer schema doesn't accept dual currencies cleanly.

### [UX] F-UX-022 — `robots.txt` disallows `/platform/signup`, blocking organic-search conversions
- **Where**: `https://assoluto.eu/robots.txt`
- **Auto-fixable**: yes
- **status**: open
- **Description**: Current `robots.txt` has `Disallow: /platform/signup`. Signup is the conversion endpoint — a Google search for "assoluto signup" / "assoluto try free" can't surface the page directly, and Google may down-rank the site overall because a conversion-relevant page is blocked.
- **Suggested fix**: Remove `Disallow: /platform/signup` from robots.txt source. Optionally add `<meta name="robots" content="noindex, follow">` to the signup HTML if you want crawlers to follow inbound links but not surface the page as its own search result. Keep `/platform/login` and `/platform/admin` disallowed.

### [BE] F-BE-004 — `gdpr_service` contact export/erase have no router — PERSISTS
- **Where**: `app/services/gdpr_service.py:102 / :233`; gap in `app/routers/me.py`
- **Auto-fixable**: yes (deferred)
- **status**: open
- **Description**: `me.py` still exposes only three routes — no `me/profile/export` or `me/profile/delete`. Customer contacts have no self-service GDPR path; staff get one, contacts don't.
- **Suggested fix**: unchanged from baseline.

### [BE] F-BE-005 — Stripe webhook handler set is narrow — PERSISTS
- **Where**: `app/platform/billing/webhooks.py:428` (`HANDLERS`)
- **Auto-fixable**: no
- **status**: open
- **Description**: 8 handlers unchanged. `customer.subscription.paused`, `customer.updated`, `payment_method.detached` still silently dropped.
- **Suggested fix**: unchanged from baseline.

### [BE] F-BE-006 — `_safe_error_summary` regex misses non-URL token vectors (also F-SEC-001)
- **Where**: `app/tasks/email_tasks.py:60`
- **Auto-fixable**: yes
- **status**: open
- **Description**: Sanitiser strips URLs and `key=value` blobs ≥12 chars. Does NOT redact tokens after a colon or whitespace (`Authorization: Bearer abcdef…`, `X-Token: abcdef…`, JWT-shape `eyJhbGciOi.payload.signature`). Realistic risk bounded — SMTP libraries usually echo only response codes, not body content — but the comment promises "must never leak", so the bar should match.
- **Suggested fix**: Add a third pattern. JWT-shape: `re.sub(r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}", "[jwt]", cleaned)`. Hex blob: `re.sub(r"\b[A-Fa-f0-9]{32,}\b", "[hex]", cleaned)`. Plus a few unit tests of the redactor.

### [BE] F-BE-007 — No test for the verify-gate on `select-tenant` / `switch` / `complete-switch`
- **Where**: `app/platform/routers/platform_auth.py:296 / :359 / :471`
- **Auto-fixable**: yes
- **status**: open
- **Description**: Commit `bfd1690` switched these three to `require_verified_identity`. The 403→303 redirect to `/platform/verify-sent` has no regression test. A future refactor that drops the dependency would silently regress.
- **Suggested fix**: Three small tests — POST `/platform/signup` (leaves identity unverified), then GET `/platform/select-tenant` → 303 to `/platform/verify-sent`; POST `/platform/switch/{slug}` → same; GET `/platform/complete-switch?token=…` → same.

### [BE] F-BE-008 — No test for the `/platform/billing/details` form
- **Where**: `app/platform/routers/billing.py:383 / :422`
- **Auto-fixable**: yes
- **status**: open
- **Description**: Commit `17a662d` added GET form + POST handler with IČO/DIČ/address validation + the gate redirect in `start_checkout` and `post_verify_checkout`. None of the four code paths has a test.
- **Suggested fix**: `tests/test_billing_details.py` — happy path, 4 validation branches, gate-redirect from `start_checkout` (need `STRIPE_SECRET_KEY` enabled to fire the gate).

### [BE] F-BE-009 — Recent feature commits without paired tests (advisory)
- **Where**: `17a662d`, `bfd1690`, `075a957`
- **Auto-fixable**: no
- **status**: open
- **Description**: Three of seven post-baseline commits ship visible behaviour with no test. F-BE-007 + F-BE-008 cover the two priority cases. The third (`075a957` comment-author batch lookup at `app/routers/orders.py:553`) has no assertion that the rendered template actually shows the resolved name. A template variable rename would silently degrade UX.
- **Suggested fix**: Add an assertion in an existing `test_orders_flow.py` lifecycle test that GET on the order detail page contains the commenter's full name once at least one comment exists. ~5 lines.

### [SEC] F-SEC-001 — `_safe_error_summary` regex misses bare token shapes
**(Already filed as F-BE-006 above — same root cause, same fix. Counted once in the totals.)**

---

## Comparison with previous run (`2026-05-01-1455`)

### Resolved (fixes held)
*From the round of /audit-fix work between baseline and this run, plus today's E2E batch:*
- F-UX-001 — `/terms` 500 in EN (gettext `%%` trap) — fixed in `efd4890`
- F-UX-002 — Two `<title>` per page — fixed
- F-UX-003 — CS contact double asterisk — fixed
- F-UX-005 — No hreflang alternates (markup ships; URL shape is the new F-UX-016)
- F-UX-006 — No language switcher — fixed
- F-UX-007 / F-UX-012 — CS+DE straight close-quote — fixed (`d914e3e` quote sweep)
- F-UX-008 — CS pricing English leak — fixed
- F-UX-009 — Contact form autocomplete — fixed
- F-UX-013 — Locale cookie host-only → `Domain=.assoluto.eu` — fixed (`d914e3e`)
- F-UX-014 — `/set-lang` HEAD 405 — fixed (broadened to F-UX-017 below)
- F-UX-015 — `SameSite` casing mismatch — fixed (`d914e3e`)
- F-BIZ-001 — backup retention drift — fixed
- F-BIZ-007 — 24 h SLA copy → 1 working day — fixed
- F-BIZ-009 — superlative on homepage — fixed
- F-BIZ-010 — annual billing on request copy — fixed
- F-BIZ-011 — 175 fuzzy EN entries (`10ce9bf` identity-catalog cleanup + 2 CI guards)
- F-SEC-001 (baseline) — SMTP error class-only log — addressed (with hardening room flagged below)

### Persisted (open in both runs)
- F-UX-004 — `/favicon.ico` 404 — P2, deferred
- F-UX-011 — No authenticated walkthrough — info, no creds
- F-BE-002 — Stripe price IDs NULL in prod (operator action)
- F-BE-003 — Zero test coverage for GDPR endpoints
- F-BE-004 — `gdpr_service` contact export/erase have no router
- F-BE-005 — Stripe webhook handler set is narrow
- F-BIZ-002 — status page URL (operator action)
- F-BIZ-003 — demo CTA → /contact, no Cal.com
- F-BIZ-004 — founder identity on `/` and `/contact`
- F-BIZ-005 — no trial-nurture cadence
- F-BIZ-006 — testimonial placeholders
- F-BIZ-008 — no refund-policy marketing copy

### Regressed
*None.* No previously-fixed finding came back this run.

### New in this run
- F-UX-016 (P1) — hreflang URLs all canonical (restated from F-UX-005/-010)
- F-UX-017 (P2) — HEAD 405 broadened (restated from F-UX-014)
- F-UX-018 (P2) — tenant login brand-colour drift
- F-UX-019 (P2) — contact form lacks honeypot
- F-UX-020 (P2) — `/pricing` SLA "percent" spell-out
- F-UX-021 (P2) — CZK-only on EN+DE pages
- F-UX-022 (P2) — robots.txt disallows /platform/signup
- F-BE-001 (P1) — billing-details mutation has no audit-trail entry
- F-BE-006 (P2) — `_safe_error_summary` regex coverage gap (= F-SEC-001)
- F-BE-007 (P2) — no verify-gate test on select-tenant / switch / complete-switch
- F-BE-008 (P2) — no test for `/platform/billing/details`
- F-BE-009 (P2) — feature commits without paired tests (advisory)
- F-BIZ-012 (P0) — homepage promises IČO at signup; signup form doesn't collect
- F-BIZ-013 (P1) — pricing FAQ doesn't disclose IČO/DIČ checkout gate
- F-BIZ-014 (P1) — SLA tier inversion (Pro 12 h vs Enterprise 24 h)

### Manual / operator action
- F-BE-002 — set `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` in `/etc/assoluto/env`
- F-BIZ-002 — status page URL
- F-BIZ-003 — demo CTA / Cal.com
- F-BIZ-004 — founder bio on /
- F-BIZ-005 — trial-nurture email cadence (product decision)
- F-BIZ-006 — testimonials (need real customer)
- F-BIZ-008 — refund policy copy (legal decision)

## Status legend

Each finding starts as `status: open`. The `/audit-fix` command updates this in place to `fixed`, `wontfix`, or `manual` (operator action required, not auto-fixable).

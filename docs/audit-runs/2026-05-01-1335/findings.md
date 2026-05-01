# Audit run 2026-05-01-1335

**Started**: 2026-05-01T13:35Z
**Tip-of-tree commit**: `a9d64e0`
**Previous run**: first run (no comparison available)

## Counts

| Perspective | P0 | P1 | P2 |
|---|---|---|---|
| UX        | 1 | 5 | 5 |
| Backend   | 1 | 2 | 4 |
| Security  | 0 | 1 | 2 |
| Business  | 1 | 5 | 4 |
| **Total** | **3** | **13** | **15** |

Two findings overlap across perspectives (counted once each, see notes):
* `[UX] F-UX-001` ≡ `[BE] F-BE-001` — `/terms` 500 in EN (gettext `%%` trap)
* `[BE] F-BE-003` ≡ `[SEC] F-SEC-002` — GDPR endpoints have zero test coverage

Unique findings: 29.

---

## P0 — must fix before next deploy

### [UX+BE] F-UX-001 / F-BE-001 — `/terms` returns HTTP 500 for English locale (gettext `%%` trap)
- **Where**: `app/templates/www/terms.html:111`; `app/locale/en/LC_MESSAGES/messages.po:5004` (msgstr empty)
- **Severity**: P0
- **Auto-fixable**: yes
- **Description**: §8 SLA paragraph contains `99.9% monthly uptime` directly inside the gettext call. Jinja's i18n extension runs `msg % {}`, Python sees `% m` and raises `ValueError: unsupported format character 'm'`. CS and DE msgstrs correctly escape as `99,9 %%`; EN msgstr is empty so gettext returns the unescaped msgid → 500. **Actively happening in production right now** — multiple `GET /terms HTTP/1.1 500` entries in last hour from public IP 93.99.225.146. Breaks signup conversion: every English visitor who clicks the mandatory Terms checkbox sees a JSON error page.
- **Suggested fix**: Use the split-form pattern per CLAUDE.md §7 — `{{ _("… targets ") }}99.9%{{ _(" monthly uptime …") }}` — at `app/templates/www/terms.html:111`. Re-extract+update+compile so the msgid is durably safe and EN keeps falling back to the literal text without `%%` issues.
- **Evidence**: `curl -H 'Accept-Language: en' https://assoluto.eu/terms` → 500. CS/DE → 200. Prod logs show the ValueError stack repeatedly.
- **status**: fixed (commit f87ae07)
### [BIZ] F-BIZ-001 — Backup retention drift: marketing says 14 days, script defaults to 30
- **Where**: `app/templates/www/pricing.html:133`; `index.html:424,428`; `terms.html` SLA clause; vs `scripts/backup.sh:11,26`
- **Severity**: P0
- **Auto-fixable**: yes
- **Description**: Pricing trust strip + both index FAQs + Terms ToS all commit to "14 days" backup retention. The script's documented default is `PORTAL_KEEP_DAYS=30`, and the rotation `find ... -mtime +30 -delete` will keep 30 days unless the operator overrides. If prod env doesn't pass `PORTAL_KEEP_DAYS=14`, we are storing more PII than the Privacy Policy + Terms commit to — a **GDPR Art. 5(1)(e) "storage limitation" exposure**, not just a marketing fib.
- **Suggested fix**: Change `KEEP_DAYS="${PORTAL_KEEP_DAYS:-30}"` → `:-14` in `scripts/backup.sh`. Verify `/etc/assoluto/env` either omits `PORTAL_KEEP_DAYS` or sets it to 14. SSH into prod and `ls -la /backups/portal-*.sql.gz*` to confirm only ≤14 files are present today.
- **Evidence**: Live marketing: `Denní zálohy — uchované 14 dní`. `scripts/backup.sh:26`: `KEEP_DAYS="${PORTAL_KEEP_DAYS:-30}"`. Terms SLA clause: `daily database backups retained for 14 days`.
- **status**: fixed (commit cda17c6)
---

## P1 — fix this sprint

### [BE] F-BE-002 — Stripe checkout silently no-ops in production (price IDs missing in `/etc/assoluto/env`)
- **Where**: prod env `/etc/assoluto/env`; affects `app/main.py:42 _sync_stripe_prices_from_env` → `app/platform/billing/service.py create_checkout_session`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **Description**: `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` keys exist in env but values are empty strings. `_sync_stripe_prices_from_env` filters out empties and no-ops, so all four `platform_plans` rows have `stripe_price_id IS NULL`. Any user clicking "Upgrade" hits the early-exit and stares at a silent failure. Container is on tag `1b1c8f9`, restarted ~1h ago — this is the live state.
- **Suggested fix**: Set actual price IDs in `/etc/assoluto/env` from the Stripe dashboard, then `docker compose ... restart web`. Defensive code change: have the boot path log `stripe_price.sync.no_env` (info) when both env vars empty so silent skip becomes visible.
- **Evidence**: `psql -c "SELECT code, stripe_price_id FROM platform_plans"` → all four NULL. `app/main.py:61` filters empties; `app/main.py:62-63` early returns.
- **status**: manual — Operator action — set STRIPE_PRICE_STARTER / _PRO in /etc/assoluto/env from the live Stripe dashboard, restart web. Code change not applicable.
### [BE+SEC] F-BE-003 / F-SEC-002 — Zero test coverage for GDPR endpoints
- **Where**: `app/routers/tenant_admin.py:595` (export), `:617` (delete); `app/services/gdpr_service.py`
- **Severity**: P1
- **Auto-fixable**: yes (writing the tests)
- **Description**: GDPR Art. 15/17/20 endpoints exist (export-for-user JSON, password-confirmed soft-erase that anonymises PII while preserving order/audit history) and are linked from the staff profile page, but `grep -rln "profile/export\|profile/delete\|gdpr_service" tests/` returns zero hits. These are precisely the routes that need regression coverage: the erase path mutates state (nulls email/full_name, bumps session_version, writes audit, blocks last-admin self-erase), and a silent regression here is a regulator-visible event.
- **Suggested fix**: Add `tests/test_gdpr_flow.py` covering: (a) export returns 200 with correct `Content-Disposition` + JSON body; (b) export does NOT include other tenants' data (RLS smoke); (c) delete with wrong password redirects with `?error=` flash and does NOT mutate; (d) delete as last admin returns "promote someone else first" and does NOT mutate; (e) successful erase nulls PII, bumps session_version, writes audit, clears session cookie.
- **Evidence**: `grep -rln "profile/export\|profile/delete\|gdpr_service" tests/` → empty.
- **status**: manual — Deferred — substantive new test code (5+ test cases with RLS smoke); will be a focused follow-up commit, not a one-line fix.
### [BE] F-BE-004 — `gdpr_service` exports `export_for_contact` / `erase_contact` but no router exposes them
- **Where**: `app/services/gdpr_service.py:102, 233`; gap in `app/routers/me.py`
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: Customer contacts are data subjects under GDPR exactly the same as staff users; the service layer correctly implements both export and soft-erase for them — but the only routes that expose them require `require_tenant_staff`. A customer contact emailing the operator asking for their data has no self-service path; the operator has to run an ad-hoc psql / Python script. Compliance gap for a regulated EU SaaS.
- **Suggested fix**: Add two endpoints to `app/routers/me.py`: `GET /app/me/profile/export` (calls `export_for_contact`) and `POST /app/me/profile/delete` (password-confirmed, calls `erase_contact`, clears session, redirects to public landing with notice). CTAs in `app/templates/me/profile.html`. Cover both with the test suite from F-BE-003.
- **Evidence**: `grep -n "@router\." app/routers/me.py` returns only the three existing routes.
- **status**: manual — Deferred — adding /app/me/profile/{export,delete} routes is a real feature with its own test surface; not appropriate for an auto-fix batch.
### [UX] F-UX-002 — Every marketing page renders TWO `<title>` tags
- **Where**: every marketing page + tenant login (`/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies`, `/imprint`, `test-a.assoluto.eu/auth/login`)
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: Two `<title>` elements in `<head>`. First is the real page title; second is `Assoluto — assoluto.eu trademark; see NOTICE.md before forking.`. Browsers pick the first for the tab but the markup is invalid HTML and confuses scrapers, OG validators, social previews, SEO tools.
- **Suggested fix**: In the base template, demote the trademark notice to either an HTML comment `<!-- ... -->` or `<meta name="trademark" content="...">`. Keep one `<title>` per page.
- **Evidence**: `grep -c '<title>' /tmp/home_cs.html` → `2`.
- **status**: fixed (commit 8707f21)
### [UX] F-UX-003 — Czech contact form shows double asterisk on the message label
- **Where**: `https://assoluto.eu/contact?lang=cs` — message field label
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: "Your message" label in CS renders as `Vaše zpráva *  *` (two red asterisks). Cause: `app/locale/cs/LC_MESSAGES/messages.po:3017` has `msgstr "Vaše zpráva *"` (asterisk baked in), template appends a second `<span class="text-rose-500">*</span>`. EN/DE render correctly.
- **Suggested fix**: Edit `app/locale/cs/LC_MESSAGES/messages.po:3017` — change `msgstr "Vaše zpráva *"` → `msgstr "Vaše zpráva"`. Recompile catalogs.
- **Evidence**: rendered HTML shows `<label …>Vaše zpráva * <span class="text-rose-500">*</span></label>`.
- **status**: fixed (commit 82d5458)
### [UX] F-UX-005 — No `<link rel="alternate" hreflang>` between CS/EN/DE
- **Where**: every marketing page
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: Same canonical URL serves three languages by `Accept-Language`. Without `hreflang`, Google indexes whichever locale Googlebot is configured for and deduplicates the rest. CZ-language SEO suffers because Googlebot from US data centres lands on EN 500-error pages right now; even after F-UX-001 is fixed, search engines still won't surface the right locale.
- **Suggested fix**: In `app/templates/www/www_base.html` add `<link rel="alternate" hreflang="cs|en|de|x-default" href="https://assoluto.eu{{ request.url.path }}">` block. Add `<meta property="og:locale:alternate">` for the other two locales.
- **Evidence**: `grep 'hreflang\|og:locale:alternate' /tmp/home_*.html` → empty.
- **status**: fixed (commit 8707f21)
### [UX] F-UX-006 — No visible language switcher on marketing site
- **Where**: every marketing page; tenant login pages
- **Severity**: P1
- **Auto-fixable**: no (UX/design choice)
- **Description**: Site detects locale from `Accept-Language` only — no globe / language dropdown in the header. CZ-headquartered SaaS targeting CZ/EN/DE markets: a German prospect on a Czech browser sees the Czech homepage and bounces; same prospect in a meeting room with a colleague trying to show the English version cannot do it via UI.
- **Suggested fix**: Add a minimal CS/EN/DE switcher in the header (or footer minimum) of `app/templates/www/www_base.html`. Persist via a cookie that overrides `Accept-Language`. Keep URL same (don't switch to `/en/...` paths).
- **Evidence**: `grep -iE 'switch.*lang|hreflang|locale-switch' /tmp/home_*.html` → empty.
- **status**: manual — Design choice — language-switcher placement, persistence semantics. Defer to a focused UX pass.
### [BIZ] F-BIZ-003 — Demo CTA goes to `/contact` form, not a real booking link
- **Where**: `app/templates/www/index.html:45` ("Book a 15-min demo"); `contact.html:43-46`
- **Severity**: P1
- **Auto-fixable**: no (operator action; needs Calendly/Cal.com)
- **Description**: Hero CTA "Book a 15-min demo" links to `/contact`, which is a passive form with the instruction "Write 'demo' in the message". For B2B SME sales the gap between a CTA promising calendar booking and reality (free-text form + the founder having to email back) is a measurable conversion-rate killer.
- **Suggested fix**: Operator action: stand up a free Cal.com event (15-minute Google Meet), put the URL behind both the homepage CTA and the contact-page demo card. Until then, change "Book a 15-min demo" → "Ask for a 15-min demo".
- **Evidence**: `index.html:45` link is `href="/contact"`; `contact.html:43-46` demo card text matches.
- **status**: manual — Operator action — stand up Cal.com event, paste URL into hero CTA + contact-page demo card.
### [BIZ] F-BIZ-004 — No founder identity on homepage / contact / footer (only buried in `/imprint`)
- **Where**: `www_base.html` (footer), `index.html`, `contact.html`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: Václav Mudra's name appears only on the legally-required `/imprint` page. Contact page says "Czech team" / plural fictional team. For a 50–200 EUR/mo B2B SaaS, founder-led trust outperforms generic-team copy. Pricing FAQ says "real people, not a chatbot" but the marketing surface offers no real person.
- **Suggested fix**: Add a small founder card to `contact.html` aside (photo + 2 sentences + LinkedIn link) and a one-line "Built by Václav Mudra in Děčín, CZ" above footer legal links. Drop "Czech team" plural framing — say "Czech support, run by Václav personally" until there's a second person.
- **Evidence**: `contact.html:64-66` says "Czech team"; imprint shows real founder data; footer is legal-only.
- **status**: manual — Operator action — write 2-sentence founder bio + photo + LinkedIn link; copy edit on contact page.
### [BIZ] F-BIZ-005 — No trial-nurture email cadence exists
- **Where**: `app/tasks/email_tasks.py`, `app/tasks/periodic.py`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: 30-day trial without day-1 ("here's how to invite your first client"), day-7 ("how's it going? what most shops set up next"), and day-25 ("trial ends in 5 days") emails leaves money on the table and surprises users with the day-30 cancel. Industry expectation for SME SaaS: at minimum a day-25 reminder so there's no "you charged me without warning" complaint. Currently the only trial-aware periodic is `expire_demo_trials` which cancels — punitive end, no welcoming side. **Single biggest activation/conversion lever you currently lack.**
- **Suggested fix**: Add `send_trial_welcome` (fired from signup), `send_trial_nudge` (periodic; once at day 7 if user has 0 orders), `send_trial_ending_reminder` (periodic; at trial-end - 5 days). Persist `last_nurture_step_sent_at` on `platform_subscriptions` to avoid double-sends.
- **Evidence**: No `send_trial_*` / `send_nurture_*` / `send_welcome_*` defined. Only periodic that knows trial dates is `expire_demo_trials`.
- **status**: manual — Substantive feature work (3 new email templates + state column on platform_subscriptions + periodic-job hooks). Standalone follow-up.
### [BIZ] F-BIZ-002 — Status page promise is conditional, but `STATUS_PAGE_URL` not set in prod
- **Where**: `app/templates/www/pricing.html:145`
- **Severity**: P1
- **Auto-fixable**: no (operator action)
- **Description**: Live curl returns the fallback "UptimeRobot 24/7, status page na vyžádání." `settings.status_page_url` is empty in production env. Marketing copy degrades gracefully, so not a P0 broken promise — but the index FAQ #5 still says "monitor uptime 24/7" without conditional, and the pricing trust strip's promise of "real" monitoring reads as "we're not quite there yet."
- **Suggested fix**: Operator action: create UptimeRobot public status page (~10 min), copy URL into `/etc/assoluto/env` as `STATUS_PAGE_URL=https://stats.uptimerobot.com/<token>`, redeploy. No code change.
- **Evidence**: Live prod fallback string. `app/config.py`: `status_page_url: str = Field(default="", ...)`.
- **status**: manual — Operator action — create UptimeRobot public status page, set STATUS_PAGE_URL in /etc/assoluto/env.
### [BIZ] F-BIZ-007 — "Czech support reply within 24h" promise has no SLA tracker / autoresponder
- **Where**: `app/templates/www/contact.html:32, 58, 77`
- **Severity**: P1
- **Auto-fixable**: yes (copy edit) + operator action (autoresponder)
- **Description**: Three copies on the contact page promise "Typical reply within 24 hours" / "We reply within 24 hours" / "Thanks — we will reply within 24 hours." No autoresponder, no helpdesk. As a single-founder operation this commits Václav to checking email 7 days a week; for weekends/holidays/travel the promise will break the first time a Friday-evening prospect gets a Monday-morning reply. The contact-page subhead actually says "within one working day" — inner cards over-promise vs subhead.
- **Suggested fix**: Edit the three `24 hours` strings → `Reply within one working day`. Set up Brevo / SMTP autoresponder for `team@assoluto.eu` confirming receipt + restating the working-day SLA.
- **Evidence**: `contact.html:17` (subhead) vs `:32, :58, :77` (cards) — subhead says "one working day", cards say "24 hours".
- **status**: fixed (commit 82d5458) — copy aligned; SMTP autoresponder still operator action
---

## P2 — backlog

### [UX] F-UX-004 — `/favicon.ico` returns 404
- **Where**: `https://assoluto.eu/favicon.ico` (and every subdomain)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Modern browsers find the icon through `<link rel="icon" type="image/svg+xml">` (works) but Safari, RSS readers, link-preview bots, browser bookmark exporters, and the literal `/favicon.ico` URL all 404. Console shows "404 favicon.ico" on every homepage tab.
- **Suggested fix**: Either (a) add a static `/favicon.ico` (16x16 / 32x32 multi-resolution) under `app/static`, or (b) add Caddy/route alias mapping `/favicon.ico` → `/static/favicon.svg`.
- **Evidence**: `curl -o/dev/null -w '%{http_code}' https://assoluto.eu/favicon.ico` → 404.
- **status**: manual — Operator action — generate a multi-resolution favicon.ico (16x16/32x32) and drop into app/static/. Or wire a Caddy alias.
### [UX] F-UX-007 — CS and DE body quotes use straight ASCII closing quote (`"`) instead of typographic close
- **Where**: homepage CS+DE testimonials, pull-quotes, "stop picking up the phone" cluster; likely on `/features`, `/contact` too
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Czech and German typography pair `„` (low-9 open) with `"` (curly close). Current copy opens with `„` and closes with straight ASCII `"`. Looks unpolished to native readers — a tell that copy was machine-translated.
- **Suggested fix**: Sweep `app/locale/cs/LC_MESSAGES/messages.po` and `de/.../messages.po` for `„[^"]*"` and replace trailing `"` with `"`. Same in any inline CZ/DE template strings. (EN keeps `"..."` straight quotes.)
- **Evidence**: 7 unique mismatched-quote phrases in `/tmp/home_cs.html`; same in DE.
- **status**: fixed (commit 82d5458)
### [UX] F-UX-008 — Pricing card "Co obsahuje každý plán" mixes Czech and English
- **Where**: `https://assoluto.eu/pricing` (CS) — "What every plan includes" panel below the four plan cards
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: CS pricing page has a feature panel with Czech labels but English fragment values: `Data v EU — Hetzner DE, encrypted at rest.` Bare English in a Czech sentence. Native CZ reader notices immediately; signals "translated dev tool, not localised software."
- **Suggested fix**: In CS msgstr (or `pricing.html` if hardcoded), translate to `Hetzner DE, šifrováno v klidu.` Scan DE — same panel may have analogous leak.
- **Evidence**: `pricing_cs.html:304` shows the bare English fragment.
- **status**: fixed (commit 82d5458)
### [UX] F-UX-009 — Contact form name/email inputs missing `autocomplete` attributes
- **Where**: `https://assoluto.eu/contact` (all locales)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Signup form sets `autocomplete="organization|name|username|new-password"` properly (kudos), but `/contact` doesn't. Visitors with browser-saved profiles can't one-click-fill name/email.
- **Suggested fix**: In `app/templates/www/contact.html` add `autocomplete="name"` to name input and `autocomplete="email"` to email input.
- **Evidence**: `grep autocomplete /tmp/contact_cs.html` → no matches inside `<form>`.
- **status**: fixed (commit 8707f21)
### [UX] F-UX-010 — Sitemap.xml does not declare hreflang alternates
- **Where**: `https://assoluto.eu/sitemap.xml`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Pairs with F-UX-005. Even after adding `<link rel="alternate">` in page `<head>`, search engines benefit from same signal in sitemap via `<xhtml:link rel="alternate" hreflang="...">` per `<url>`. Current sitemap (9 URLs, single-locale) doesn't.
- **Suggested fix**: Update sitemap generator (`app/routers/www.py:248-277`) to emit per-locale alternates per entry, plus `x-default`.
- **Evidence**: `curl -s https://assoluto.eu/sitemap.xml` shows plain `<url><loc>` with no `xhtml:link` siblings.
- **status**: fixed (commit 8707f21)
### [UX] F-UX-011 — No authenticated walkthrough of `/app` (credentials not provided)
- **Where**: `/app`, `/app/orders`, `/app/customers`, `/app/products`, `/app/admin/*`, `/platform/admin/*`
- **Severity**: P2 (informational — not a defect)
- **Auto-fixable**: no
- **Description**: This audit could not log in and walk the inner customer portal or platform admin pages. New platform admin subscription editor (`1b1c8f9`), Plan/Billing status/Period ends columns on `/platform/admin/tenants`, in-app dark-mode regressions cannot be verified without browser session + operator credentials.
- **Suggested fix**: For next `/audit` run, drop a one-shot operator credential via `AUDIT_PLATFORM_EMAIL` / `AUDIT_PLATFORM_PASSWORD` env vars (revoke after). Or stand up an `audit-ux` ephemeral identity per run.
- **Evidence**: `curl https://test-a.assoluto.eu/app` → 401. No credentials available.
- **status**: manual — Informational — needs operator credentials for the next /audit-verify run.
### [BE] F-BE-005 — Recent feature commits land without paired test changes (advisory)
- **Where**: commits `1b1c8f9` (subscription editor — +226 lines, 0 tests) and `884508b` (verify-gate + honeypot — +88 router lines, +58 service lines, 0 tests)
- **Severity**: P2
- **Auto-fixable**: no (process / discipline)
- **Description**: Two recent business-logic commits ship substantive new code paths and zero test additions. Subscription editor with plan-swap, trial/period-end edits, state auto-correct, quick-action buttons — none exercised by `tests/test_billing.py`. `_has_unverified_identity` lookup, `UnverifiedIdentity` exception path, public `POST /platform/check-email`, honeypot — never asserted. Both features user-visible and security-adjacent.
- **Suggested fix**: Three small tests as follow-up — (1) `test_subscription_editor.py` happy-path POST + audit row, (2) `set_active` quick action, (3) `test_signup.py` honeypot-tripped POST asserts no Identity/Tenant/User created + `signup.honeypot_tripped` log emitted (use `caplog`).
- **Evidence**: `git show --stat 1b1c8f9 884508b` — neither lists any `tests/` file.
- **status**: manual — Process discipline, not a code change.
### [BE] F-BE-006 — Stripe webhook handler set is narrow (no `payment_method.detached` / `customer.updated` / `customer.subscription.paused`)
- **Where**: `app/platform/billing/webhooks.py:428` `HANDLERS` registry
- **Severity**: P2
- **Auto-fixable**: no (depends on product policy)
- **Description**: Three events worth considering: `customer.subscription.paused` (Stripe lets customer self-pause via portal — not seeing it = drift), `customer.updated` (billing address / VAT-ID change = next invoice has stale data), `payment_method.detached` (only saved card removed = "no payment method" banner before next renewal would prevent silent past_due → cancel).
- **Suggested fix**: At minimum add `customer.subscription.paused` because the cancel-flow trust story breaks if Stripe self-service users can pause without our DB knowing.
- **Evidence**: `webhooks.py:428-437` enumerates 8 handlers; no handler for the three listed.
- **status**: manual — Product policy decision — which Stripe events to handle (paused / customer.updated / payment_method.detached). Defer to operator product call.
### [BE] F-BE-007 — Mypy / ruff / pytest baseline established (informational)
- **Where**: tooling outputs
- **Severity**: P2 (informational — establishing baseline for `/audit-verify`)
- **Auto-fixable**: n/a
- **Description**: First-run baseline: mypy 0 errors / 87 files, ruff clean, ruff format clean (147 files), pytest 423 passed in 58.97s (under 60s budget but close). 12 test warnings — all third-party slowapi `asyncio.iscoroutinefunction` deprecation. RLS / tenant isolation tests pass. Architecture invariants validated: lock IDs unique, public routes use `read_session_for_tenant`, every `background_tasks.add_task` preceded by explicit `await db.commit()`, schema vs ORM drift on heavy tables → none. Migration chain clean (head `1006_drop_starter_orders_cap`).
- **Suggested fix**: When `/audit-verify` runs, baseline these counts and flag any of: mypy >0, ruff non-clean, ruff format drift, pytest non-green, pytest >60s, new test warning categories, new lock_id collision.
- **Evidence**: command outputs in `backend.md`.
- **status**: manual — Informational baseline — captured for /audit-verify regression detection.
### [SEC] F-SEC-001 — `Server: uvicorn` and `Via: 1.1 Caddy` headers leak internals
- **Where**: production reverse proxy → uvicorn; surfaces on every response
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: `Server: uvicorn` tells an attacker the app is FastAPI/Starlette. `Via: 1.1 Caddy` tells them the reverse-proxy software. Neither exploitable on its own — but standard hardening hides them so vuln scanners can't trivially auto-target known CVEs in the stack.
- **Suggested fix**: In Caddyfile fronting the app, add `header -Server` and `header -Via`. uvicorn can also launch with `--no-server-header` for defence-in-depth.
- **Evidence**: `curl -sI https://assoluto.eu/` returns both headers (verified live).
- **status**: fixed (commit cb53240) — required manual Caddy image rebuild on prod (`docker compose build caddy && up -d caddy`); the deploy workflow only rebuilds the `web` image. Followup: add Caddy rebuild step to `.github/workflows/deploy-production.yml` so future Caddyfile edits propagate without SSH.
### [SEC] F-SEC-003 — `_has_unverified_identity` opens a fresh engine per failed login
- **Where**: `app/services/auth_service.py:85`
- **Severity**: P2
- **Auto-fixable**: no (refactor — needs lifespan-managed singleton)
- **Description**: Every wrong-password login attempt against an account with an Identity triggers a brand-new `create_async_engine` + connect + dispose cycle against the **portal-owner DSN** (RLS bypass). asyncpg engines are cheap-ish but not free; under credential-stuffing burst this fans out owner-DSN connections beyond pool ceiling. Worse: keeping the owner-DSN touched on the hot login path means a bug here could expose owner-level connection state to tenant request scope. Fails open on exception so not a confidentiality bug today, but the architecture is fragile.
- **Suggested fix**: Create the platform-lookup engine once in `app.main.lifespan` (or `app/platform/__init__.py:install`), stash on `app.state.platform_lookup_engine`, have `_has_unverified_identity(request, email)` pull from there. Disposal moves to lifespan shutdown.
- **Evidence**: `auth_service.py:85` — `engine = create_async_engine(...)` then `engine.dispose()` per call.
- **status**: manual — Refactor — needs lifespan-managed singleton engine. Standalone follow-up, not auto-fixable.
### [BIZ] F-BIZ-006 — Testimonial figcaptions present but no logos / first-customer story
- **Where**: `app/templates/www/index.html:358-401`
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Testimonials section is honest about pre-launch ("Logos will appear here as soon as clients agree to be published") but already shows three styled quote cards with attribution like "Owner, metalwork shop — Morava, 12 employees". A skeptical SME prospect reads "Early access" + ungrounded quotes as fabricated — worse than no testimonials.
- **Suggested fix**: Delete the three placeholder figure cards, replace with one large "Why I'm building Assoluto" founder quote + photo. Keep "Early access" badge. Plan to swap to real customer quote once first paying customer signs.
- **Evidence**: `index.html:367` says "Logos will appear here..."; quote 3 attributed to fictional Prague co-owner.
- **status**: manual — Operator decision — write founder note OR delete placeholder testimonials. Copy + design call.
### [BIZ] F-BIZ-008 — No refund policy stated; pricing FAQ silent on refunds
- **Where**: `app/templates/www/pricing.html` FAQ block (`:154-170`); `app/platform/billing/webhooks.py` (`charge.refunded` handler exists)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Webhook handler `handle_charge_refunded` exists — system is wired to receive Stripe refund events. But no marketing copy or Terms tells the customer when/why we'd issue a refund (or won't). "What happens when I cancel?" FAQ explains data export but is silent on whether the in-progress month is refunded. CZK SME owners read absence as "they'll keep my money."
- **Suggested fix**: Add one explicit refund-policy line to the cancel FAQ in both `pricing.html` and `index.html`: "Cancelling stops the next charge; the current month is not prorated. For accidental double-charges or annual-plan refund requests, email team@assoluto.eu."
- **Evidence**: `webhooks.py` has `"charge.refunded": handle_charge_refunded`; `pricing.html:167` cancel FAQ silent on refunds.
- **status**: manual — Operator copy decision — define the actual refund policy first, then add the FAQ line.
### [BIZ] F-BIZ-009 — Two unverified superlative claims in homepage feature copy
- **Where**: `app/templates/www/index.html:152`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: "Five calls a day turn into zero" presented as feature outcome (not customer quote), so it's an unsubstantiated outcome promise. Should be hedged or moved into testimonials block where attribution makes it credible.
- **Suggested fix**: Edit `index.html:152` `Five calls a day turn into zero.` → `Most "where's my order?" calls go away on their own.`
- **Evidence**: `index.html:152` quotes the unconditional claim.
- **status**: fixed (commit 82d5458)
### [BIZ] F-BIZ-010 — Annual-billing offer copy correct but inconsistent across surfaces
- **Where**: `app/templates/www/pricing.html:113-115, 165`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Pricing-page annual callout says "typically two months free" (~16 % discount). Pricing FAQ #2 says "Annual plan: arranged on request — bank transfer with a Czech invoice, net-14" — silent on the discount. Index FAQ "Can I pay by bank transfer" also silent. Prospect comparing pages may wonder if discount is real.
- **Suggested fix**: Add `(typically two months free for the year)` to the FAQ answers in both `pricing.html:165` and `index.html` "Can I pay by bank transfer" answer.
- **Evidence**: `pricing.html:114` mentions discount; `:165` doesn't.
- **status**: fixed (commit 82d5458)
---

## Comparison with previous run

First run — no comparison available. The next `/audit-verify` invocation will diff against this one and mark each subsequent finding as `resolved` / `persisted` / `regressed` / `new`.

---

## Status legend

Each finding starts as `status: open`. The `/audit-fix` command updates this in place to:
* `fixed` (with the commit SHA) — auto-fix succeeded, deployed.
* `wontfix` — operator decision, with rationale.
* `manual` — needs operator action (config change, copy decision, design choice) — not auto-fixable.

Run `/audit-fix` to apply the auto-fixable findings in this run.

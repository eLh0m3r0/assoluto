# UX audit (verification run) — 2026-05-09-0829

Run mode: HTTP-only walk via curl + raw HTML inspection (Chrome MCP
tools were not available again — searched both `select:` and keyword
forms; no `mcp__claude-in-chrome__*` tools surfaced). This is the 3rd
consecutive run; HTTP-only mode is reliable enough that the missing
visual sweep is logged but no longer surprises me. Status of every
previous finding is tracked below; new issues caught this round are
listed after.

## Verification status of previous findings

| ID | Title | Previous severity | Status | Notes |
|---|---|---|---|---|
| F-UX-001 | `/terms` 500 in EN | P0 | **resolved** | Stable across two runs. |
| F-UX-002 | Two `<title>` per page | P1 | **resolved** | All 27 marketing URLs render exactly one `<title>`. |
| F-UX-003 | CS contact double asterisk | P1 | **resolved** | Single asterisk confirmed CS+EN+DE. |
| F-UX-004 | `/favicon.ico` 404 | P2 | **deferred** (still 404). |
| F-UX-005 | No hreflang alternates | P1 | **partially resolved** — the markup ships, but every alternate URL is identical to the canonical (see new F-UX-016). |
| F-UX-006 | No language switcher | P1 (bonus) | **resolved** — switcher in header + footer, persists via cookie. |
| F-UX-007 / F-UX-012 | CS+DE straight close-quote | P2 | **resolved** — counter shows 8 typographic `”` and 0 ASCII `"` per locale. |
| F-UX-008 | CS pricing English leak | P2 | **resolved**. |
| F-UX-009 | Contact form autocomplete | P2 | **resolved**. |
| F-UX-010 | Sitemap hreflang | P2 | **partially resolved** — same defect as F-UX-016: 36 alternates ship but each just repeats the canonical URL. |
| F-UX-011 | No authenticated walkthrough | info | **deferred** — no creds. |
| F-UX-013 | Locale cookie host-only | P2 | **resolved** — `Domain=.assoluto.eu` set; verified the DE choice on apex now flips `test-a/auth/login` title from "Přihlásit se" to "Anmelden". |
| F-UX-014 | `/set-lang` HEAD 405 | P2 | **resolved for `/set-lang`, persists everywhere else** — see new F-UX-017. |
| F-UX-015 | `SameSite` casing mismatch | P2 | **resolved** — both `sme_locale` and `csrftoken` now use `SameSite=lax`. |

Net: 11 fixes verified, 2 partial (F-UX-005/010 grouped into F-UX-016,
and F-UX-014 broadened into F-UX-017), 1 P2 deferred (favicon), 1 info
deferred (no creds). 4 brand-new findings filed below.

## New findings this run

### F-UX-016 — hreflang alternates all point to the same URL → invalid per Google's spec
- **Where**: `https://assoluto.eu/`, `/pricing`, `/features`, `/self-hosted`, `/contact` (and `sitemap.xml` mirrors the same defect)
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: F-UX-005's fix added `<link rel="alternate" hreflang="cs|en|de|x-default">` to every marketing page, but **every alternate `href` is the canonical URL itself** — e.g. on `/pricing` all four alternates resolve to `https://assoluto.eu/pricing`. The site uses cookie + `Accept-Language` content negotiation rather than per-locale URLs, so there's no separate URL to point to. Google's hreflang documentation requires distinct URLs per language. The current shape signals "all three languages at the same URL" which is what `x-default` already conveys; the per-locale entries become noise that Google's Search Console flags as invalid hreflang. SEO consequence: the CS, EN, DE versions never get separately indexed and ranked for the right markets.
- **Suggested fix**: Two viable shapes — (a) **subpath locales** `/cs/pricing`, `/en/pricing`, `/de/pricing` with each rendering only that language and the canonical pointing to itself; or (b) **query-string locales** `/pricing?lang=en` etc., similar pattern. Option (a) is the cleaner long-term shape; option (b) is faster to ship if you keep the existing single-handler-per-page architecture and just key the rendered locale off the query string when present (cookie still wins when the param is absent). Update `sitemap.xml` correspondingly. Keep the existing language switcher; it just needs to swap its href targets.
- **Evidence**: `grep "hreflang" /tmp/2026-05-09-home_cs.html` → all 4 alternates `href="https://assoluto.eu/"`. Same shape on EN/DE pages. `sitemap.xml` has 36 `xhtml:link` entries; spot check at `/features` shows 4 links all pointing to `https://assoluto.eu/features`.

### F-UX-017 — HEAD returns 405 on every public GET endpoint except `/set-lang`
- **Where**: `https://assoluto.eu/`, `/pricing`, `/features`, `/contact`, `/terms`, `/sitemap.xml`, `https://test-a.assoluto.eu/auth/login`, `/auth/password-reset`, `https://assoluto.eu/platform/login`, `/platform/signup`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: F-UX-014's fix patched `/set-lang` to honour HEAD (it now returns 303 with empty body). But the underlying defect — FastAPI/Starlette routes only register the verb you declare, never auto-deriving HEAD from GET — was not fixed framework-wide. Every other GET endpoint still returns `HTTP 405 Allow: GET` to a HEAD probe. RFC 9110 requires HEAD for every GET; the practical impact today is noise in uptime monitors (UptimeRobot defaults to HEAD for HTTP/2 monitors), failed link-checkers, and false alarms in security scanners that use HEAD as a cheap reachability test. Marketing pages are the most-monitored surfaces, so the noise is concentrated where it matters.
- **Suggested fix**: Either (a) add `methods=["GET", "HEAD"]` to every page-rendering route — repetitive but explicit; or (b) add a small ASGI middleware in `app/main.py` that intercepts HEAD requests, dispatches them as GET internally, and strips the body before the response is sent. Option (b) is one ~15-line middleware that fixes every current and future GET route in one shot. Make sure the middleware sits **after** Starlette's routing so it can fall back to the per-route registration for endpoints that genuinely don't support HEAD (POST-only forms etc.).
- **Evidence**: `for url in /, /pricing, /features, /contact, /terms, /sitemap.xml, /auth/login (test-a), /platform/login, /platform/signup: curl -I … → 405`. `/set-lang -I → 303`.

### F-UX-018 — Tenant `/auth/login` still uses `bg-blue-600` while marketing + platform login use `bg-brand-600`
- **Where**: `https://test-a.assoluto.eu/auth/login`, `/auth/password-reset`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Marketing pages and platform-apex auth pages have been migrated to the `brand-*` colour palette (`bg-brand-600`, `bg-brand-700`, focus-ring `brand-500`). The tenant subdomain `/auth/login` template still uses generic `bg-blue-600/700/800`, plus `text-blue-600`/`text-blue-400` for the "Forgot password?" link. A prospect who clicks "Vyzkoušet" on the marketing page, signs up, lands on their new subdomain `acme.assoluto.eu/auth/login` — the brand colour suddenly shifts. Small thing but a brand consistency drift on a visually-prominent surface (the primary CTA button).
- **Suggested fix**: Edit `app/templates/auth/login.html` (and `password_reset.html`): replace `bg-blue-600/700/800` → `bg-brand-600/700`, `text-blue-600`/`text-blue-400` → `text-brand-600`/`text-brand-400`, `focus:ring-blue-500/20` → `focus:ring-brand-500/20`. About a dozen single-token swaps per file.
- **Evidence**: `grep -oE "bg-(blue|brand)-[0-9]+" /tmp/2026-05-09-test-a-login.html` → 3× `bg-blue-*`, 0× `bg-brand-*`. Same file on `/tmp/2026-05-09-platform_login.html` → 0× blue, 2× brand.

### F-UX-019 — Contact form lacks the honeypot the platform signup uses; the rate-limit (5/15min) is the only bot defence
- **Where**: `https://assoluto.eu/contact` POST handler (`app/routers/www.py:92-176`)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: `/platform/signup` ships a hidden `name="website"` honeypot input (off-screen, `tabindex="-1"`, `autocomplete="off"`) and the backend rejects submissions where it's filled — the canonical bot trap. The contact form has no equivalent. Its only defence is a 5-per-15-minutes rate-limit (`@rate_limit("5/15 minutes")`) which is per-IP after the `TRUSTED_PROXIES` correction landed, but that doesn't help against distributed cheap-bot traffic. The brief explicitly says "we don't add to the wave of fake signups" — same logic should apply to the contact form, since contact-form submissions email the founder directly and waste time.
- **Suggested fix**: Mirror the platform/signup honeypot in `app/templates/www/contact.html`: add a hidden `<div aria-hidden="true" style="position:absolute;left:-10000px;...">` wrapping `<input type="text" name="website" tabindex="-1" autocomplete="off">`. In `app/routers/www.py:contact_submit`, accept `website: str = Form("")` and silently 200 (or 204) without sending email when it's non-empty. Keep the rate-limit; layered defence.
- **Evidence**: `grep -i honeypot /tmp/2026-05-09-contact_cs.html` → no match; `grep "name=\"website\"" /tmp/2026-05-09-platform_signup.html` → present.

### F-UX-020 — `/pricing` Enterprise card spells out "percent" / "procent" / "Prozent" instead of using `%` (artifact of the `%%` Jinja trap)
- **Where**: `https://assoluto.eu/pricing` (CS, EN, DE) — Enterprise card, "SLA 99,9 Prozent" line; source: `app/templates/www/pricing.html:97`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The Enterprise plan's last bullet reads `SLA 99,9 procent` (CS), `SLA 99.9 percent` (EN), `SLA 99,9 Prozent` (DE). Spelling out the unit is awkward in all three languages — every other SLA bullet on the web uses `%`. The shape strongly suggests the developer wrote `99.9 %` first, hit the documented `%%` trap (`ValueError: unsupported format character`), and avoided it by spelling out the word. Per the rule in `CLAUDE.md` §7, the canonical fix is to escape with `%%` in both msgid and msgstr, or — easier — split the value out so it isn't inside a gettext string at all.
- **Suggested fix**: In `app/templates/www/pricing.html:97`, change `{{ _("SLA 99.9 percent") }}` to a split-form pattern: `{{ _("SLA") }} 99,9 %` (rendering the `%` as a literal character outside the gettext call). Re-extract + recompile catalogs; verify the message no longer triggers the format-string path. Affects one msgid in each of three locales.
- **Evidence**: `grep "SLA 99" /tmp/2026-05-09-pricing_*.html`:
  - cs: `SLA 99,9 procent`
  - en: `SLA 99.9 percent`
  - de: `SLA 99,9 Prozent`

### F-UX-021 — Pricing in CZK only on EN + DE pages; no FX hint for non-Czech prospects
- **Where**: `https://assoluto.eu/pricing` EN+DE; also `<script type="application/ld+json">` on EN+DE homepage (`priceCurrency: "CZK"` in `Offer` blocks)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: An English-speaking or German prospect lands on the pricing page and sees `490 CZK / month` / `490 CZK / Monat`. They have to mentally convert (`~€20`) before deciding. Same for Google rich-result snippets — the structured data in `og` and ld+json declares `priceCurrency: CZK` for every offer in every locale, so a German Google snippet for "Assoluto Starter" shows `490 CZK`. Editorial decision (the founder bills in CZK only and may not want to imply EUR pricing), but for first-impression UX a parenthetical EUR equivalent under each price card on the EN+DE pages would dramatically reduce reader friction. Note: the existing copy already references "with a Czech invoice / mit tschechischer Rechnung" so the CZK-only invoicing is communicated separately.
- **Suggested fix**: On EN+DE versions of `app/templates/www/pricing.html`, add a `<p class="text-xs text-slate-500">≈ €X / month (rate as of YYYY-MM-DD)</p>` under each priced card. Keep the headline price in CZK to avoid implying EUR invoicing. Optional: refresh the rate periodically by storing a single multiplier in `Settings`. Skip changing the ld+json (Google's offer schema doesn't accept dual currencies cleanly; better to keep it consistent with the actual invoice currency).
- **Evidence**: `python3 -c "import re,json; …" /tmp/2026-05-09-home_en.html` → `priceCurrency: "CZK"`. Pricing-card text excerpts: `en: 490 CZK / month`, `de: 490 CZK / Monat`.

### F-UX-022 — `robots.txt` disallows `/platform/signup`, blocking organic-search conversions
- **Where**: `https://assoluto.eu/robots.txt`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The current `robots.txt` has `Disallow: /platform/signup` and `Disallow: /platform/login`. The login page being de-indexed is fine, but the signup page is the **conversion endpoint** — every "Try free" CTA on the marketing site routes there. Disallowing it means a Google search like "assoluto signup" or "assoluto try free" can't surface the signup page directly, and Google may also down-rank the site overall because conversion-relevant pages are blocked. Most SaaS expose signup to crawlers (sometimes with `noindex` instead of robots disallow, so crawlers can follow inbound links but don't index the page).
- **Suggested fix**: In whatever generates robots.txt (likely a static asset or `app/routers/www.py`), remove `Disallow: /platform/signup`. Optionally add a `<meta name="robots" content="noindex, follow">` to the signup HTML if you want the page reachable via crawl but not surfaced as its own search result. Keep `/platform/login` and `/platform/admin` disallowed.
- **Evidence**: `curl https://assoluto.eu/robots.txt` lists `Disallow: /platform/signup`.

## Walkthrough log

Wall clock: ~22 min. Same constraints as the last two runs (Chrome MCP
not available; HTTP-only walk via `curl` + Python HTML grepping).

- **Apex marketing — Czech (`Accept-Language: cs`)**
  - `/` 200 — single `<title>`, hero copy "Přestaňte zvedat telefon" present, switcher in header + footer, ld+json `SoftwareApplication` + `FAQPage`, og + twitter cards, hreflang block (with the F-UX-016 caveat).
  - `/pricing` 200 — 4 plan cards (Community / Starter / Pro / Enterprise), Enterprise rightmost, `Doporučujeme` badge on Pro, "Cenovou nabídku pošleme do 1 pracovního dne" copy. F-UX-020 caught the "procent" line.
  - `/features` 200 — github URL is intentionally split across two `<div>`s for code-block styling (no actual broken link).
  - `/self-hosted` 200, `/contact` 200, `/terms` 200, `/privacy` 200, `/cookies` 200, `/imprint` 200 — all clean. Imprint shows IČO 09989978, DIČ "Neplátce DPH (§6 ZDPH)", ARS + ODR links.
  - Quote sweep PASSED — both CS and DE homepages now show 8 typographic `”` and 0 straight `"` (F-UX-007 / F-UX-012 fully resolved).
- **Apex marketing — English (`Accept-Language: en`)**
  - All 9 marketing URLs 200; quote-curlification not applicable (EN uses `"…"` directly).
  - F-UX-001 stable: `/terms` EN renders `99.9%` cleanly.
- **Apex marketing — German (`Accept-Language: de`)**
  - All 9 URLs 200, all single `<title>`.
  - DE pricing card translations look natural; F-UX-008 fix held (`Hetzner DE, im Ruhezustand verschlüsselt`).
- **Locale switcher / `/set-lang`**
  - GET → 303 to `next` (open-redirect-safe).
  - HEAD → 303 (F-UX-014 fix held). All other GET endpoints still 405 (F-UX-017 broadened).
  - Cookie now `Domain=.assoluto.eu` (F-UX-013 resolved). Verified DE choice on apex flips `test-a/auth/login` to "Anmelden".
  - Cookie `SameSite=lax` matches `csrftoken=…SameSite=lax` (F-UX-015 resolved).
- **Static / infra**
  - `/healthz`, `/readyz`, `/sitemap.xml`, `/robots.txt`, `/static/og/assoluto-og.png` (1200×630), `/static/{js/{theme-init,app,palette}.js,vendor/htmx.min.js,css/app.css,favicon.svg}` all 200.
  - `/favicon.ico` still 404 (F-UX-004 deferred).
  - `robots.txt` flagged in F-UX-022.
- **Tenant subdomain auth (test-a, CS+EN+DE)**
  - `/` 200 — landing renders with switcher.
  - `/auth/login` 200 — autocomplete `username`/`current-password`, autofocus on email, `forgot password?` link, dark-mode classes intact. Brand-colour drift (F-UX-018).
  - `/auth/password-reset` 200.
  - Title localises correctly: "Přihlásit se" / "Sign in" / "Anmelden" matches the active locale cookie.
- **Platform flows**
  - `/platform/login` 200 — autocomplete attrs + brand-coloured submit.
  - `/platform/signup` 200 — honeypot `name="website"` present, off-screen, `tabindex="-1"`, `autocomplete="off"`. All real inputs have proper autocomplete (`organization`, `name`, `username`, `new-password`). Terms checkbox + links to `/terms` and `/privacy` (with `target="_blank"` but no `rel="noopener"`; modern browsers default to noopener so not flagged).
  - `/platform/check-email` 200 in all three locales — copy reads naturally.
  - `/platform/select-tenant`, `/platform/switch/test-a`, `/platform/complete-switch` all return 303 to `/platform/login?next=…` when unauth (sane). Authenticated-but-unverified path has `require_verified_identity` → raises 403 with `Location` header → the global `http_exception_handler` in `app/main.py:434-442` converts that to a 303 to `/platform/verify-sent` for HTML requests. Defensive engineering checked out.
  - `/platform/billing/details` template inspected (`app/templates/platform/billing/details.html`): IČO `pattern="[0-9]{8}" maxlength="8" inputmode="numeric"`, DIČ has `placeholder="CZ12345678"`, autocomplete attrs on all fields, dark-mode classes intact, `_flash.html` include for POST→GET flash. Looks production-ready.
- **Inner app + platform admin** — skipped, no creds (F-UX-011 still deferred). Confirmed all `/app/*` and `/platform/admin/*` paths return either 303 (HTML accept) or 401 JSON (API accept), which is the correct dual contract. Templates spot-checked: `app/templates/platform/admin/tenants.html` has the new `Plan / Billing status / Period ends` columns; `subscription_edit.html` has plan + period + quick-action UI as the brief described.
- **Translation catalog hygiene**
  - CS: 0 untranslated msgids (proper multi-line PO parser).
  - DE: 0 untranslated msgids — the new `Billing details` template is fully translated (`Rechnungsdaten`, `Firmenname (wie auf der Rechnung)`, `Speichern & weiter`, etc.).
  - EN: 1007 empty msgstrs, all expected (English IS the source language; gettext falls back to msgid).
- **Mobile (390px) reflow** — could not test live; viewport meta present on every page; only fixed-pixel widths in markup are decorative blur overlays in the hero (`h-[520px] w-[520px]`).
- **Dark-mode visual sweep** — could not toggle; static markup has consistent `dark:bg-slate-{900,950}` and `dark:text-slate-{100,300}` pairs across auth, signup, billing-details. `theme-init.js` loaded synchronously in `<head>` to avoid FOUC.
- **Console errors / 5xx sweep** — no inline JS errors visible in shipped templates; no `{{` template leaks; no `&nbsp;` literal escapes; no `\"` escapes. Tested 18 paths × 3 locales = 54 requests, zero 5xx responses.

## Diff vs. previous run

- **Resolved (8 verified + 3 newly-resolved this run)**: F-UX-001, -002, -003, -006, -008, -009, -012 (was -007), -013, -014 *(for `/set-lang`)*, -015. F-UX-005 / -010 partially resolved (markup ships, URL shape wrong → restated as F-UX-016).
- **Persisted / restated**: F-UX-005 + F-UX-010 → F-UX-016 (all alternates point to the canonical URL); F-UX-014 → F-UX-017 (HEAD 405 on every other GET).
- **Deferred unchanged**: F-UX-004 (favicon.ico), F-UX-011 (no creds).
- **New**: F-UX-016 (hreflang URL shape, P1), F-UX-017 (HEAD 405 broadened, P2), F-UX-018 (brand-colour drift on tenant login, P2), F-UX-019 (contact form lacks honeypot, P2), F-UX-020 (SLA "percent" spell-out, P2), F-UX-021 (CZK-only pricing on EN+DE, P2), F-UX-022 (robots disallows signup, P2).

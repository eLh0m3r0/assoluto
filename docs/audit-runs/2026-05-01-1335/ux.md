# UX audit — 2026-05-01-1335

Run mode: HTTP-only walk via curl + raw HTML inspection (Chrome MCP
tools were not available in this session). Findings derived from
status codes, response bodies, and the rendered HTML of every page.
Browser-runtime concerns (theme-toggle JS, focus rings under tab,
mobile reflow at 390px) could not be observed directly — flagged
where the static HTML is suggestive.

### F-UX-001 — `/terms` returns HTTP 500 for any English locale
- **Where**: `https://assoluto.eu/terms` with `Accept-Language: en`, `en-US`, `en-GB`
- **Severity**: P0
- **Auto-fixable**: yes
- **Description**: The English Terms of Service page is broken on production. Every request with an English `Accept-Language` returns `{"detail":"Internal server error"}` (HTTP 500). CS and DE locales return 200. Root cause is the documented `%%` trap from CLAUDE.md §7: msgid `"...targets 99.9% monthly uptime..."` (`app/templates/www/terms.html:111`) contains a bare `%`. CS msgstr is `"99,9 %% měsíční…"` and DE is `"99,9 %% monatliche…"` — both correctly escaped. EN has empty msgstr (it falls back to msgid), Jinja's i18n extension runs `msg % {}`, and `% m` raises `ValueError: unsupported format character`. This breaks signup conversion: every English-speaking visitor who clicks the Terms link from the signup form (mandatory checkbox) sees a JSON error page instead of the legal text.
- **Suggested fix**: Either (a) populate the EN msgstr in `app/locale/en/LC_MESSAGES/messages.po:4994` with a translated string that uses `99.9%%` (preferred — fluent EN page), or (b) change the source msgid in `app/templates/www/terms.html:111` to use `99.9%%` so the msgid itself is safe to fall back to. The split-form pattern recommended by CLAUDE.md (`{{ _('targets') }} 99.9% {{ _('monthly uptime') }}`) is the durable fix. Also re-extract+compile after the change so EN doesn't keep falling back.
- **Evidence**: `curl -sS -o /dev/null -w '%{http_code}\n' https://assoluto.eu/terms -H 'Accept-Language: en'` returns `500`; with `Accept-Language: cs` returns `200`. Body is `{"detail":"Internal server error"}`. Reproduces 100%. Source: `app/locale/en/LC_MESSAGES/messages.po:4994-5004` (msgstr empty), `app/templates/www/terms.html:111` (msgid).

### F-UX-002 — Every marketing page renders TWO `<title>` tags
- **Where**: `https://assoluto.eu/` (and `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies`, `/imprint` — all locales). Same on `test-a.assoluto.eu/auth/login`.
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: Every page emits two `<title>` elements in the `<head>`. First is the real page title (e.g. `Assoluto · Zákaznický portál pro výrobní firmy v ČR`), second is `Assoluto — assoluto.eu trademark; see NOTICE.md before forking.`. Browsers will pick the first one for the tab, but the markup is invalid HTML and confuses scrapers, OG validators, social previews, and SEO tools. The second `<title>` looks like a developer note that was meant as a `<meta>` or a comment but ended up as a real `<title>`.
- **Suggested fix**: In the base template (`app/templates/www/www_base.html` or a layout it extends), demote the trademark notice to either an HTML comment `<!-- ... -->` or a `<meta name="trademark" content="...">`. Keep exactly one `<title>` per page.
- **Evidence**: `grep -c '<title>' /tmp/home_cs.html` returns `2`; `grep '<title>' /tmp/home_cs.html` shows both lines. Reproduces on every marketing page and the tenant login.

### F-UX-003 — Czech contact form shows double asterisk on the message label
- **Where**: `https://assoluto.eu/contact?lang=cs` — message field label
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: The "Your message" label in CS renders as `Vaše zpráva *  *` (two red asterisks). Cause: the CS msgstr in `app/locale/cs/LC_MESSAGES/messages.po:3016` is `"Vaše zpráva *"` (bakes the asterisk into the translation), then the template appends a second `<span class="text-rose-500">*</span>`. EN ("Your message *") and DE ("Ihre Nachricht *") render correctly because their msgstrs do not include the asterisk. Looks sloppy on the highest-funnel locale.
- **Suggested fix**: Edit `app/locale/cs/LC_MESSAGES/messages.po:3017` — change `msgstr "Vaše zpráva *"` to `msgstr "Vaše zpráva"`. Recompile catalogs.
- **Evidence**: rendered HTML in `/tmp/contact_cs.html`: `<label for="message" ...>Vaše zpráva * <span class="text-rose-500">*</span></label>`. Compared to EN/DE which only emit the template's own asterisk.

### F-UX-004 — `/favicon.ico` returns 404
- **Where**: `https://assoluto.eu/favicon.ico` (and the same on every subdomain — Caddy routes the same)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Modern browsers find the icon through `<link rel="icon" type="image/svg+xml" href=".../favicon.svg">` (which works — 200, ~SVG). But Safari, RSS readers, link-preview bots, browser bookmark exporters, and the literal `/favicon.ico` URL still 404. Each homepage tab shows a "404 favicon.ico" line in the developer console.
- **Suggested fix**: Either (a) add a static `/favicon.ico` (a 16x16 / 32x32 multi-resolution ICO mirroring the SVG) and serve it from the same `app/static` mount, or (b) add a Caddy/route alias that maps `/favicon.ico` to `/static/favicon.svg`.
- **Evidence**: `curl -sS -o /dev/null -w '%{http_code}\n' https://assoluto.eu/favicon.ico` returns `404`. SVG icon at `/static/favicon.svg` returns `200`.

### F-UX-005 — No `<link rel="alternate" hreflang>` between CS/EN/DE versions
- **Where**: every marketing page (`/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies`, `/imprint`)
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: The same canonical URL serves three different languages depending on `Accept-Language`. Without `<link rel="alternate" hreflang="cs" href="...">` (and `en`, `de`, plus `x-default`), Google indexes whichever locale Googlebot is configured for and deduplicates the rest. Czech-language SEO suffers because Googlebot from US data centres lands on `en_US` 500-error pages right now; once F-UX-001 is fixed, search engines still won't surface the right locale. Also confuses social previews — OG `og:locale` is set per request but there's no `og:locale:alternate` either.
- **Suggested fix**: In `app/templates/www/www_base.html`, add a static block of `<link rel="alternate" hreflang="cs" href="https://assoluto.eu{{ request.url.path }}">` for `cs`, `en`, `de`, and `x-default`. Same content under all three (the server already returns the right locale by `Accept-Language`); the hreflang signal is what matters. Also add `<meta property="og:locale:alternate" content="...">` for the other two locales.
- **Evidence**: `grep 'hreflang\|og:locale:alternate' /tmp/home_*.html` returns nothing; `<link rel="canonical">` is identical across all three locale versions.

### F-UX-006 — No visible language switcher on marketing site
- **Where**: every marketing page; also tenant login pages
- **Severity**: P1
- **Auto-fixable**: no (requires UX/design choice on placement)
- **Description**: A Czech-language visitor cannot read the Terms in English without manually editing browser preferences. The site detects locale from `Accept-Language` only — there is no globe / language dropdown in the header. For a Czech-headquartered SaaS targeting CZ, EN, and DE markets this is a significant friction point: a German prospect on a Czech browser sees the Czech homepage and bounces; the same prospect in a meeting room with a colleague trying to show the English version cannot do it via the UI.
- **Suggested fix**: Add a minimal CS/EN/DE switcher in the `<header>` (or footer minimum) of `app/templates/www/www_base.html`. Persist choice via a cookie that overrides `Accept-Language`. Keep the URL the same (don't switch to `/en/...` paths) so canonical/hreflang stays simple.
- **Evidence**: `grep -iE 'switch.*lang|hreflang|locale-switch' /tmp/home_*.html` returns nothing. No `<select name="locale">` or `<a href="?lang=...">` anywhere.

### F-UX-007 — Czech and German body quotes use straight ASCII closing quote (`"`) instead of typographic close
- **Where**: homepage CS+DE (testimonials, pull-quotes, "stop picking up the phone" cluster); same pattern likely on `/features`, `/contact`, etc.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Czech and German typography both pair `„` (low-9 open) with `"` (curly close). The current copy opens with `„` and closes with a straight ASCII `"`. Examples on home CS: `„kde je má zakázka?"`, `„Každý den mi dvacet lidí volá…"`. Same in DE: `„wo ist mein Auftrag?"`. Looks unpolished to native readers — a tell that copy was machine-translated.
- **Suggested fix**: Sweep `app/locale/cs/LC_MESSAGES/messages.po` and `app/locale/de/LC_MESSAGES/messages.po` for `„[^"]*"` and replace the trailing `"` with `"`. Same in any inline Czech/German strings in templates. (EN keeps `"..."` straight quotes — that's fine for English.)
- **Evidence**: `grep -oE '„[^"]*"' /tmp/home_cs.html` returns 7 unique quoted phrases, all with straight closing quote; same in `/tmp/home_de.html`.

### F-UX-008 — Pricing card "Co obsahuje každý plán" mixes Czech and English
- **Where**: `https://assoluto.eu/pricing` (CS locale) — the "What every plan includes" panel below the four plan cards
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The CS pricing page has a feature panel with Czech labels but English fragment values: `Data v EU — Hetzner DE, encrypted at rest.` The phrase `encrypted at rest` is bare English in a Czech sentence. A native CZ reader notices immediately; signals "this is a translated dev tool, not localised software".
- **Suggested fix**: In the CS msgstr for that string (or in `app/templates/www/pricing.html` if the value is hardcoded), translate to `Hetzner DE, šifrováno v klidu.` or similar. Also worth scanning DE — the same DE panel may have an analogous leak.
- **Evidence**: `/tmp/pricing_cs.html` line 304: `<strong class="text-slate-900 dark:text-slate-100">Data v EU</strong> — Hetzner DE, encrypted at rest.`

### F-UX-009 — Contact form name/email inputs missing `autocomplete` attributes
- **Where**: `https://assoluto.eu/contact` (all locales)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The signup form sets `autocomplete="organization" / name / username / new-password` properly (kudos), but the `/contact` form does not. Visitors with browser-saved profiles can't one-click-fill name and email. Minor friction; relevant for the prospect funnel where contact is a fallback to "talk to a human".
- **Suggested fix**: In `app/templates/www/contact.html`, add `autocomplete="name"` to the name input and `autocomplete="email"` to the email input.
- **Evidence**: `grep autocomplete /tmp/contact_cs.html` returns no matches inside the contact `<form>`.

### F-UX-010 — Sitemap.xml does not declare hreflang alternates
- **Where**: `https://assoluto.eu/sitemap.xml`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Pairs with F-UX-005. Even after adding `<link rel="alternate" hreflang>` in the page `<head>`, search engines benefit from the same signal in the sitemap via `<xhtml:link rel="alternate" hreflang="..." href="..."/>` blocks per `<url>`. The current sitemap (9 URLs, all single-locale) does not.
- **Suggested fix**: Update the sitemap generator (`app/routers/www.py:248-277` block) to emit per-locale alternates for each entry, plus `x-default`.
- **Evidence**: `curl -s https://assoluto.eu/sitemap.xml` shows plain `<url><loc>...</loc><priority>...</priority></url>` entries with no `xhtml:link` siblings.

### F-UX-011 — No authenticated walkthrough of `/app` (credentials not provided)
- **Where**: `/app`, `/app/orders`, `/app/customers`, `/app/products`, `/app/admin/*`, `/platform/admin/*`
- **Severity**: P2 (informational — not a defect)
- **Auto-fixable**: no
- **Description**: This audit could not log in and walk the inner customer portal or platform admin pages. `/app` returns `401` unauthenticated (correctly; no redirect — it's an XHR-shaped endpoint). The new platform admin subscription editor (commit `1b1c8f9`), the Plan / Billing status / Period ends columns on `/platform/admin/tenants`, and the in-app dark-mode regressions cannot be verified without a browser session and operator credentials. Filing as a "would-have-tested-but" so the next audit run can compare apples-to-apples.
- **Suggested fix**: For the next `/audit` invocation, drop a one-shot operator credential into the run via `AUDIT_PLATFORM_EMAIL` / `AUDIT_PLATFORM_PASSWORD` env vars (then revoke after the run). Or stand up a `audit-ux` ephemeral identity per run that has read-only platform admin + read-only tenant membership in `test-a`.
- **Evidence**: `curl -sS -o /dev/null -w '%{http_code}' https://test-a.assoluto.eu/app` returns `401`. No credentials available in this session.

## Walkthrough log

Wall clock: ~22 min. Tool constraints: Chrome MCP not loaded; HTTP-only via curl + raw HTML inspection.

- **Apex marketing — Czech (`Accept-Language: cs`)**
  - `/` 200 — single h1, hero "Přestaňte zvedat telefon", testimonials, CTA. Quotes use Czech opening `„` paired with straight ASCII close (F-UX-007).
  - `/pricing` 200 — 4 cards (Community / Starter / Pro / Enterprise) all render; "Doporučujeme" badge on Pro; English leak in "Co obsahuje" panel (F-UX-008).
  - `/features` 200 — 6 H2 headings present, dark-mode classes consistent.
  - `/self-hosted` 200 — visible H1; AGPL section present.
  - `/contact` 200 — form renders; double-asterisk on message label (F-UX-003); no autocomplete on inputs (F-UX-009).
  - `/terms` 200 — long but clean; `99,9 %%` correctly escaped.
  - `/privacy` 200 — full GDPR doc; no English leaks beyond proper-noun subprocessor names.
  - `/cookies` 200 — short, 4 sections, OK.
  - `/imprint` 200 — present (verified after spotting it in sitemap).
- **Apex marketing — English (`Accept-Language: en`)**
  - `/` 200; hero "Stop picking up the phone".
  - `/pricing` 200; "Recommended" badge on Pro; same panel as CS but English ("Czech support — real humans, not a chatbot").
  - `/features` 200.
  - `/self-hosted` 200.
  - `/contact` 200; no double-asterisk; missing autocomplete (F-UX-009).
  - **`/terms` 500** — broken (F-UX-001).
  - `/privacy` 200.
  - `/cookies` 200.
- **Apex marketing — German (`Accept-Language: de`)**
  - `/` 200; hero "Hören Sie auf, ans Telefon zu gehen" pattern; same straight-quote issue as CS (F-UX-007).
  - `/pricing` 200; "Empfohlen" badge.
  - `/features` 200; 6 H2 headings translated.
  - `/self-hosted` 200; "Anforderungen", "Lokale Demo in 60 Sekunden", etc.
  - `/contact` 200; no double-asterisk.
  - `/terms` 200.
  - `/privacy` 200.
  - `/cookies` 200.
- **Tenant auth surfaces — CS (`test-a`, `test-b`, `testfirma`)**
  - `/` 200 — landing card with workspace tagline + "Přihlásit se" link.
  - `/auth/login` 200 — form is well-themed (light + dark Tailwind classes paired); has `autocomplete="username"` + `autocomplete="current-password"`; "Zapomenuté heslo?" link present; workspace name interpolated.
  - `/auth/password-reset` 200 — clean; `autocomplete="username"`; "Zpět na přihlášení" link back.
  - All three test tenants render the same template with the right workspace name; only difference is the embedded tenant string. Good.
- **Platform flows**
  - `/platform/login` 200 — clean form, autocomplete attrs present.
  - `/platform/signup` 200 — full form: company_name (org), slug (off, with `pattern="[a-z0-9-]*"`), owner_full_name (name), owner_email (username), password (new-password, minlength=8), terms_accepted required checkbox. Honeypot is correctly off-screen (`position:absolute;left:-10000px;...;tabindex="-1";autocomplete="off"`); not Tab-reachable. Fully Czech regardless of `Accept-Language: en` for the form labels — but title is "Create portal" in EN, so locale routing partially works on this template.
  - `/platform/check-email` 200 — works with and without `?email=` query; resend form has CSRF.
  - `/platform/password-reset` 200 — render OK.
- **Static / infra**
  - `/healthz` 200 `{"status":"ok"}`; `/readyz` 200 `{"status":"ok"}`.
  - `/robots.txt` 200 — disallows `/app`, `/auth/`, `/platform/admin`, `/platform/login`, `/platform/signup`, `/platform/password-reset`. Sitemap reference present.
  - `/sitemap.xml` 200 — 9 URLs, no hreflang alternates (F-UX-010).
  - `/static/og/assoluto-og.png` 200 (image/png, 195 KB, 1200×630 declared).
  - `/static/favicon.svg` 200 (referenced in `<link rel="icon">`).
  - `/favicon.ico` **404** (F-UX-004).
- **Inner app + platform admin** — skipped, no credentials provided (F-UX-011).
- **Mobile (390px) reflow** — could not test; Chrome MCP unavailable. No HTML signals of overflow but flagged for the next browser-equipped run.
- **Dark-mode visual sweep** — could not toggle the theme; static HTML shows consistent `dark:bg-slate-{900,950}` + `dark:text-slate-{100,300}` classes on auth and signup pages. Theme-init JS is present at `/static/js/theme-init.js`.
- **Console errors** — could not observe live; HTML inspection shows no inline JS errors and no `console.error(` calls in shipped templates.

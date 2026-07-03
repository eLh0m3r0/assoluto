# UX audit — 2026-07-03-1153

**Started**: 2026-07-03T11:53:00+02:00
**Tip-of-tree commit**: `39a8dfb`
**Previous run**: [`2026-05-09-0931`](../2026-05-09-0931/ux.md)
**Mode**: standard re-walk (5th audit).

**Tooling note**: `mcp__claude-in-chrome__*` tools were NOT available this run
(same limitation as `2026-05-09-0931`). The walk was done with `curl` + locale
headers (`Accept-Language: cs|en|de`) + HTML/byte inspection + `WebFetch`. This
means the following checks could **not** be performed and are logged as gaps, not
passes: live JS console/network error scan, live dark-mode visual render, mobile
(390px) resize. Dark-mode findings below are from static Tailwind-class analysis
only. Locale is content-negotiated via `Accept-Language` — the `?lang=` query
param does **not** switch locale (verified), so each locale was fetched by header.

**Walkthrough-blocking gaps** (see log at bottom):
- Seeded test tenants `test-a`, `test-b`, `testfirma` **no longer resolve** —
  they return `404 {"detail":"Tenant not found"}`. Only demo tenant `4mex`
  resolves, so tenant auth surfaces were inspected on `4mex.assoluto.eu`.
- No tenant credentials and no operator credentials were provided, so the
  authenticated `/app/*` walkthrough and the `/platform/admin/*` walkthrough
  (dashboard, tenants columns, subscription quick-actions) could not be run.

---

## Findings

### F-UX-001 — hreflang alternates for all three locales point to the same URL
- **Where**: `https://assoluto.eu/` `<head>` (and every apex marketing page); rendered on CS/EN/DE.
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The page emits `<link rel="alternate" hreflang="cs|en|de|x-default" href="https://assoluto.eu/">` — all four alternates resolve to the identical URL. Because locale is chosen by `Accept-Language` content negotiation and there is no per-locale URL, Google cannot distinguish the language variants and will treat the hreflang cluster as misconfigured (it effectively self-references), losing the multi-language SEO benefit the tags are meant to provide. This is invisible to users but directly undercuts DE/EN discoverability for a site that clearly invested in full DE/EN translation.
- **Suggested fix**: Give each locale a stable, crawlable URL and point hreflang at it. Cheapest path: honour a `?lang=` (or `/en/`, `/de/` path prefix) that pins the locale server-side and set a canonical per variant, then emit `hreflang="en" href=".../?lang=en"` etc. If per-locale URLs are out of scope, drop the misleading `hreflang` alternates and keep only `canonical` — a single self-referential canonical is better than four identical hreflang links.
- **Evidence**: `curl -H "Accept-Language: en" https://assoluto.eu/ | grep hreflang` → three `hreflang` links all with `href="https://assoluto.eu/"`; `?lang=en` confirmed not to switch locale (page stays `<html lang="cs">`).

### F-UX-002 — Signup form inputs inconsistent dark-mode border (4 of 5 miss `dark:border-*`)
- **Where**: `https://assoluto.eu/platform/signup`; `app/templates/platform/signup.html` (input fields).
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: On the signup form only the `owner_full_name` input carries `dark:border-slate-700`; `company_name`, `slug`, `owner_email` and `password` do not. In dark mode those four keep the light `border-slate-300` (#cbd5e1) against `dark:bg-slate-900`, rendering a harsh bright border, while `owner_full_name` gets the muted dark border — so one field visibly differs from its neighbours in the same form. The sibling `/platform/login` form applies `dark:border-slate-700` to every input, which is the correct target. (Static-class inference; not visually confirmed — Chrome MCP unavailable.)
- **Suggested fix**: Add `dark:border-slate-700` to the four inputs missing it in `signup.html` so all five match `owner_full_name` / the login form.
- **Evidence**: per-input class audit — `company_name: MISSING`, `slug: MISSING`, `owner_email: MISSING`, `password: MISSING`, `owner_full_name: has dark:border`.

### F-UX-003 — Tenant auth shell and platform auth shell use divergent input styling
- **Where**: `https://4mex.assoluto.eu/auth/login` vs `https://assoluto.eu/platform/login`.
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The two auth surfaces render visibly different input controls: tenant login uses `rounded-lg`, `ring-2 ring-brand-500/20`, `border-gray-300`, `dark:bg-slate-800`; platform login uses `rounded-md`, `ring-1 ring-brand-500`, `border-slate-300`, `dark:bg-slate-900`. Both are legible in light and dark mode, so this is polish, not a defect — but a prospect who signs up on the platform shell and then lands on the tenant login sees two different-looking form systems for the same product.
- **Suggested fix**: Pick one input treatment (the tenant `rounded-lg`/`ring-2` set is the richer of the two) and extract it into a shared `_input.html` macro or a Tailwind component class used by both `auth/` and `platform/` login templates.
- **Evidence**: tenant email input class = `... rounded-lg ... focus:ring-2 focus:ring-brand-500/20 ... dark:bg-slate-800 ...`; platform email input class = `... rounded-md ... focus:ring-1 focus:ring-brand-500 ... dark:bg-slate-900 ...`.

### F-UX-004 — `text-blue-*` link leak in auth language switcher (persisted from F-UX-023, unfixed)
- **Where**: `https://assoluto.eu/platform/login`, `…/platform/signup`, `https://4mex.assoluto.eu/auth/login`, `…/auth/password-reset`. Shared auth-shell language-switcher partial.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Carried forward from the previous audit (F-UX-023) and never fixed. The language-switcher links at the foot of every auth page still render `class="font-semibold text-blue-600 dark:text-blue-400"` instead of the brand palette used everywhere else on the page — 8 stranded `text-blue-*` references across the four auth surfaces (2 per page in the EN render). Small brand-consistency drift against the otherwise `brand-*` pages.
- **Suggested fix**: In the shared switcher partial replace `text-blue-600 dark:text-blue-400` → `text-brand-600 dark:text-brand-400`. One template, cascades to all four pages.
- **Evidence**: `grep -o 'text-blue-[0-9]*' plogin-en.html psignup-en.html tlogin-en.html treset-en.html` → 2 × `text-blue-400` + 2 × `text-blue-600` per file (8 total).

### F-UX-005 — Homepage FAQ still spells "percent / procent / Prozent" (persisted from F-UX-024, unfixed)
- **Where**: `https://assoluto.eu/` FAQ ("Security & outages?"), all three locales; visible `<dd>` and JSON-LD `FAQPage.acceptedAnswer.text`. `app/templates/www/index.html`.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Unchanged since the previous audit. The homepage FAQ writes the availability figure long-form — `99,9 procent` (CS), `99.9 percent` (EN), `99,9 Prozent` (DE) — while the pricing card was already fixed to use the `%` glyph. Awkward in all three languages and inconsistent with the pricing surface. 2 occurrences per locale (body + JSON-LD).
- **Suggested fix**: Use the split-form pattern from CLAUDE.md §7 — render `99,9 %` / `99.9 %` outside the gettext call, keep only the surrounding sentence in `_()`. Update both the `<dd>` and the JSON-LD source (same string). Re-extract/compile catalogs.
- **Evidence**: `grep -o '99[.,]9 \(percent\|procent\|Prozent\)'` → 2 each in `index-cs/en/de.html`.

### F-UX-006 — Czech word "doklad" leaks into EN pricing FAQ (persisted from F-UX-025, unfixed)
- **Where**: `https://assoluto.eu/pricing` (EN), FAQ "What do I need to enter at first paid checkout?". EN msgstr in `app/templates/www/pricing.html`.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Unchanged since the previous audit. The EN answer ends "...to issue a valid Czech tax doklad." — the Czech word `doklad` was pasted untranslated into English copy, so an English prospect sees what reads as a typo or an unknown brand term. DE (`Steuerbeleg`) is correct.
- **Suggested fix**: Replace `tax doklad` → `tax invoice` in the EN msgstr; `pybabel compile`.
- **Evidence**: `grep -o 'tax doklad' pricing-en.html` → 1 match.

### F-UX-007 — EN pricing Enterprise card uses comma decimal `SLA 99,9 %` (persisted from F-UX-026, unfixed)
- **Where**: `https://assoluto.eu/pricing` (EN), Enterprise card last bullet. `app/templates/www/pricing.html`.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Unchanged since the previous audit. All three locales render the literal `SLA 99,9 %` with a comma decimal; correct for CS/DE, but English convention is `99.9 %`, so it reads as a typo to a native English reader. The localized period form already exists in EN elsewhere (the homepage FAQ writes "99.9").
- **Suggested fix**: Localize the decimal separator — `{% if locale == 'en' %}99.9{% else %}99,9{% endif %} %` — or move the string into a per-locale msgid (mind the `%%` trap, CLAUDE.md §7).
- **Evidence**: `grep -o 'SLA 99[.,]9' pricing-en.html` → `SLA 99,9`.

### F-UX-008 — DE contact microcopy mixes German opening quote with English closing quote (partial-fix of F-UX-027, still wrong)
- **Where**: `https://assoluto.eu/contact` (DE), microcopy `Schreiben Sie „Demo" in die Nachricht`. `app/templates/www/contact.html` DE msgstr.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The previous audit (F-UX-027) flagged a German opening quote paired with an ASCII straight quote. It was partly addressed — the closing character is now a curly quote — but it is U+201D (`"`, the *English* right double quote), not the German closing U+201C (`"`). Standard German typography is `„…"` (U+201E … U+201C). The pair is still typographically incorrect for German.
- **Suggested fix**: Change the closing character in the DE msgstr from U+201D to U+201C so the pair reads `„Demo"`.
- **Evidence**: byte dump of the rendered string = `e2 80 9e` (`„` U+201E) `44 65 6d 6f` (`Demo`) `e2 80 9d` (`"` U+201D) — should end in `e2 80 9c` (U+201C).

---

## Verification of prior-audit findings

| Prior ID | Status this run | Evidence |
|---|---|---|
| F-UX-017 (all surfaces 200) | held | `/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies`, `/robots.txt`, `/sitemap.xml`, `/platform/{login,signup,check-email,password-reset}` all 200; `4mex.assoluto.eu/auth/{login,password-reset}` 200. `/healthz` + `/readyz` 200. |
| F-UX-019 (honeypot) | held | `/platform/signup` honeypot `website` input inside `aria-hidden` `position:absolute;left:-10000px` div with `tabindex="-1" autocomplete="off"` — not tab-reachable. |
| F-UX-022 (robots signup) | held | `robots.txt` has no `Disallow: /platform/signup`. |
| F-UX-023 → **F-UX-004** | **persisted (unfixed)** | 8 `text-blue-*` on auth switcher. |
| F-UX-024 → **F-UX-005** | **persisted (unfixed)** | percent/procent/Prozent on homepage FAQ. |
| F-UX-025 → **F-UX-006** | **persisted (unfixed)** | `tax doklad` in EN pricing. |
| F-UX-026 → **F-UX-007** | **persisted (unfixed)** | `SLA 99,9 %` comma decimal in EN. |
| F-UX-027 → **F-UX-008** | **partial (still wrong)** | close quote now curly but U+201D not U+201C. |

No previously-fixed finding regressed. The five persisted items (F-UX-004..008) were all P2 in the prior run and appear untouched — the prior run was a verification pass; these low-severity items were evidently not routed through `/audit-fix`.

---

## What passed silently (positive evidence)

- **Signup form quality**: honeypot correctly hidden + non-tabbable; `autocomplete` attrs correct (`organization`, `name`, `username`, `new-password`); `required`/`minlength=8`/`pattern` present; `autofocus` on first field; visible `focus:ring` on every control; Terms+Privacy consent checkbox `required`.
- **Copy hygiene**: no `%(var)s` placeholder leaks, no literal `&nbsp;`, no escaped `\"`, across all 42 fetched HTML files. No untranslated English strings in CS/DE visible text on homepage, pricing, features, self-hosted, contact (apparent "orders" hits were a demo URL and a SQL RLS code sample; "Support" is a valid German word).
- **Hero**: "Stop picking up the phone" H1 correctly translated in all three locales (CS `Přestaňte zvedat telefon.`, DE `Hören Sie auf, das Telefon abzunehmen.`).
- **Pricing**: 4 plan cards (Community / Starter / Pro / Enterprise) consistent across locales; prices render (`490` / `1 490` with localized unit — `Kč` CS, `CZK` EN/DE — and localized period `/ měsíc`, `/ month`, `/ Monat`); "Recommended" badge present and translated in all three (`Doporučujeme` / `Recommended` / `Empfohlen`); Starter/Pro CTAs → `/platform/signup?plan=starter|pro`, Community → GitHub, Enterprise → contact.
- **Meta/SEO**: `og:title`/`og:description` localized per locale; `og:image` 200 (1200×630 PNG, 195 KB); `twitter:card=summary_large_image`; `canonical` present. (hreflang defect logged as F-UX-001.)
- **Error handling**: apex unknown path and unknown-tenant subdomain both serve a branded HTML 404 to `Accept: text/html` clients (raw JSON only for non-HTML clients — correct API/HTML split).
- **Auth gating**: `/platform/admin/dashboard` and `/platform/admin/tenants` 303-redirect to `/platform/login?next=…` for unauthenticated visitors.
- **Dark mode (static)**: platform-login and tenant-login inputs carry full `text-slate-900 dark:text-slate-100` + `dark:bg-*` — no invisible-text risk found on auth surfaces (only the signup border inconsistency F-UX-002).

---

## Walkthrough log

- **Setup**: confirmed Chrome MCP unavailable; fell back to curl+header inspection. `/healthz`, `/readyz` → 200.
- **Apex marketing (CS/EN/DE via Accept-Language)**: fetched `/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies` in all three locales (24 HTML docs). Verified hero, 4 pricing cards + badges + prices + CTAs, features/self-hosted section headings, meta tags, hreflang, cookie page (no consent-banner regression — site uses only the essential `csrftoken` cookie). Contact form rendered and inspected; **not submitted**.
- **Auth surfaces**: `test-a`/`test-b`/`testfirma` return `404 Tenant not found` — used `4mex.assoluto.eu` instead. Inspected `/auth/login` and `/auth/password-reset` (light + dark class analysis only; no live theme toggle).
- **Platform flows**: inspected `/platform/login`, `/platform/signup` (full field/honeypot/autocomplete/focus audit — **not submitted**), `/platform/check-email`, `/platform/password-reset` in all three locales. check-email copy fully translated CS/DE.
- **Authenticated tenant `/app/*`**: SKIPPED — no credentials provided and no seeded test tenant resolves.
- **Platform admin `/platform/admin/*`**: SKIPPED for content — no operator credentials. Confirmed only that the routes are auth-gated (303 → login). The new Plan / Billing status / Period-ends columns on `/platform/admin/tenants` and the subscription quick-actions could **not** be verified this run.
- **Not performed (Chrome MCP absent)**: live JS console error scan, live network ≥400 scan, live dark-mode visual, mobile 390px overflow check. These remain unverified for this run.
- **Time**: ~18 min wall clock.

## Would-have-tested-but

- **Seeded test tenants gone**: `test-a`, `test-b`, `testfirma` all 404. If these are meant to exist for audits, they need re-seeding (`python -m scripts.create_tenant`); otherwise update the audit agent definition to point at `4mex`. Without them, the entire authenticated tenant + contact-portal walkthrough is unreachable.
- **Operator + tenant credentials**: not supplied via env or prior conversation, so `/app/*`, `/app/admin/*`, `/app/me/profile`, `/platform/admin/*` content were all out of reach.

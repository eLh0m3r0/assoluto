# UX audit (verification run) — 2026-05-01-1455

Run mode: HTTP-only walk via curl + raw HTML inspection (Chrome MCP
tools were not available again). This is the verification pass after
`/audit-fix` against the 2026-05-01-1335 baseline. Each previous
finding has a status: **resolved** (fix held), **persisted** (fix
incomplete or not applied), **deferred** (intentionally not fixed),
or **regressed** (worsened).

## Verification status of previous findings

| ID | Title | Previous severity | Status | Notes |
|---|---|---|---|---|
| F-UX-001 | `/terms` 500 in EN | P0 | **resolved** | EN/CS/DE all 200; the `99.9%` text now renders cleanly across all three locales. |
| F-UX-002 | Two `<title>` tags per page | P1 | **resolved** | All 27 (3 locales × 9 pages) marketing URLs render exactly one `<title>`; trademark notice removed entirely from output. |
| F-UX-003 | CS contact double asterisk | P1 | **resolved** | `Vaše zpráva *` shows exactly one `<span>` asterisk; CS msgstr trimmed. |
| F-UX-004 | `/favicon.ico` 404 | P2 | **deferred** (still 404) | As noted — manual; still surfaces. |
| F-UX-005 | No hreflang alternates | P1 | **resolved** | Homepage has cs/en/de/x-default `<link rel="alternate" hreflang>`; also `og:locale:alternate` for the other two locales; canonical preserved. |
| F-UX-006 | No language switcher | P1 | **resolved** (bonus) | Was tagged `Auto-fixable: no`, but the fix wave shipped a CS/EN/DE switcher visible in both header and footer of every marketing page — and on tenant subdomain pages too. Persists choice via `sme_locale` cookie (1y, Secure, SameSite=Lax). `next=` is sanitized against open-redirect (absolute URLs and protocol-relative `//evil.com` fall back to `/`). |
| F-UX-007 | CS+DE straight close quote | P2 | **persisted** | Only ONE quote per locale was curlified (the repeated `„kde je má zakázka?"` got a curly close on its second occurrence). All five testimonial pull-quotes on the CS homepage and all five on DE still close with straight ASCII `"`, plus `„implementační projekt"` and `„rychlý hovor s obchodem"` in body copy. The fix appears to have curlified ~12% of cases. See F-UX-012 for the new finding spelling out exactly which msgstrs still need attention. |
| F-UX-008 | CS pricing English leak | P2 | **resolved** | Now reads `Hetzner DE, šifrováno v klidu.`; DE counterpart correctly translated to `Hetzner DE, im Ruhezustand verschlüsselt.` |
| F-UX-009 | Contact form autocomplete | P2 | **resolved** | All three locales now have `autocomplete="name"` and `autocomplete="email"` on the contact form. |
| F-UX-010 | Sitemap hreflang | P2 | **resolved** | `sitemap.xml` now has 36 `xhtml:link` lines (9 URLs × 4 locales). Confirmed across `/`, `/features`, `/pricing`, `/self-hosted`. |
| F-UX-011 | No authenticated walkthrough | P2 (info) | **deferred** | Still no creds — `/app`, `/app/orders`, `/platform/admin/*` all 401 anonymously, as expected. |

Eight P0/P1/P2 fixes verified resolved; one bonus resolution (F-UX-006);
two P2s intentionally deferred; one P2 (F-UX-007) persisted because the
fix only swept one occurrence per locale instead of all eight.

## New findings this run

### F-UX-012 — Typographic close-quote sweep was incomplete; 13 testimonial/body quotes still use straight ASCII `"` on CS+DE
- **Where**: `https://assoluto.eu/` (CS + DE), and the same pattern bleeds into `https://assoluto.eu/features`, `/pricing`, `/contact` per the previous audit. Specific instances on the CS homepage:
  - `„Nemůžeme posunout termín na 29. 4.? Právě nám volali z lakovny…"`
  - `„kde je má zakázka?"` (first occurrence — second occurrence is correctly `”`)
  - `„Každý den mi dvacet lidí volá, kde je jejich zakázka. A já se pak musím jít zeptat mistra do dílny."`
  - `„Furt hledám v Outlooku, kterou verzi výkresu klient poslal minulý týden. A občas uděláme díl podle špatné revize."`
  - `„Máme tabulku, ale aktualizuje ji jenom Jana. Když je nemocná, nevíme nic."`
  - `„implementační projekt"` and `„rychlý hovor s obchodem"`
  - DE has the symmetric set with German wording (six quotes).
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The `/audit-fix` commit for F-UX-007 only swept one msgid per locale (specifically the `„kde je má zakázka?"` / `„wo ist mein Auftrag?"` pair, which now closes with `"`). All other testimonial pull-quotes and body quotes still use straight ASCII `"` after a Czech low-9 opening `„`. This is the same typographic mismatch that the original finding flagged — the fix needs to be applied catalog-wide, not to a single match. Without it, native Czech and German readers continue to perceive the testimonial copy as machine-translated.
- **Suggested fix**: Run a real catalog sweep on `app/locale/cs/LC_MESSAGES/messages.po` and `app/locale/de/LC_MESSAGES/messages.po`: `python -c "import re,sys; ..."` (or hand-edit) to replace every `"` that closes a `„`-opened phrase with `"`. The regex `„([^"„]+)"` → `„\1"` should be safe; sanity-check with the verification snippet below before recompiling. After: `pybabel compile -d app/locale`.
- **Evidence**: Closing-quote distribution from `/tmp/home_cs.html` and `/tmp/home_de.html`: each locale has 7 straight-ASCII closes vs. 1 typographic close. Reproduces with: `python3 -c "import re; from collections import Counter; print(Counter(re.findall(r'„[^„]{3,200}?([\"“”])', open('/tmp/home_cs.html').read())))"`.

### F-UX-013 — Locale cookie is host-only; choice on apex doesn't carry to tenant subdomain
- **Where**: `https://assoluto.eu` → any tenant subdomain (`https://test-a.assoluto.eu`, `https://testfirma.assoluto.eu`, etc.)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: `/set-lang` sets the `sme_locale` cookie without a `Domain` attribute (host-only). A German prospect who picks DE on `assoluto.eu`, then signs up and lands on `test-a.assoluto.eu/auth/login`, sees the tenant login in CS again because the locale preference doesn't follow them across subdomains. Friction on the highest-value moment (first login). Reproduced: `curl -c jar.txt 'https://assoluto.eu/set-lang?lang=en&next=/'` writes `sme_locale=en` scoped to `assoluto.eu` only; `curl -b jar.txt https://test-a.assoluto.eu/auth/login` returns the CS title `Přihlásit se · Test Alpha s.r.o.` — same as a fresh visit.
- **Suggested fix**: In the `/set-lang` handler, set the cookie with `domain=.assoluto.eu` in production (gated on `settings.platform_cookie_domain` so dev single-host stays unaffected). Mirror the same cookie scope rules used by the platform session cookie. Be aware that this couples the locale preference across all tenant subdomains a user touches — that's the desired behaviour; an enterprise customer who pinned EN on the marketing site presumably wants EN inside their tenant too.
- **Evidence**: Cookie jar dump: `assoluto.eu	FALSE	/	TRUE	1809176449	sme_locale	en` — the `FALSE` means host-only, no domain match. Confirmed empirically: cookie does not propagate to subdomain.

### F-UX-014 — `/set-lang` returns 405 on HEAD even though it's idempotent + safe
- **Where**: `https://assoluto.eu/set-lang?lang=en&next=/`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: `HEAD /set-lang?...` returns `HTTP 405 Allow: GET`. RFC 9110 requires that any resource supporting GET also support HEAD (responding with the same headers and an empty body). Browsers and some link-checkers (uptime monitors, security scanners) probe with HEAD; getting 405 is noise in observability dashboards. Minor by itself, but worth aligning since the language-switcher endpoint is high-traffic now that the switcher ships in every page footer.
- **Suggested fix**: In `app/routers/www.py` (or wherever `set_lang` is wired), expose the route on both GET and HEAD, e.g. `methods=["GET", "HEAD"]`. FastAPI/Starlette will share the same handler; for HEAD you can short-circuit and return a `Response(status_code=303, headers={"Location": next_url})` without setting cookies (HEAD must not change state, which is the deeper concern — a HEAD probe should not flip a user's locale either).
- **Evidence**: `curl -sS -I 'https://assoluto.eu/set-lang?lang=en&next=/'` returns `HTTP/2 405 Allow: GET`. GET to the same URL returns 303.

### F-UX-015 — `/set-lang` cookie attribute case differs from platform session cookies (`SameSite=lax` vs `SameSite=Lax`)
- **Where**: `Set-Cookie: sme_locale=en; ... SameSite=lax; ...` on `/set-lang`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Cosmetic but inconsistent: the `csrftoken` cookie sets `SameSite=Lax` (capital L), the new `sme_locale` cookie sets `SameSite=lax` (lowercase). RFC 6265bis / browsers accept both, so no functional break. But code reviewers and the next operator will assume two different cookies mean two different policies. The mismatch suggests two different code paths assemble cookies; consolidating them makes audits + rotation simpler.
- **Suggested fix**: In the `set_lang` handler use the same cookie helper as the rest of the codebase (or pass `samesite="Lax"` matching the casing used in `app/security/session.py`). One-line change; verify by grepping `samesite=` in the Python sources.
- **Evidence**: Header capture from `curl -D - 'https://assoluto.eu/set-lang?lang=en&next=/'`: `set-cookie: sme_locale=en; Max-Age=31536000; Path=/; SameSite=lax; Secure` next to `set-cookie: csrftoken=...; SameSite=Lax`.

## Walkthrough log

Wall clock: ~18 min. Constraints same as last run (Chrome MCP not loaded; HTTP only).

- **Apex marketing — Czech (`Accept-Language: cs`)**
  - `/` 200 — single `<title>`, hero copy, switcher present in header + footer (3 anchors each). Quote sweep persisted (F-UX-012).
  - `/pricing` 200 — 4 plan cards, "Doporučujeme" badge, English leak fixed (`šifrováno v klidu`).
  - `/features` 200 — same quote pattern as home (F-UX-012 surface).
  - `/self-hosted` 200.
  - `/contact` 200 — single asterisk on message label (F-UX-003 fix held), `autocomplete="name"` + `autocomplete="email"` present (F-UX-009 fix held).
  - `/terms` 200 — clean, `99,9 %` rendering correct.
  - `/privacy` 200, `/cookies` 200, `/imprint` 200 — all clean.
- **Apex marketing — English (`Accept-Language: en`)**
  - `/terms` 200 (was 500) — F-UX-001 confirmed fixed; `99.9%` text renders properly.
  - All other pages 200; canonical = same URL across locales as expected; hreflang alternates present per F-UX-005.
- **Apex marketing — German (`Accept-Language: de`)**
  - All pages 200; same quote-sweep deficit as CS (F-UX-012).
  - `/pricing` DE: `Hetzner DE, im Ruhezustand verschlüsselt.` — translated correctly.
- **Static / infra**
  - `/healthz` 200, `/readyz` 200.
  - `/sitemap.xml` 200, 9 `<url>` blocks, 36 `<xhtml:link>` lines (4 alternates per URL — F-UX-010 fix held).
  - `/static/og/assoluto-og.png` 200 (1200×630, 195KB).
  - `/static/{css/app.css, js/app.js, js/theme-init.js, js/palette.js, vendor/htmx.min.js, favicon.svg}` all 200.
  - `/favicon.ico` 404 (F-UX-004 deferred).
- **Language switcher (new since last audit)**
  - `/set-lang?lang=en&next=/` returns 303 → `/`, sets `sme_locale=en` cookie (Path=/, Max-Age=1y, Secure, SameSite=lax).
  - Open-redirect defended: `next=https://evil.com/` and `next=//evil.com` both rewrite Location to `/`.
  - HEAD method 405 (F-UX-014 new).
  - Cookie host-only — no domain= attribute, doesn't carry to subdomains (F-UX-013 new).
  - Switcher anchors appear in header + footer of every marketing page and on tenant subdomain auth pages.
- **Tenant auth surfaces (test-a, CS)**
  - `/` 200 — landing renders, switcher present.
  - `/auth/login` 200 — single `<title>`; `autocomplete="username"` + `autocomplete="current-password"`; theme classes consistent.
  - `/auth/password-reset` 200.
- **Platform flows**
  - `/platform/login` 200 — autocomplete attrs intact.
  - `/platform/signup` 200 — single `<title>`; honeypot `<input name="website" tabindex="-1" autocomplete="off">` correctly hidden + non-tab-reachable; all other inputs have proper autocomplete (organization / name / username / new-password).
  - `/platform/check-email` 200, `/platform/password-reset` 200.
- **Inner app + platform admin** — skipped, no credentials provided (F-UX-011 still deferred).
- **Mobile (390px) reflow** — could not test live; HTML still has `<meta name="viewport" content="width=device-width, initial-scale=1">`, no signals of overflow in static markup.
- **Dark-mode visual sweep** — could not toggle; static HTML still has consistent `dark:bg-slate-{900,950}` + `dark:text-slate-{100,300}` pairs on auth and signup pages.
- **Console errors** — could not observe live; no inline JS errors in shipped templates; no `{{` template leaks; no `&nbsp;` literal escapes; no `\"` escapes.

## Diff vs. previous run

- **Resolved (8)**: F-UX-001, F-UX-002, F-UX-003, F-UX-005, F-UX-006 (bonus), F-UX-008, F-UX-009, F-UX-010
- **Persisted (1)**: F-UX-007 (incomplete sweep — restated as F-UX-012 with explicit instance list)
- **Deferred unchanged (2)**: F-UX-004 (favicon.ico still 404), F-UX-011 (no creds for inner walkthrough)
- **New (4)**: F-UX-012 (quote sweep restatement), F-UX-013 (locale cookie host-only), F-UX-014 (HEAD 405 on /set-lang), F-UX-015 (cookie attr case mismatch)

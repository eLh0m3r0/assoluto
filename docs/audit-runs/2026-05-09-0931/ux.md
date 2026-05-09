# UX audit — 2026-05-09-0931 (verification run)

**Started**: 2026-05-09T09:31:00+02:00
**Tip-of-tree commit**: `d3d911e`
**Previous run**: [`2026-05-09-0829`](../2026-05-09-0829/findings.md)
**Mode**: verification of fixes from `/audit-fix` cycle (12 of 19 closed) + standard re-walk.
**Tooling note**: Chrome MCP tools were not available this run. Verification done via
`curl` + locale headers + HTML inspection. No live JS console scan; no live dark-mode
visual screenshot. Findings that would require runtime observation are flagged.

---

## Verification of fixes claimed by `/audit-fix` (5 UX + 3 BIZ)

| ID         | Status this run | Evidence |
|---         |---              |---|
| F-UX-017   | held            | `HEAD` returns 200 on `/`, `/pricing`, `/features`, `/contact`, `/terms`, `/sitemap.xml`, `/platform/signup`, `/platform/login`, `/platform/check-email`, `test-a.assoluto.eu/auth/login`, `…/auth/password-reset`. |
| F-UX-018   | partial — see F-UX-023 | `bg-blue-*` swept on tenant `/auth/login` + `/auth/password-reset`, but `text-blue-600 dark:text-blue-400` persists on the language-switcher links inside the shared auth-shell macro. Same leak on platform `/platform/login` + `/platform/signup`. |
| F-UX-019   | held            | `<input type="text" id="website" name="website" tabindex="-1" autocomplete="off">` rendered inside an `aria-hidden="true"` div with `position:absolute;left:-10000px` on `/contact`. Same shape as `/platform/signup`. |
| F-UX-020   | partial — see F-UX-024 | Pricing-card Enterprise bullet now reads `SLA 99,9 %` in CS/EN/DE. **But** the homepage FAQ + ld+json `acceptedAnswer` still say `99,9 procent` / `99.9 percent` / `99,9 Prozent`. The fix only swept `pricing.html`; `index.html` was missed. |
| F-UX-022   | held            | `https://assoluto.eu/robots.txt` no longer contains `Disallow: /platform/signup`. `/platform/login` and `/platform/admin` remain disallowed (correct). |
| F-BIZ-012  | held            | Homepage step-1 reads `Email, password, company name.` (EN) / `E-mail, heslo, název firmy.` (CS) / `E-Mail, Passwort, Firmenname.` (DE). IČO mention removed across all three locales. |
| F-BIZ-013  | held            | Pricing FAQ entry "What do I need to enter at first paid checkout?" present in CS / EN / DE with IČO + DIČ + billing-address copy. Note one new copy bug — see F-UX-025 below. |
| F-BIZ-014  | held            | Enterprise card now shows `Priority support (4 h business hours, written SLA)` + `SLA 99,9 %` in CS/EN/DE. Pro card unchanged (12 h). Tier inversion resolved. |

**Bottom line**: 6 of 8 fixes fully held; 2 (F-UX-018, F-UX-020) are partial — the
fix touched the page that was the original example but left sibling surfaces with the
same root cause. New findings F-UX-023 / F-UX-024 below capture the remaining gap.

No previously-fixed finding regressed.

---

## New findings

### F-UX-023 — `text-blue-*` link leak in auth-shell language switcher (F-UX-018 scope gap)
- **Where**: `https://test-a.assoluto.eu/auth/login`, `…/auth/password-reset`,
  `https://assoluto.eu/platform/login`, `…/platform/signup`. Likely
  `app/templates/auth/_shell.html` or a shared `_lang_switcher.html` partial
  that the templates touched in `5263a05` did not include.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: F-UX-018 swept `bg-blue-*` on the tenant auth pages but missed
  the language-switcher links that render at the bottom of those same pages.
  Each rendered page contains 2 instances of
  `class="font-semibold text-blue-600 dark:text-blue-400"` (CS link + EN link;
  the active locale link drops the class). Across tenant `/auth/login`,
  `/auth/password-reset`, platform `/platform/login`, `/platform/signup`, that's
  8 stranded `text-blue-*` references. Same brand-consistency drift the original
  finding flagged — small but visible against the otherwise `brand-*` page.
- **Suggested fix**: Find the partial that renders those `<a>` links and replace
  `text-blue-600 dark:text-blue-400` → `text-brand-600 dark:text-brand-400`.
  One template, ~2 swaps; cascades to all four pages.
- **Evidence**: `grep -E 'text-blue-' /tmp/{p-login-en,signup-en,test-a-login,test-a-reset}.html`
  returns 8 matches. Snippet: `class="font-semibold text-blue-600 dark:text-blue-400" aria-label="Čeština"`.

### F-UX-024 — Homepage FAQ still spells "percent" / "procent" / "Prozent" (F-UX-020 scope gap)
- **Where**: `app/templates/www/index.html` FAQ — answer to "Bezpečnost a výpadky?".
  Renders on `https://assoluto.eu/` in the visible FAQ `<dd>` AND inside the
  JSON-LD `FAQPage` `acceptedAnswer.text`.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: F-UX-020 fixed the Enterprise pricing card to use the literal
  `%` glyph via the split-form pattern. The homepage FAQ entry that contains the
  identical phrasing — `Cílová dostupnost 99,9 procent` (CS), `Target uptime: 99.9 percent` (EN),
  `Zielverfügbarkeit: 99,9 Prozent` (DE) — was not touched. Same `%%` Jinja
  trap workaround still in use. Awkward in all three languages; same
  brand-consistency point as F-UX-020.
- **Suggested fix**: Same split-form pattern: render `99,9 %` (or `99.9 %` for
  EN) outside the gettext call, keep just the surrounding sentence inside `_()`.
  Update CS/EN/DE msgids; `pybabel extract -F babel.cfg ... && pybabel update`.
  Both the visible `<dd>` and the JSON-LD string need updating (same source).
- **Evidence**: `grep "99.9 percent\|99,9 procent\|99,9 Prozent" /tmp/index-{cs,en2,de}.html`
  returns 6 matches across the 3 locales (visible body + JSON-LD).

### F-UX-025 — `tax doklad` Czech-word leak in EN pricing FAQ
- **Where**: `https://assoluto.eu/pricing` (EN), Pricing FAQ entry
  "What do I need to enter at first paid checkout?". Body string is in the EN msgstr
  for `app/templates/www/pricing.html` — the F-BIZ-013 commit (`35c2b03`).
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The EN body reads `"...We need these to issue a valid Czech
  tax doklad."` — the Czech word `doklad` (= invoice / tax document / receipt) was
  pasted untranslated into the English copy. An English-speaking prospect sees
  what looks like a typo or a brand term they've never heard. The translator
  (likely the same commit author working CS-first) appears to have missed it.
- **Suggested fix**: Replace `tax doklad` → `tax invoice` in the EN msgstr. DE
  msgstr is fine (`Steuerbeleg`). One-line change, then `pybabel compile`.
- **Evidence**: `grep "tax doklad" /tmp/pricing-en2.html` →
  `<dd ...>Billing name, IČO (8 digits) and billing address. DIČ if you are a VAT payer. We need these to issue a valid Czech tax doklad. You enter them once — we keep them on file.</dd>`

### F-UX-026 — EN pricing card decimal uses comma (CS convention) instead of period
- **Where**: `https://assoluto.eu/pricing` EN — Enterprise card, last bullet:
  `SLA 99,9 %`. Likely the literal-`%` patch from F-UX-020 hard-coded the CS
  comma form across all three locales rather than localizing the decimal
  separator.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: English convention is `99.9 %` (period decimal). Czech and
  German both use `99,9 %` (comma decimal), so CS and DE are correct, but EN
  reads as a typo to a native English-speaker. Small but pre-launch polish.
  Note the homepage FAQ has the inverse problem (writes "99.9 percent" in EN
  with period — so the localized form already exists in EN strings; the pricing
  card just needs to follow it).
- **Suggested fix**: Either (a) wrap the literal `99,9` in a tiny conditional
  `{% if locale == 'en' %}99.9{% else %}99,9{% endif %} %`, or (b) add the
  whole `SLA 99,9 %` / `SLA 99.9 %` string back into a translatable msgid that
  uses `%%` correctly per locale. Option (a) is the simpler local fix.
- **Evidence**: `grep "SLA 99" /tmp/pricing-en2.html` → `<li ...>SLA 99,9 %</li>`.

### F-UX-027 — DE contact form mixes German opening quote with ASCII closing quote
- **Where**: `https://assoluto.eu/contact` (DE locale). The microcopy `Schreiben Sie
  „Demo" in die Nachricht` under the "Email" contact tile.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Hex dump of the rendered string shows `e2 80 9e 44 65 6d 6f 22`
  — i.e. correct German opening quote `„` (U+201E) followed by ASCII straight
  double-quote `"` (U+0022). German typography closes with `"` (U+201C — left
  double quotation mark, used as German closing). The mixed pair is a typography
  flaw the same way F-UX-007 / F-UX-012 caught straight close-quotes in CS in
  the previous run; this one slipped through the quote sweep because it's only
  two characters and only on the contact page.
- **Suggested fix**: In the DE msgstr for the corresponding msgid in
  `app/locale/de/LC_MESSAGES/messages.po`, replace the ASCII `"` with `"`
  (U+201C). Recompile catalog. Add `„text"` (mixed-pair) to the lint added by
  `d914e3e` if there is one — defensive.
- **Evidence**: `grep "Demo" /tmp/contact-de2.html | xxd` shows
  `Schreiben Sie ..` + `.Demo"` (the opening byte triple `e2 80 9e` then the
  closing single byte `22`).

### F-UX-028 — `/favicon.ico` and `/static/favicon.ico` both 404 (PERSISTS from F-UX-004)
- **Where**: `https://assoluto.eu/favicon.ico` and `…/static/favicon.ico`.
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Same finding as F-UX-004 from `2026-05-01-1455` and the
  prior run. Browser tab + bookmark UI shows the generic globe icon instead of
  the Assoluto mark. Brand polish issue, not a flow blocker. Restating because
  the previous run filed it as `manual` / deferred but it's auto-fixable in
  one commit (drop the file in `app/static/`, add a `<link rel="icon">` to
  `base.html`).
- **Suggested fix**: Add `app/static/favicon.ico` (16×16 + 32×32 multi-res)
  generated from the existing `app/static/og/assoluto-og.png` brand source.
  Add `<link rel="icon" type="image/x-icon" href="/static/favicon.ico">` and a
  modern `<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">`
  in `app/templates/_base.html`. Optionally add `apple-touch-icon` PNGs.
- **Evidence**: `curl -sS -I https://assoluto.eu/favicon.ico` → `HTTP/2 404`.

---

## Persisted (manual) findings — confirmed still open, not regressions

- **F-UX-016** (P1) — hreflang alternates all point to canonical URL. Confirmed
  on `/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, plus
  in `sitemap.xml`. Needs URL-architecture decision. No code action this run.
- **F-UX-021** (P2) — pricing in CZK only on EN+DE. Confirmed on EN/DE pricing
  card render — no parenthetical EUR equivalent. Editorial decision pending.
- **F-UX-011** — no authenticated walkthrough. Credentials not provided this
  run; `/app`, `/app/orders`, `/app/customers`, `/app/products`,
  `/app/admin/users`, `/app/admin/audit`, `/app/admin/profile`, and the
  `/platform/admin/*` operator pages were not exercised. `/app` returns 303 →
  `/auth/login?next=/app` for HTML clients (correct). Operator pages return
  401 unauthenticated (correct).

---

## Walkthrough log

Verification mode — only HTML/curl observation. Items below confirm "no
finding" = positive evidence the surface is clean for the listed checks.

- **`https://assoluto.eu/`** (CS / EN / DE via `Accept-Language`):
  - Title localized correctly per locale.
  - `<html lang="…">` matches the served locale.
  - Hero `Stop picking up the phone.` renders in EN (and equivalents in CS/DE).
  - Step 1 — F-BIZ-012 fix held: "Email, password, company name" / "E-mail, heslo,
    název firmy" / "E-Mail, Passwort, Firmenname". No IČO mention.
  - FAQ — F-UX-024 found: `99.9 percent` / `99,9 procent` / `99,9 Prozent` still
    in the security/uptime answer body and in the ld+json `acceptedAnswer`.
  - OG image present + reachable (`/static/og/assoluto-og.png` returns 200).
- **`/pricing`** (CS / EN / DE):
  - All four plan cards render (Community, Starter, Pro, Enterprise).
  - Enterprise: `Priority support (4 h business hours, written SLA)` + `SLA 99,9 %`
    — F-BIZ-014 + F-UX-020 held.
  - F-UX-026 found: EN renders `SLA 99,9 %` with comma decimal — should be `99.9 %`.
  - F-BIZ-013 FAQ entry "What do I need to enter at first paid checkout?" present
    in CS / EN / DE with billing-name / IČO / DIČ / address copy.
  - F-UX-025 found: EN body says "Czech tax doklad" — Czech-word leak.
  - 7 FAQ entries total, all rendered in all three locales.
- **`/features`** (CS / EN / DE) — 200, title localized, no console-style errors
  visible in HTML.
- **`/self-hosted`** (CS / EN / DE) — 200, title localized
  (`Vlastní hosting` / `Self-hosted` / `Self-hosted` for DE — DE matches EN
  here, possibly editorial).
- **`/contact`** (CS / EN / DE):
  - 200 in all three locales.
  - Honeypot `<input name="website">` rendered inside an offscreen
    `aria-hidden="true"` div with `tabindex="-1"` + `autocomplete="off"` —
    F-UX-019 held.
  - Form fields use proper `autocomplete="name"` / `autocomplete="email"`.
  - F-UX-027 found: DE microcopy `„Demo"` mixes German opening quote with
    ASCII closing quote.
  - Form NOT submitted per agent rules.
- **`/terms` / `/privacy` / `/cookies`** — 200, titles localized in CS/EN/DE.
  No `%%` Jinja trap regression on `/terms` (F-UX-001 stayed fixed).
- **`/robots.txt`** — `/platform/signup` removed from Disallow; `/platform/login`
  and `/platform/admin` remain disallowed. F-UX-022 held.
- **`/sitemap.xml`** — 200, all 7 URLs include the (canonical-only) hreflang
  alternates mentioned by F-UX-016.
- **`/healthz` / `/readyz`** — both return 200 + `{"status":"ok"}`. Cookie set
  is `csrftoken; Path=/; SameSite=lax; Secure` — correct.
- **`/.well-known/security.txt`** — 404. Out of scope for this audit's contract,
  but worth noting for a future polish pass.
- **`/platform/login`** (CS / EN / DE) — 200, title + h1 localized. Form fields
  have `name="email"` + `name="password"` + CSRF hidden. Submit button uses
  `bg-brand-600`. F-UX-023 found: language switcher links use `text-blue-*`.
- **`/platform/signup`** (CS / EN / DE) — 200, honeypot present + offscreen + tab-skipped.
  Field autocomplete attrs correct (`organization`, `name`, `username`,
  `new-password`). Hidden `plan` value renders. Submit `bg-brand-600`. Same
  F-UX-023 lang-switcher leak.
- **`/platform/check-email`** (CS / EN / DE) — 200, title localized correctly
  (`Ověřit e-mail` / `Verify your email` / `E-Mail bestätigen`). Form renders.
- **`/platform/password-reset`** (EN) — 200, title localized.
- **`/platform/select-tenant`** + **`/platform/verify-sent`** — return 401
  unauthenticated (correct; would be 303 with HTML accept).
- **`/platform/admin/dashboard`** + **`/platform/admin/tenants`** — 401
  unauthenticated. Cannot verify the Plan / Billing status / Period ends columns
  this run because no operator session was started; would-have-tested-but
  flagged.
- **HEAD verification** — `HEAD` returns 200 on all marketing + auth + sitemap
  endpoints tested. F-UX-017 held.
- **Tenant `https://test-a.assoluto.eu/auth/login`** — 200, primary CTA uses
  `bg-brand-600` + `focus:ring-brand-500/50`. Form fields use brand. F-UX-023
  found: 2× `text-blue-*` lang-switcher links remain. Light-mode HTML render
  inspected; dark-mode visual not exercised (no Chrome MCP this run).
- **Tenant `…/auth/password-reset`** — same pattern as login: brand on primary
  CTA, blue on lang-switcher.
- **`https://test-a.assoluto.eu/`** (tenant root) — 200; redirect chain not
  tripped; no `ERR_TOO_MANY_REDIRECTS`-shape behaviour observed.
- **`/app`** (unauth) — returns 303 → `/auth/login?next=/app` with HTML accept,
  401 JSON without (FastAPI default). Both correct.
- **Mobile / dark-mode visual** — NOT exercised this run (Chrome MCP unavailable).
  Dark-mode regressions would need a follow-up visual run.


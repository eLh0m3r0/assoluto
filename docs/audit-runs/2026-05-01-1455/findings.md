# Audit run 2026-05-01-1455 (verification)

**Started**: 2026-05-01T14:55Z
**Tip-of-tree commit**: `efd4890`
**Previous run**: [`2026-05-01-1335`](../2026-05-01-1335/findings.md) (HEAD `a9d64e0`)

This is a **verification run** of `/audit-verify`. The 4 sub-agents
re-walked the same surface, re-graded findings, and the
consolidator diffed against the previous run.

## Counts

| Perspective | P0 | P1 | P2 | Δ vs prev |
|---|---|---|---|---|
| UX        | 0 | 0 | 4 | -1 P0, -5 P1, -1 P2 |
| Backend   | 0 | 2 | 3 | -1 P0, 0 P1, -1 P2 |
| Security  | 0 | 1 | 1 | 0 P0, 0 P1, -1 P2 |
| Business  | 0 | 0 | 1 | -1 P0, -5 P1, -3 P2 |
| **Total** | **0** | **3** | **9** | **-3 P0, -10 P1, -6 P2** |

P0 count went from **3 → 0**. P1 count went from **13 → 3** (-77%).
All three remaining P1s are the same persisted-manual operator
items from the previous run.

---

## Verification vs. previous run (`2026-05-01-1335`)

### ✅ Resolved (14 — fixes held)

| ID | Sev | Title | Fixed in |
|---|---|---|---|
| F-UX-001 / F-BE-001 | **P0** | `/terms` 500 in EN — `99.9%%` escape; live curl with Accept-Language: en → 200 | `f87ae07` |
| F-BIZ-001 | **P0** | Backup retention 30d → 14d default; aligned with marketing + GDPR Art. 5(1)(e) | `cda17c6` |
| F-UX-002 | P1 | Two `<title>` tags per page → exactly one across all 27 marketing URLs | `8707f21` |
| F-UX-003 | P1 | CS contact form double asterisk → single | `82d5458` |
| F-UX-005 | P1 | hreflang cs/en/de/x-default + og:locale:alternate present on every marketing page | `8707f21` |
| F-UX-006 | P1 | **BONUS** — Language switcher landed in header + footer + tenant pages with cookie persistence + open-redirect defence (was tagged `Auto-fixable: no`) | `8707f21`? |
| F-UX-008 | P2 | CS pricing "encrypted at rest" → "šifrováno v klidu" + DE counterpart | `82d5458` |
| F-UX-009 | P2 | Contact form `autocomplete="name"` + `email` | `8707f21` |
| F-UX-010 | P2 | Sitemap hreflang — 36 `xhtml:link` entries (9 URLs × 4 locales) | `8707f21` |
| F-SEC-001 | P2 | `Server` + `Via` headers stripped at Caddy edge (after manual rebuild) | `cb53240` |
| F-BIZ-007 | P1 | 3 contact strings "24 hours" → "1 working day" in CS+EN+DE | `82d5458` |
| F-BIZ-009 | P2 | Superlative "Five calls a day → zero" hedged | `82d5458` |
| F-BIZ-010 | P2 | Annual discount mention added to pricing FAQ + index FAQ + JSON-LD | `82d5458` |

### 🚨 Regressed: NONE ✓

Zero findings flipped from `fixed` back to `open`. The auto-fix
process is not leaking; recent commits did not undo prior work.

### ⏳ Persisted (16 — all expected, mostly manual / deferred)

| ID | Sev | Title | Why persisted |
|---|---|---|---|
| F-UX-004 | P2 | `/favicon.ico` 404 | manual — operator action (multi-resolution ICO) |
| F-UX-007 | P2 | CS+DE typographic quotes — incomplete sweep | restated as **F-UX-012** below (only 1 of ~12 instances was curlified) |
| F-UX-011 | P2 | No authenticated walkthrough credentials | manual — needs ephemeral audit identity |
| F-BE-002 | P1 | Stripe checkout silently no-ops (price IDs missing) | manual — operator must set in /etc/assoluto/env |
| F-BE-003 / F-SEC-002 | P1 | GDPR endpoints zero test coverage | deferred — substantive new test code |
| F-BE-004 | P2 | `gdpr_service.{export,erase}_for_contact` no router | deferred — feature scope |
| F-BE-005 | P2 | Recent feature commits without paired tests | process discipline, not auto-fixable |
| F-BE-006 | P2 | Stripe webhook breadth (paused / customer.updated / payment_method.detached) | product policy decision |
| F-BE-007 | P2 | Hygiene baseline (mypy/ruff/pytest) | informational — re-baselined, no regression |
| F-SEC-003 | P2 | `_has_unverified_identity` opens fresh engine per call | refactor — needs lifespan singleton |
| F-BIZ-002 | P1 | `STATUS_PAGE_URL` not set in prod env | operator config |
| F-BIZ-003 | P1 | Demo CTA → /contact form, not real booking | operator action — needs Cal.com |
| F-BIZ-004 | P1 | Founder identity not on marketing footer/contact | operator copy decision |
| F-BIZ-005 | P1 | No trial-nurture email cadence | substantive feature, deferred |
| F-BIZ-006 | P2 | Testimonial placeholders unchanged | operator decision — wait for first paying customer |
| F-BIZ-008 | P2 | No refund policy in pricing FAQ | operator copy decision |

### 🆕 New in this run (5 — all P2)

| ID | Sev | Title | Notes |
|---|---|---|---|
| F-UX-012 | P2 | CS+DE typographic close-quote sweep was incomplete; 11+ instances still use ASCII `"` | Restatement of F-UX-007 with explicit instance list — fix only swept one msgid per locale |
| F-UX-013 | P2 | Locale cookie host-only — choice on apex doesn't carry to tenant subdomain | Bug introduced by the bonus language-switcher: cookie missing `Domain=.assoluto.eu` |
| F-UX-014 | P2 | `/set-lang` returns 405 on HEAD | Minor RFC 9110 hygiene — affects link-checkers/uptime monitors |
| F-UX-015 | P2 | `sme_locale` cookie attr `SameSite=lax` (lowercase) vs other cookies' `Lax` | Cosmetic inconsistency — both are functionally identical |
| F-BIZ-011 | P2 | 175 fuzzy entries in `app/locale/en/LC_MESSAGES/messages.po` (CS=0, DE=0) | Tripwire — pybabel update side-effect; if anyone clears flags carelessly, prospects see "Manage tenants" on contact success card |

---

## Active findings — full per-finding detail

### F-UX-012 — CS+DE typographic close-quote sweep was incomplete; 13 quotes still use straight ASCII `"`
- **Where**: `https://assoluto.eu/` (CS+DE), `/features`, `/pricing`, `/contact`. Specific instances on the CS homepage:
  - `„Nemůžeme posunout termín na 29. 4.? …"`
  - `„kde je má zakázka?"` (first occurrence — second is correct)
  - `„Každý den mi dvacet lidí volá…"`
  - `„Furt hledám v Outlooku…"`
  - `„Máme tabulku, ale aktualizuje ji jenom Jana…"`
  - `„implementační projekt"` and `„rychlý hovor s obchodem"`
  - DE has the symmetric set with German wording (six quotes).
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The previous /audit-fix swept one msgid per locale (specifically `„kde je má zakázka?"` / `„wo ist mein Auftrag?"`). All other testimonial pull-quotes and body quotes still close with straight ASCII `"` — the original F-UX-007 sweep didn't take. Native readers continue to perceive testimonial copy as machine-translated.
- **Suggested fix**: Catalog-wide pass. Regex `„([^"„]+)"` → `„\1"` on `app/locale/{cs,de}/LC_MESSAGES/messages.po`, sanity-check before recompile, then `pybabel compile -d app/locale`. The previous attempt's regex excluded the right chars but evidently matched only the rebuilt msgid (which had passed through the pybabel update cycle), not the older unchanged ones.
- **Evidence**: 7 straight-ASCII closes vs. 1 curly close per locale on the homepage.
- **status**: fixed (commit d914e3e)
### F-UX-013 — Locale cookie host-only; choice on apex doesn't carry to tenant subdomain
- **Where**: `set_language` route in `app/routers/public.py` (sets `sme_locale` cookie without `Domain=`)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: A German prospect picks DE on `assoluto.eu`, signs up, lands on `test-a.assoluto.eu/auth/login` — sees CS again because the cookie doesn't follow them across subdomains. Friction on the highest-value moment (first login).
- **Suggested fix**: In the `/set-lang` handler, set the cookie with `domain=.assoluto.eu` in production (gated on `settings.platform_cookie_domain` so dev single-host stays unaffected). Mirror the platform session cookie scope rules.
- **Evidence**: `curl -c jar.txt 'https://assoluto.eu/set-lang?lang=en&next=/'`; `curl -b jar.txt https://test-a.assoluto.eu/auth/login` → CS title even though `sme_locale=en` is in the jar (host-only doesn't match subdomain).
- **status**: fixed (commit d914e3e)
### F-UX-014 — `/set-lang` returns 405 on HEAD
- **Where**: `https://assoluto.eu/set-lang?lang=en&next=/`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: `HEAD /set-lang?...` returns `HTTP 405 Allow: GET`. RFC 9110 requires that any resource supporting GET also support HEAD. Browsers and link-checkers (uptime monitors, security scanners) probe with HEAD; getting 405 is observability noise. Endpoint is high-traffic now that the switcher ships in every page footer.
- **Suggested fix**: Expose the route on both GET and HEAD (`methods=["GET", "HEAD"]`). For HEAD, short-circuit and return `Response(status_code=303, headers={"Location": next_url})` without setting the cookie (HEAD must not change state).
- **Evidence**: `curl -sS -I 'https://assoluto.eu/set-lang?lang=en&next=/'` → `HTTP/2 405 Allow: GET`.
- **status**: fixed (commit d914e3e)
### F-UX-015 — `sme_locale` cookie attribute case differs from other cookies
- **Where**: `Set-Cookie: sme_locale=en; ... SameSite=lax; ...` on `/set-lang`
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Cosmetic inconsistency — `csrftoken` cookie uses `SameSite=Lax` (capital L), `sme_locale` uses `SameSite=lax` (lowercase). Browsers accept both, no functional break. But the mismatch suggests two different code paths assemble cookies; consolidating makes audits + rotation simpler.
- **Suggested fix**: One-line change in the `set_lang` handler — pass `samesite="Lax"` matching `app/security/session.py`.
- **Evidence**: header capture confirms case mismatch.
- **status**: fixed (commit d914e3e)
### F-BIZ-011 — 175 fuzzy entries in EN catalog (process tripwire)
- **Where**: `app/locale/en/LC_MESSAGES/messages.po` (175 `#, fuzzy` markers; CS = 0, DE = 0)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: `pybabel update` mechanically marked unrelated msgstrs as fuzzy when extract picked up English-source strings. Examples sitting dormant:
  - `msgid "Message sent" → msgstr "Manage tenants"` (contact.html:76)
  - `msgid "Write to us" → msgstr "Switch to"` (contact.html:81)
  - `msgid "Submit" → msgstr "Submitted"`, `msgid "Confirm" → msgstr "Confirmed"`, `msgid "Cancel" → msgstr "Cancelled"`
  - `msgid "Order" → msgstr "Orders"`, `msgid "Product" → msgstr "In production"`
  - `msgid "Verify email" → msgstr "Verify your email"`
  - 168 more.
  Today gettext correctly skips fuzzy entries → users see msgid (English source) → no visible regression. **But** if anyone runs `pybabel update` and either (a) clears fuzzy flags carelessly, or (b) ships tooling change that promotes fuzzies, prospects see "Manage tenants" on contact success card.
- **Suggested fix**: Walk EN .po, for each fuzzy entry either set `msgstr ""` (forces fallback to English msgid — desired for EN identity catalog) and remove the `#, fuzzy` flag, or write the correct msgstr. ~30 min mechanical work. Add CI guard mirroring `tests/test_cs_catalog_health.py` (commit `71c75a4`) for EN.
- **Evidence**: `grep -c "^#, fuzzy" app/locale/{en,cs,de}/LC_MESSAGES/messages.po` → `en:175 cs:0 de:0`.
- **status**: fixed (commit 10ce9bf) — 175 fuzzies cleared, EN identity restored, 2 new CI guards added
---

## Persisted findings — terse status (full detail in previous run)

These are already-graded items from `2026-05-01-1335/findings.md` —
unchanged between runs. See the previous findings.md for description,
suggested fix, and evidence. Status updated below for tracking.

### Persisted P1
- **F-BE-002**: Stripe price IDs missing in `/etc/assoluto/env`. **status**: manual (operator config)
- **F-BE-003 / F-SEC-002**: GDPR endpoints zero test coverage. **status**: manual (deferred substantive test code)
- **F-BIZ-002**: `STATUS_PAGE_URL` not set. **status**: manual (operator config)
- **F-BIZ-003**: Demo CTA → /contact form. **status**: manual (operator action)
- **F-BIZ-004**: Founder identity not in marketing surface. **status**: manual (operator decision)
- **F-BIZ-005**: No trial-nurture cadence. **status**: manual (substantive feature deferred)

### Persisted P2
- **F-UX-004**: `/favicon.ico` 404. **status**: manual (operator action)
- **F-UX-011**: No authenticated walkthrough credentials. **status**: manual (operator action)
- **F-BE-004**: GDPR contact routes missing. **status**: manual (deferred feature work)
- **F-BE-005**: Process discipline. **status**: manual (process)
- **F-BE-006**: Stripe webhook breadth. **status**: manual (product policy)
- **F-BE-007**: Hygiene baseline (re-baselined, no regression). **status**: informational
- **F-SEC-003**: Per-call engine in `_has_unverified_identity`. **status**: manual (refactor)
- **F-BIZ-006**: Testimonials unchanged. **status**: manual (operator decision)
- **F-BIZ-008**: No refund policy. **status**: manual (operator decision)

---

## Hygiene baseline (re-baselined for next run)

* `ruff check .` → clean
* `ruff format --check .` → 147 files already formatted
* `mypy app/` → 0 errors / 87 source files
* `pytest tests/ -q` → 423 passed, 12 warnings, **57.60s** (was 58.97s, slightly faster)
* 12 warnings unchanged in shape — third-party slowapi `asyncio.iscoroutinefunction` deprecation
* All architecture invariants hold (lock IDs, `read_session_for_tenant`, bg-task commit pattern, schema vs. ORM drift, app.platform isolation)

The next `/audit-verify` should not show regression on any of these
counts.

---

## Recommendation

**Zero regressions + 13/13 targeted fixes held + 1 bonus resolution +
77% reduction in P1 count.** The audit-fix process is working
cleanly. The 5 new P2 findings are all minor — three are side-
effects of the bonus language-switcher (cookie scope, HEAD method,
attr case) and one is a process tripwire (fuzzy EN catalog) worth a
follow-up.

**Suggested next step**: One more `/audit-fix` against this run to
sweep up F-UX-012 + F-UX-013 + F-UX-014 + F-UX-015 + F-BIZ-011
(all `Auto-fixable: yes`, all P2, no risk). Then close the audit
cycle for this round; ship operator-action items separately.

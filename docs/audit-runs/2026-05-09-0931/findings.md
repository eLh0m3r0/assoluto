# Audit run 2026-05-09-0931 — verification

**Started**: 2026-05-09T09:31:00+02:00
**Tip-of-tree commit**: `d3d911e`
**Previous run**: [`2026-05-09-0829`](../2026-05-09-0829/findings.md)
**Mode**: verification of the audit-fix cycle that closed 12 of 19 findings from the previous run.

## Counts (new findings this run only)

| Perspective | P0 | P1 | P2 |
|---|---|---|---|
| UX        | 0 | 0 | 6 |
| Backend   | 0 | 0 | 2 |
| Security  | 0 | 0 | 1 |
| Business  | 0 | 0 | 0 |
| **Total** | **0** | **0** | **9** |

**Zero regressions.** Every previously-fixed finding still passes its regression check. F-UX-018 and F-UX-020 are flagged as **partial** — the fix held on the surface that was the original example, but sibling surfaces with the same root cause were missed. That's a scope gap, not a regression: the original assertion still holds.

> Status legend: `open`, `fixed`, `wontfix`, `manual`. New findings start as `open`. The `/audit-fix` skill flips them to `fixed` when applied.

---

## P0 — must fix before next deploy

*None.*

---

## P1 — fix this sprint

*None new.* Two P1s persist as `manual` (operator action) — see Verification block below.

---

## P2 — backlog

### [UX] F-UX-023 — `text-blue-*` link leak in auth-shell language switcher (F-UX-018 scope gap)
- **Where**: `https://test-a.assoluto.eu/auth/login`, `…/auth/password-reset`, `https://assoluto.eu/platform/login`, `…/platform/signup`. Likely a shared `_lang_switcher.html` partial that the `5263a05` sweep did not include.
- **Auto-fixable**: yes
- **status**: open
- **Description**: F-UX-018 swept `bg-blue-*` on tenant auth pages but missed the language-switcher links at the bottom. Each rendered page still has 2× `class="font-semibold text-blue-600 dark:text-blue-400"` (one per non-active locale). Across 4 pages, 8 stranded blue references. Same brand-consistency drift the original finding flagged.
- **Suggested fix**: Find the partial that renders those `<a>` links and replace `text-blue-600 dark:text-blue-400` → `text-brand-600 dark:text-brand-400`. One template, ~2 swaps; cascades to all four pages.

### [UX] F-UX-024 — Homepage FAQ still spells "percent" / "procent" / "Prozent" (F-UX-020 scope gap)
- **Where**: `app/templates/www/index.html` FAQ — answer to "Bezpečnost a výpadky?". Renders in the visible `<dd>` AND inside the JSON-LD `FAQPage.acceptedAnswer.text`.
- **Auto-fixable**: yes
- **status**: open
- **Description**: F-UX-020 fixed the Enterprise pricing card to use the literal `%` glyph via the split-form pattern. The homepage FAQ entry that contains the identical phrasing — `Cílová dostupnost 99,9 procent` (CS), `Target uptime: 99.9 percent` (EN), `Zielverfügbarkeit: 99,9 Prozent` (DE) — was not touched. Same `%%` Jinja trap workaround still in use.
- **Suggested fix**: Same split-form pattern: render `99,9 %` (or `99.9 %` for EN) outside the gettext call. Update CS/EN/DE msgids; `pybabel extract && update`. Both the visible `<dd>` and the JSON-LD string need updating (same source).

### [UX] F-UX-025 — `tax doklad` Czech-word leak in EN pricing FAQ
- **Where**: `https://assoluto.eu/pricing` (EN), Pricing FAQ entry "What do I need to enter at first paid checkout?". Body string is the EN msgstr from commit `35c2b03`.
- **Auto-fixable**: yes
- **status**: open
- **Description**: EN body reads `"...We need these to issue a valid Czech tax doklad."` — the Czech word `doklad` was pasted untranslated into the English copy. An English-speaking prospect sees what looks like a typo or an unfamiliar brand term.
- **Suggested fix**: Replace `tax doklad` → `tax invoice` in the EN msgstr. DE msgstr is fine (`Steuerbeleg`). One-line change, then `pybabel compile`.

### [UX] F-UX-026 — EN pricing card decimal uses comma (CS convention) instead of period
- **Where**: `https://assoluto.eu/pricing` EN — Enterprise card last bullet: `SLA 99,9 %`. The literal-`%` patch from F-UX-020 hard-coded the CS comma form across all three locales rather than localising the decimal separator.
- **Auto-fixable**: yes
- **status**: open
- **Description**: English convention is `99.9 %` (period decimal). CS and DE are correct, EN reads as a typo to a native English-speaker. The homepage FAQ (F-UX-024) has the inverse problem — the period form already exists in EN strings; the pricing card should follow it.
- **Suggested fix**: (a) `{% if locale == 'en' %}99.9{% else %}99,9{% endif %} %` next to the `_("SLA")` call; (b) put the whole `SLA 99,9 %` / `SLA 99.9 %` back into a translatable msgid using `%%` per locale. Option (a) is simpler.

### [UX] F-UX-027 — DE contact form mixes German opening quote with ASCII closing quote
- **Where**: `https://assoluto.eu/contact` (DE) — microcopy `Schreiben Sie „Demo" in die Nachricht` under the Email contact tile.
- **Auto-fixable**: yes
- **status**: open
- **Description**: Hex dump shows `e2 80 9e Demo 22` — correct German opening `„` (U+201E) followed by ASCII straight `"` (U+0022). German typography closes with `"` (U+201C). Same shape as F-UX-007/F-UX-012 caught for CS in the previous run; this slipped through the quote sweep because it's only two characters and only on the contact page.
- **Suggested fix**: Replace the ASCII `"` with `"` (U+201C) in the DE msgstr. Recompile. Defensive: extend the quote sweep linter (commit `d914e3e`) to catch mixed `„text"` pairs.

### [UX] F-UX-028 — `/favicon.ico` still 404 (PERSISTS from F-UX-004)
- **Where**: `https://assoluto.eu/favicon.ico` and `…/static/favicon.ico`.
- **Auto-fixable**: yes
- **status**: open
- **Description**: Same finding as F-UX-004 from `2026-05-01-1455` and the previous run. Browser tab shows the generic globe icon instead of the Assoluto mark. Restated as auto-fixable because it's actually a one-commit drop-in.
- **Suggested fix**: Add `app/static/favicon.ico` (16×16 + 32×32 multi-res) generated from `app/static/og/assoluto-og.png`. Add `<link rel="icon" type="image/x-icon" href="/static/favicon.ico">` and `<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">` in `app/templates/_base.html`.

### [BE] F-BE-010 — Audit-row test asserts existence only, not actor or diff
- **Where**: `tests/test_billing_details.py:204-215`
- **Auto-fixable**: yes
- **status**: open
- **Description**: The happy-path billing-details test asserts that an `audit_events` row with `action='tenant.settings_updated'` is created, but does NOT assert the `actor_*` columns point at the TENANT_ADMIN user (vs the platform Identity), nor that the `before` / `after` JSON columns carry the four billing keys with the right values. A future refactor that flips the actor or drops a key from the diff would land silently. Production code does the right thing today; the test is the regression net.
- **Suggested fix**: Extend the existing `SELECT` to read `actor_type, actor_id, actor_label, before_data, after_data` and assert `actor_type == 'user'`, `before_data['billing_ico'] == ''`, `after_data['billing_ico'] == '12345678'`. About 8 lines.

### [BE] F-BE-011 — pytest wall time 62.75 s exceeds 60 s budget (advisory)
- **Where**: full suite, current machine
- **Auto-fixable**: no
- **status**: open (advisory)
- **Description**: Full pytest run takes 62.75 s vs 55.48 s previously. Slowdown is entirely the 30 new tests landing — slowest-10 unchanged in shape (long-tail throttle eviction tests dominate). Listed because the agent contract calls out the 60 s threshold; not a quality issue.
- **Suggested fix**: Leave alone unless wall-time creeps past 90 s. If/when the long-tail throttle eviction tests need attention, gate them behind `@pytest.mark.slow` and skip in CI fast-paths.

### [SEC] F-SEC-002 — `HeadMethodMiddleware` rewrites `scope["method"]` BEFORE the route runs, neutering the explicit HEAD guard on `/set-lang`
- **Where**: `app/security/head_method.py:33` (the `{**scope, "method": "GET"}` rewrite) ↔ `app/routers/public.py:363` (the now-dead `if request.method == "HEAD": return response` guard).
- **Auto-fixable**: yes
- **status**: open
- **Description**: HeadMethodMiddleware mutates `scope["method"]` to GET before dispatch. Starlette's `Request.method` reads from `scope["method"]`, so by the time `set_language()` runs, `request.method == "HEAD"` is always False. The defensive HEAD-no-mutate guard at `public.py:363` is now unreachable. Today's behaviour: a HEAD probe to `/set-lang?lang=de&next=/` will set the `sme_locale` cookie of any client that accepts cookies on HEAD responses. Threat model is low (locale preference, not security context); the fix is about not silently neutralising existing HEAD guards.
- **Suggested fix**: Stash the original method on `scope`. In `head_method.py`: `rewritten = {**scope, "method": "GET", "_original_method": "HEAD"}`. Add a helper `original_method(request) -> str` returning `scope.get("_original_method") or request.method`. Update the `set-lang` guard to use it. Alternative: scope the middleware to skip routes that explicitly declare `methods=["GET", "HEAD"]`.

---

## Verification vs. previous run (`2026-05-09-0829`)

### Resolved (fixes held)
- **F-BIZ-012** (P0) — Homepage IČO promise dropped (fixed in `35c2b03`) — confirmed CS/EN/DE
- **F-BIZ-013** (P1) — Pricing FAQ IČO entry (fixed in `35c2b03`) — confirmed CS/EN/DE, copy specific
- **F-BIZ-014** (P1) — Enterprise SLA = 4 h written (fixed in `35c2b03`) — confirmed CS/EN/DE
- **F-BE-001** (P1) — billing-details audit row (fixed in `176c4bf`) — emits with correct actor + diff
- **F-BE-006 / F-SEC-001** (P2) — `_safe_error_summary` regex hardening (fixed in `29994d0`) — 6 new tests pass
- **F-BE-007** (P2) — verify-gate regression tests (fixed in `0eb0a56`) — 3 tests pass
- **F-BE-008** (P2) — billing-details regression tests (fixed in `0eb0a56`) — 11 tests pass
- **F-UX-017** (P2) — HEAD-from-GET middleware (fixed in `db45cf5`) — HEAD returns 200 on every walked URL
- **F-UX-018** (P2) — Tenant `/auth/login` brand-color (fixed in `5263a05`) — **partial; see F-UX-023**
- **F-UX-019** (P2) — Contact form honeypot (fixed in `0782f01`) — confirmed offscreen + tab-skipped
- **F-UX-020** (P2) — Pricing SLA `%` (fixed in `35c2b03`) — **partial; see F-UX-024**
- **F-UX-022** (P2) — robots.txt allows `/platform/signup` (fixed in `de17b9a`) — confirmed live

### Persisted (open in both runs — manual / operator action)
- **F-UX-016** (P1) — hreflang URLs all canonical — needs URL architecture decision
- **F-BE-002** (P1) — Stripe price IDs NULL in prod — operator must set `STRIPE_PRICE_*`
- **F-BE-003** (P1) — GDPR test design (deferred — needs proper test cases)
- **F-BE-004** (P2) — GDPR contact routes (product decision)
- **F-BE-005** (P2) — Stripe webhook scope (product judgement)
- **F-BE-009** (P2) — comment-author render assertion (advisory)
- **F-UX-021** (P2) — CZK-only display on EN+DE (editorial decision)
- **F-BIZ-002** — status page URL (operator action)
- **F-BIZ-003** — demo CTA / Cal.com booking
- **F-BIZ-004** — founder bio on `/`
- **F-BIZ-005** — trial-nurture email cadence (product decision)
- **F-BIZ-006** — testimonials (need real customer)
- **F-BIZ-008** — refund-policy marketing copy (legal decision)

### Regressed (came back!)
*None.* Every previously-fixed finding still passes its regression check. No architecture invariant was broken by this audit-fix cycle.

### New in this run
- **F-UX-023** (P2) — `text-blue-*` lang-switcher leak (F-UX-018 scope gap)
- **F-UX-024** (P2) — homepage FAQ still spells "percent" (F-UX-020 scope gap)
- **F-UX-025** (P2) — `tax doklad` Czech-word leak in EN pricing FAQ
- **F-UX-026** (P2) — EN pricing card uses comma decimal `99,9 %`
- **F-UX-027** (P2) — DE contact form mixes German `„` with ASCII `"`
- **F-UX-028** (P2) — `/favicon.ico` 404 (restated as auto-fixable)
- **F-BE-010** (P2) — audit-row test asserts existence only
- **F-BE-011** (P2) — pytest wall time 62.75 s (advisory)
- **F-SEC-002** (P2) — HEAD-middleware nullifies `set-lang` HEAD guard

### Manual / operator action (carried over, summary)
- Set `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` in `/etc/assoluto/env` (F-BE-002)
- Decide URL architecture for hreflang locales (F-UX-016)
- Decide CZK-only vs dual-currency display on EN+DE pricing (F-UX-021)
- Design GDPR endpoint test cases (F-BE-003)
- Decide self-service vs org-admin override for contact GDPR (F-BE-004)
- Extend Stripe webhook handler set per product judgement (F-BE-005)
- Stand up status page (F-BIZ-002), Cal.com booking (F-BIZ-003), founder bio (F-BIZ-004), trial-nurture cadence (F-BIZ-005), testimonials (F-BIZ-006), refund policy copy (F-BIZ-008)

---

## Recommendation

**Fixes held; consider closing the audit cycle.** Twelve targeted findings closed cleanly, zero regressions, no architecture invariant broken (CLAUDE.md §2 / §6 / §13 / §17 all green). The 9 new P2s are either scope gaps from over-narrow `/audit-fix` patches (F-UX-023/024 — sibling surfaces with the same root cause), polish items the verification surfaced for the first time (F-UX-025/026/027/028), or coverage hardening (F-BE-010, F-SEC-002).

**Two follow-up patterns worth noting** for future `/audit-fix` runs:

1. **Scope gaps (F-UX-018, F-UX-020).** When a finding cites one specific surface but the root cause is shared, the auto-fix should sweep the codebase for the pattern, not edit the literally-named template. F-UX-018 said "tenant `/auth/login` uses bg-blue-*" and the fix touched only that template — but a shared lang-switcher partial included by 4 pages still has the same colour. F-UX-020 said "pricing.html uses `percent`" — but homepage FAQ has the same bug. Both need a one-commit cleanup.
2. **Test coverage breadth (F-BE-010).** The fix landed 11 new tests but the audit-row assertion only checks existence. A future refactor that flipped the actor would land silently. Worth tightening.

Both follow-ups are auto-fixable. Run `/audit-fix` once more to close the new P2s, then close the cycle.

## Status legend

Each finding starts as `status: open`. The `/audit-fix` command updates this in place to `fixed`, `wontfix`, or `manual` (operator action required, not auto-fixable).

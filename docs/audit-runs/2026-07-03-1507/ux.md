# UX audit — 2026-07-03-1507

**Started**: 2026-07-03T15:07:00+02:00
**Tip-of-tree commit**: `39a8dfb` (fixes 4eb457b / 8a207b1 / c674577 deployed)
**Previous run**: [`2026-07-03-1153`](../2026-07-03-1153/ux.md)
**Mode**: VERIFICATION pass (6th audit) — confirm the 6 auto-fixed UX findings
held on the live site, detect regressions, note anything new.

**Tooling note**: `mcp__claude-in-chrome__*` tools were NOT available this run
(ToolSearch returns no matching deferred tools — same limitation as the two
prior runs). The walk was done with `curl` + locale headers
(`Accept-Language: cs|en|de`) + HTML/byte inspection. Therefore **live JS
console scan, live network ≥400 scan, live dark-mode visual render, and mobile
390px overflow checks could not be performed** and are logged as gaps, not
passes. Dark-mode conclusions are static Tailwind-class analysis only. Locale
is `Accept-Language`-negotiated (`?lang=` does not switch locale — unchanged).

---

## Verification of the 6 auto-fixed UX findings

| Prior ID | Fix commit | Status this run | Live evidence |
|---|---|---|---|
| F-UX-002 (signup dark borders) | 4eb457b | **held / fixed** | All 5 visible inputs (`company_name`, `slug`, `owner_full_name`, `owner_email`, `password`) + `terms_accepted` now carry `dark:border-slate-700`; only hidden `csrf_token`/`plan`/honeypot `website` lack it (correct). |
| F-UX-004 (auth switcher `text-blue-*`) | 4eb457b | **held / fixed** | `text-blue-*` count = 0 on all four auth surfaces (`/platform/login`, `/platform/signup`, `4mex/auth/login`, `4mex/auth/password-reset`). |
| F-UX-005 (homepage FAQ "percent") | 8a207b1 | **held / fixed** | FAQ now renders `99,9 %` (CS), `99.9 %` (EN), `99,9 %` (DE) — glyph, localized decimal. |
| F-UX-006 (EN "tax doklad") | 8a207b1 | **held / fixed** | EN pricing FAQ now reads `tax invoice`; zero `tax doklad` matches. |
| F-UX-007 (EN "SLA 99,9 %") | 8a207b1 | **held / fixed** | EN Enterprise card now `SLA 99.9`; CS + DE correctly keep `SLA 99,9`. |
| F-UX-008 (DE quote pair) | 8a207b1 | **held / fixed** | DE contact microcopy bytes now `e2 80 9e` (`„` U+201E) … `e2 80 9c` (`"` U+201C) — correct German pair `„Demo"`. |

**All 6 fixes held. 0 regressions.**

---

## Findings

Two prior P2 items were **out of scope for the fix batch** (both marked
`manual` / no auto-fix in `2026-07-03-1153/findings.md`) and remain present on
the live site. Re-filed here so reruns keep tracking them; no new defects were
found this pass.

### F-UX-001 — hreflang alternates for all three locales point to the same URL (persisted, manual)
- **Where**: `https://assoluto.eu/` `<head>` (and every apex marketing page); CS/EN/DE.
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Unchanged since the prior run. The page still emits `<link rel="alternate" hreflang="cs|en|de|x-default" href="https://assoluto.eu/">` — all four alternates resolve to the identical URL. Because locale is chosen by `Accept-Language` and there is no per-locale URL, Google treats the hreflang cluster as misconfigured (effectively self-referential) and the DE/EN SEO benefit of the tags is lost. Invisible to users but undercuts discoverability for a fully-translated site.
- **Suggested fix**: Give each locale a stable crawlable URL (`?lang=en` / `/en/` prefix) that pins locale server-side and set per-variant canonicals; point each `hreflang` at its variant. If per-locale URLs are out of scope, drop the misleading `hreflang` links and keep only the self-referential `canonical`.
- **Evidence**: `curl https://assoluto.eu/ | grep 'rel="alternate"'` → 4 links, all `href="https://assoluto.eu/"`.

### F-UX-003 — Tenant auth shell and platform auth shell use divergent input styling (persisted, manual)
- **Where**: `https://4mex.assoluto.eu/auth/login` vs `https://assoluto.eu/platform/login`.
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Unchanged since the prior run. The two auth surfaces still render different input controls — tenant login uses `rounded-lg` / `ring-2 ring-brand-500/20` / `dark:bg-slate-800`; platform login uses `rounded-md` / `ring-1 ring-brand-500` / `dark:bg-slate-900`. Both are legible, so this is polish, not a defect — but a prospect who signs up on the platform shell then lands on the tenant login sees two different-looking form systems for one product.
- **Suggested fix**: Pick one input treatment (the richer tenant `rounded-lg`/`ring-2` set) and extract it into a shared `_input.html` macro / Tailwind component class used by both `auth/` and `platform/` login templates.
- **Evidence**: class diff unchanged from prior run — tenant input `rounded-lg … focus:ring-2 … dark:bg-slate-800`; platform input `rounded-md … focus:ring-1 … dark:bg-slate-900`.

---

## What passed silently (positive evidence)

- **All surfaces 200**: `/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies`, `/robots.txt`, `/sitemap.xml`, `/platform/{login,signup,check-email,password-reset}` → 200; `4mex.assoluto.eu/auth/{login,password-reset}` → 200; `/healthz` + `/readyz` → 200.
- **Honeypot (F-UX-019)**: `/platform/signup` `website` input still `tabindex="-1" autocomplete="off"` inside an `aria-hidden="true"` `position:absolute; left:-10000px` wrapper — not tab-reachable.
- **robots.txt (F-UX-022)**: no `Disallow: /platform/signup`.
- **Auth gating**: `/platform/admin/dashboard` + `/platform/admin/tenants` correctly 303 → `/platform/login?next=…` for HTML clients; a bare `401` is returned only to non-HTML (API) clients — correct content-negotiated split, **not** a regression despite the raw code differing from the prior run's note.
- **og:image**: `https://assoluto.eu/static/og/assoluto-og.png` → 200.
- **Copy hygiene**: no `%(var)s` placeholder leaks, no literal `&nbsp;`, no escaped `\"` across all 24 apex HTML docs (8 pages × 3 locales).
- **Dark mode (static)**: signup form now consistent — no invisible-border risk remaining after F-UX-002 fix.

---

## Walkthrough log

- **Setup**: confirmed Chrome MCP unavailable (ToolSearch → no matching deferred tools); fell back to curl+header inspection. `/healthz` + `/readyz` → 200.
- **Fix verification (priority)**: fetched `/`, `/pricing`, `/contact`, `/platform/signup`, and the four auth surfaces across CS/EN/DE; byte-level and class-level confirmed all 6 auto-fixed findings (F-UX-002/004/005/006/007/008) held. See table above.
- **Apex marketing (CS/EN/DE)**: re-scanned 8 pages × 3 locales for copy leaks — clean. hreflang defect (F-UX-001) confirmed still present. og:image 200.
- **Auth surfaces**: `4mex/auth/{login,password-reset}` inspected (static class only; no live theme toggle). `text-blue-*` leak gone. F-UX-003 input-shell divergence confirmed still present.
- **Platform flows**: `/platform/{login,signup,check-email,password-reset}` all 200; signup honeypot + dark borders re-verified. **Not submitted.**
- **Authenticated tenant `/app/*`**: SKIPPED — no credentials provided; seeded `test-a`/`test-b`/`testfirma` still return `404 Tenant not found` (only `4mex` resolves).
- **Platform admin `/platform/admin/*`**: content SKIPPED — no operator credentials. Confirmed only auth-gating (303 → login for HTML). The new Plan / Billing status / Period-ends columns on `/platform/admin/tenants` and subscription quick-actions could **not** be verified this run.
- **Not performed (Chrome MCP absent)**: live JS console scan, live network ≥400 scan, live dark-mode visual, mobile 390px overflow. Remain unverified.
- **Time**: ~12 min wall clock.

## Would-have-tested-but

- **Seeded test tenants still gone**: `test-a`, `test-b`, `testfirma` all 404 (unchanged from prior run). Needs re-seeding (`python -m scripts.create_tenant`) or the agent definition should point at `4mex`. Without them the authenticated tenant + contact-portal walkthrough stays unreachable.
- **Operator + tenant credentials not supplied**: `/app/*`, `/app/admin/*`, `/app/me/profile`, `/platform/admin/*` content out of reach; the new tenants-table billing columns remain unverified across three consecutive runs.

# Business audit — run 2026-05-09-0931 (verification)

Verification pass after the audit-fix cycle that closed F-BIZ-012,
F-BIZ-013, F-BIZ-014 from the 2026-05-09-0829 run. Tip-of-tree
`d3d911e`. Live-probed CS / EN / DE renders of `/`, `/pricing`,
`/contact`, `/imprint`. Cross-checked `app/templates/www/index.html`,
`app/templates/www/pricing.html`, `app/platform/billing/service.py`,
`migrations/versions/1003_billing.py`, `scripts/backup.sh`, and
`app/tasks/`.

## Summary vs prior run (2026-05-09-0829)

* F-BIZ-001 (backup retention) — still **resolved**.
* F-BIZ-002 (status page URL) — still **persisted** (operator action;
  pricing copy is honest: "status page on request" / "status page
  na vyžádání").
* F-BIZ-003 (demo CTA → /contact, no Cal.com) — still **persisted**.
  Homepage "Book a 15-min demo" CTA still hrefs `/contact`, not a
  real booking link.
* F-BIZ-004 (founder identity on `/`, `/contact`) — still **persisted**.
  Václav Mudra surfaces only on `/imprint` (`<dd>Václav Mudra</dd>`).
  `/contact` shows just `team@assoluto.eu`. Homepage has no founder
  bio block.
* F-BIZ-005 (no trial-nurture cadence) — still **persisted**. `grep
  -rE "send_trial|nurture|welcome_email|day_7|day_25" app/tasks/
  app/email/` returns nothing.
* F-BIZ-006 (testimonial placeholders) — still **persisted**, copy
  unchanged: "Assoluto is in early-access launch. Logos will appear
  here as soon as clients agree to be published."
* F-BIZ-007 (24h SLA copy) — still **resolved**.
* F-BIZ-008 (no refund policy) — still **persisted**. `curl
  https://assoluto.eu/pricing | grep -iE "refund|vrácení|rückerstatt"`
  returns nothing.
* F-BIZ-009 (homepage superlatives) — still **resolved**. No "best /
  fastest / leading / world-class / #1" anywhere on homepage.
* F-BIZ-010 (annual billing on request) — still **resolved**.
* F-BIZ-012 — **resolved this cycle** (verification below).
* F-BIZ-013 — **resolved this cycle** (verification below).
* F-BIZ-014 — **resolved this cycle** (verification below).

## Fixes from 2026-05-09-0829 — verified

### F-BIZ-012 — Homepage step 1 IČO promise dropped — VERIFIED resolved

* **CS live**: `E-mail, heslo, název firmy. Za 30 vteřin máte vlastní
  adresu — třeba vasefirma.assoluto.eu.`
* **EN live**: `Email, password, company name. You'll get your own
  address — e.g. yourfirm.assoluto.eu — in 30 seconds.`
* **DE live**: `E-Mail, Passwort, Firmenname. In 30 Sekunden haben
  Sie Ihre eigene Adresse — z. B. ihrefirma.assoluto.eu.`
* Source `app/templates/www/index.html:204` matches. No `IČO` /
  `Company ID` / `Firmen-ID` strings in the step-1 paragraph in any
  locale. Step heading still reads "Sign up, choose a subdomain" /
  "Registrace, výběr subdomény" / "Anmelden, Subdomain wählen" — no
  promise of IČO upfront. The promise/code gap is closed: signup
  form (`signup.html`) collects company_name, email, password,
  full_name only, matching what the homepage now promises.

### F-BIZ-013 — Pricing FAQ discloses IČO/DIČ checkout gate — VERIFIED resolved

New FAQ entry present in all three locales, copy is **specific** as
required (mentions IČO, mentions 8 digits, mentions DIČ as optional /
VAT-payer-only):

* **CS** (`/pricing`):
  * Q: `Co potřebuju zadat při prvním placeném checkoutu?`
  * A: `Fakturační název, IČO (8 číslic) a fakturační adresu. DIČ
    pokud jste plátce DPH. Bez nich nemůžeme vystavit platný daňový
    doklad. Vyplníte jednou — uložíme.`
* **EN** (`/pricing`):
  * Q: `What do I need to enter at first paid checkout?`
  * A: `Billing name, IČO (8 digits) and billing address. DIČ if you
    are a VAT payer. We need these to issue a valid Czech tax
    doklad. You enter them once — we keep them on file.`
* **DE** (`/pricing`):
  * A: `Rechnungsname, IČO (8 Ziffern) und Rechnungsadresse. DIČ,
    falls Sie USt-pflichtig sind. Ohne diese Angaben können wir
    keinen gültigen tschechischen Steuerbeleg ausstellen. Sie geben
    es einmal ein — wir speichern es.`

`<details>` count on `/pricing` = 7 in both CS and EN (was 6) — new
entry is in. Copy aligns with `app/platform/routers/billing.py`'s
`_billing_details_present` gate.

### F-BIZ-014 — Enterprise SLA inversion fixed — VERIFIED resolved

All three locales now show Enterprise's response time strictly
tighter than Pro's, and "SLA 99,9 %" rendered correctly:

* **EN live** (Enterprise card): `Priority support (4 h business
  hours, written SLA)` and `SLA 99,9 %`.
* **CS live**: `Prioritní podpora (4 h v pracovní době, písemné SLA)`
  and `SLA 99,9 %`.
* **DE live**: `Prioritäts-Support (4 h zur Geschäftszeit,
  schriftliches SLA)` and `SLA 99,9 %`.
* Source: `pricing.html:96` uses the new msgid; `:97` uses the split-
  form `{{ _("SLA") }} 99,9 %` pattern (literal `%` outside the
  gettext call, dodging the documented `%%` Jinja trap).

F-UX-020 (`SLA 99.9 percent` → `SLA 99,9 %`) is also resolved by the
same change — confirmed in all three locales' live HTML.

## Marketing ↔ code coherence — re-verified

| Check | Code | Marketing | Status |
|---|---|---|---|
| Trial length | `TRIAL_DAYS=30` (`service.py:22`) | "30 days" / "30 dní" / "30 Tage" everywhere | match |
| Cancel grace | `CANCEL_GRACE_DAYS=3` (`service.py:149`) | "3 days to export" / "3 dny na export" / "3 Tage zum Datenexport" | match |
| Starter limits | `1003_billing.py:145` — 3 / 20 / NULL / 2048 | "3 staff users · 20 client contacts · Unlimited orders · 2 GB" | match |
| Pro limits | `1003_billing.py:146` — 15 / 100 / NULL / 20480 | "15 staff users · 100 client contacts · Unlimited orders · 20 GB" | match |
| Pricing numbers | n/a (template) | live `490 Kč / měsíc` and `1 490 Kč / měsíc` | match |
| Backup retention | `KEEP_DAYS=14` (`backup.sh:29`) | "14-day retention" promise | match |
| AGPL self-host | `LICENSE` AGPL-3.0 + `FEATURE_PLATFORM=false` default | "AGPL-3.0, your server, your data" | match |

No new drift introduced by the IČO/SLA edit. Value-prop story on the
homepage is intact: dropping `IČO` from step 1 actually tightened the
"30 seconds to a portal" claim (one less thing to look up).

## Live probe summary

```
curl -s https://assoluto.eu/pricing | grep -E "490|1 490"
→ 490 Kč / měsíc, 1 490 Kč / měsíc        ← matches plan rows
curl -s https://assoluto.eu/pricing | grep -iE "Uptime|status"
→ "UptimeRobot 24/7, status page na vyžádání"  ← honest copy
curl -s https://assoluto.eu/pricing | grep -cE "<details "
→ 7 (CS), 7 (EN)                          ← new IČO FAQ included
curl -s https://assoluto.eu/contact | grep -iE "Václav|Mudra"
→ (no match)                              ← F-BIZ-004 persists
curl -s https://assoluto.eu/imprint | grep -iE "Václav|Mudra"
→ "<dd>Václav Mudra</dd>"                 ← imprint correct
```

## SEO consistency note (P2, not new finding)

The `FAQPage` JSON-LD blob lives on **`/`**, not `/pricing` (7
questions, none about IČO/checkout fields). The new pricing FAQ
entry is therefore not picked up by Google's rich-results parser.
Not a regression (this state pre-dates the fix), but worth folding
into the next pricing-SEO pass — either move FAQPage to `/pricing`
or add the new question to the homepage FAQ + JSON-LD. Tracked as
operator backlog, not filed as a new F-BIZ because it is a
pre-existing condition.

## No new findings filed

All three targeted fixes verified across CS / EN / DE. No regression
in TRIAL_DAYS, CANCEL_GRACE_DAYS, plan limits, backup retention,
AGPL pitch, or pricing numbers. Six previously-persisting operator
items (F-BIZ-002, -003, -004, -005, -006, -008) carry forward
unchanged.

# Business audit — run 2026-05-09-0829

Third automated audit. Re-ran the 15 mandatory marketing-↔-code checks
against the code state at d0a5e35 and live https://assoluto.eu. Spent
the bulk of the budget on the four diff-meaningful changes called out
in the brief: verify-email gate, IČO/DIČ JIT gate, Enterprise plan
ordering, and staff item-edit on SUBMITTED/QUOTED. Live-probed CS + EN
renders of /pricing, /, /contact, /imprint.

Summary vs prior run (2026-05-01-1455):

* F-BIZ-001 (backup retention) — still resolved, retention default = 14
  in `scripts/backup.sh:29`.
* F-BIZ-002 (status page URL) — still persisted (operator action).
* F-BIZ-003 (demo CTA → /contact, no Cal.com) — still persisted.
* F-BIZ-004 (founder identity) — still persisted on `/` and `/contact`;
  imprint still carries Václav Mudra correctly.
* F-BIZ-005 (no trial-nurture cadence) — still persisted; `grep -nE
  "send_trial|send_welcome|send_nurture" app/tasks/*.py` returns nothing.
* F-BIZ-006 (testimonial placeholders) — still persisted.
* F-BIZ-007 (24h SLA copy) — still resolved, msgstrs unchanged.
* F-BIZ-008 (no refund policy) — still persisted; no marketing copy on
  refunds anywhere; only the Stripe `charge.refunded` plumbing exists.
* F-BIZ-009 (superlative on homepage) — still resolved.
* F-BIZ-010 (annual billing on request) — still resolved.
* F-BIZ-011 (175 fuzzy EN entries) — out of business scope; tracked for
  the UX/i18n auditor.

Three new findings filed against this week's diff. Two pre-existing
gaps re-flagged because the new IČO gate now amplifies them.

---

### F-BIZ-012 — Homepage step 1 promises "IČO/Company ID" at signup but form does not collect it

- **Where**:
  - Marketing: `app/templates/www/index.html:204` and CS msgstr at
    `app/locale/cs/LC_MESSAGES/messages.po:3796-3800`
  - Code: `app/templates/platform/signup.html:30-110` (no IČO field)
  - Live (CS): `E-mail, heslo, IČO. Za 30 vteřin máte vlastní adresu — třeba vasefirma.assoluto.eu.`
  - Live (EN): `Email, password, Company ID. You'll get your own address — e.g. yourfirm.assoluto.eu — in 30 seconds.`
- **Severity**: P0
- **Auto-fixable**: yes
- **Description**: The "How it works" step-1 panel on the homepage
  promises that signup takes "email, password, Company ID (IČO)" and
  produces a portal in "30 seconds." Reality after commit 17a662d:
  signup form (`signup.html`) collects only `company_name`,
  `owner_email`, `owner_full_name`, `password`, `terms_accepted` (and
  optional `slug`). IČO + DIČ + fakturační adresa are now collected
  just-in-time at the first paid Stripe checkout via
  `/platform/billing/details` (gated by
  `_billing_details_present` in `app/platform/routers/billing.py:54-58`).
  This is a P0 promise gap — the operator's own homepage instructs the
  prospect to have IČO ready, then the form silently doesn't ask for
  it, and the IČO requirement only resurfaces after the user has
  burned 30 days of trial and clicks Upgrade. Either (a) add an
  optional IČO field to signup and seed `tenant.settings`, or (b) drop
  "IČO/Company ID" from the homepage step 1 and surface the
  IČO-at-checkout requirement in the pricing FAQ instead.
- **Suggested fix**: Lowest-friction option = option (b). Edit the
  msgid at `index.html:204` to `"Email, password, company name. You'll
  get your own address — e.g. yourfirm.assoluto.eu — in 30 seconds."`
  + drop "IČO" / "Company ID" from CS / EN / DE catalogs. Add a new
  pricing FAQ entry: *"What do I need to enter at first paid checkout?
  Company name, IČO (8 digits), billing address. DIČ if you are a VAT
  payer. We need these to issue a valid Czech tax doklad."* Higher-
  friction option (a) is a real signup-form change; preferred only if
  the operator wants to nudge prospects toward "we know you are
  serious" by asking for IČO upfront.
- **Evidence**: live curl above + signup.html field listing.

---

### F-BIZ-013 — Pricing FAQ does not disclose the IČO/DIČ checkout gate

- **Where**:
  - `app/templates/www/pricing.html:164-170` (Pricing FAQ macro
    invocations) — no entry covers what's needed at checkout.
  - `app/templates/www/index.html:425` (FAQ) — touches "Czech invoice"
    and "tax documents" but treats them as a *resolution channel*
    (email team@assoluto.eu) rather than a checkout precondition.
  - Code: `app/platform/routers/billing.py:230-241` (the redirect to
    `/platform/billing/details?next=...`).
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: Trial users now hit a hard stop at first checkout
  if `tenant.settings` is missing `billing_ico`, `billing_name`, or
  `billing_address`. Marketing copy on /pricing and the homepage FAQ
  never warns about this. The `/platform/billing` dashboard does
  surface an amber "Add your billing details" banner
  (`dashboard.html:14-25`), but a prospect comparison-shopping on
  /pricing has no way to know what they'll need to enter. Friction
  surprise = trust gap — and it specifically hits the moment of
  highest commercial intent (the user has clicked Upgrade and is
  about to pay).
- **Suggested fix**: Add one Pricing FAQ entry. Suggested copy (CS):
  *"Co potřebuju zadat při prvním placeném checkoutu? Fakturační
  název, IČO (8 číslic) a fakturační adresu. DIČ pokud jste plátce
  DPH. Bez nich nemůžeme vystavit platný daňový doklad. Vyplníte
  to jednou — uložíme."* EN/DE mirrors via the catalog. Bonus: also
  put a one-line note on the pricing card CTA: e.g. tooltip "Karta
  + IČO se zadávají až na konci 30 dní" so the prospect doesn't
  feel surprise at conversion time.
- **Evidence**:
  - `curl -s -H "Accept-Language: cs" https://assoluto.eu/pricing | grep -E -i "IČO|DIČ|tax|faktur|invoice"` returns only the annual-billing callout and "Roční fakturace" FAQ — nothing about *required* checkout fields.
  - `app/platform/routers/billing.py:234`:
    `if settings.stripe_enabled and not _billing_details_present(tenant):`
    redirect to `/platform/billing/details?next=...`.

---

### F-BIZ-014 — SLA tier inversion: Pro promises 12h, Enterprise promises 24h

- **Where**: `app/templates/www/pricing.html:78` (Pro) and `:96`
  (Enterprise)
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: Pricing card SLA copy is inverted. Starter promises
  48 h, Pro promises 12 h, Enterprise promises 24 h. Live (CS):
  `Prioritní e-mailová podpora (12 h)` on Pro, `Prioritní podpora
  (24 h)` on Enterprise. Live (EN) identical numbers. A Pro customer
  paying 1 490 Kč/mo gets a faster guaranteed response than an
  Enterprise customer paying "price on request" — which is either
  a copy-paste bug or a number Enterprise prospects will negotiate
  out of the contract immediately. Either way it makes the
  Enterprise tier visibly weaker and undermines the "let's talk to
  us" CTA. This is a pre-launch find — fix before the first paid
  Enterprise prospect reads it.
- **Suggested fix**: Make Enterprise's response time strictly tighter
  than Pro's. Two clean options:
  * Enterprise = "Priority support (4 h business hours, written SLA)"
    — matches the "SLA 99.9 percent" claim already on the same card.
  * Or keep Enterprise = "SLA on request" without a number, and let
    the contract specify per-customer.
  Pick one and update the msgid; if the new copy is "SLA on request"
  also drop the redundant "Priority support" line. Update CS / DE
  msgstrs in the same commit.
- **Evidence**:
  - Source `pricing.html:78`:
    `{{ _("Priority email support (12 h)") }}` (Pro)
  - Source `pricing.html:96`:
    `{{ _("Priority support (24 h)") }}` (Enterprise)
  - Live (EN): `Priority email support (12 h)` on Pro and
    `Priority support (24 h)` on Enterprise.

---

## What the d0a5e35 diff got *right* (verified, no finding)

* **Trial length** — `TRIAL_DAYS = 30` in
  `app/platform/billing/service.py:22` matches every "30 days" /
  "30-day trial" copy across pricing, index, JSON-LD, signup form
  ("30-day free trial. No credit card required."). No drift.
* **Cancel grace** — `CANCEL_GRACE_DAYS = 3` in both `service.py:149`
  and `app/tasks/periodic.py:46` matches the "3 days to export your
  data" copy on pricing FAQ, index FAQ, terms.html, and the cancel
  modal.
* **Plan limits** — Migration `1003_billing.py` seeds Starter (3 / 20 /
  2 GB) and Pro (15 / 100 / 20 GB); pricing card copy still matches
  exactly.
* **Pricing numbers (live)** — `490 Kč / měsíc` on Starter and
  `1 490 Kč / měsíc` on Pro confirmed via `curl
  https://assoluto.eu/pricing`.
* **Annual billing on request** — copy unchanged, callout block + FAQ
  + JSON-LD rich snippet all consistent.
* **Self-host pitch** — `LICENSE` is GNU AGPL v3, self-hosted page
  brands as AGPL-3.0, `FEATURE_PLATFORM=false` is the default in
  `.env.example`.
* **Backup retention** — `KEEP_DAYS="${PORTAL_KEEP_DAYS:-14}"` in
  `scripts/backup.sh:29`, header comment still references
  marketing/Terms/GDPR Art. 5(1)(e) commitment.
* **Imprint** — Live `/imprint` correctly carries Václav Mudra /
  Lidická 2020/2 / Děčín / IČO / COI ADR + EU ODR links.
* **Trademark notice** — README still references ™ / common-law,
  ÚPV filing in progress; pricing page does not use unauthorized
  registered-mark symbol.
* **Refund + past_due plumbing** — `charge.refunded` Stripe webhook
  handler still wired (separate from the marketing-copy gap of
  F-BIZ-008).
* **Verify-email gate hardening (commit bfd1690)** — code gate is
  correctly in place; `require_verified_identity` now covers
  `select_tenant`, `switch_to_tenant`, `complete_switch` (per
  `app/platform/routers/platform_auth.py:296,359,471`). **No
  marketing claim contradicts this** — homepage / pricing / signup
  page never promise "instant access" or "no email verification."
  The closest claim ("Up and running in 30 minutes" on homepage hero
  subtext) is loose enough to comfortably include the email-click
  step. So the verify-gate change is business-clean — the IČO change
  is what surfaces the two new findings.
* **Enterprise plan rendering (commit d2a1f22)** — `PLAN_DISPLAY_ORDER
  = (starter, pro, enterprise)` in `app/platform/billing/service.py`
  and `dashboard.html:130-131` correctly shows "Price on request" for
  enterprise. /pricing renders Community-Starter-Pro-Enterprise
  left-to-right (live confirmed); dashboard renders the same order.
  Marketing-↔-code now consistent. **The only remaining gap is the
  inverted SLA hours (F-BIZ-014).**
* **Staff item-edit extension (commit 17c1528)** —
  `STAFF_ITEM_EDIT_STATES = {DRAFT, SUBMITTED, QUOTED}` matches
  marketing perfectly. `features.html:31` says "You quote the price
  and deadline" and the state diagram (`:47-54`) shows the SUBMITTED
  → QUOTED transition. `index.html:222` says "Client uploads a
  drawing, you quote, they confirm." No marketing claim implies a
  one-click quote flow that the new code contradicts. No nudge /
  onboarding doc exists yet (separately tracked under the trial-
  nurture gap, F-BIZ-005), but the underlying contract between
  marketing and code is consistent.


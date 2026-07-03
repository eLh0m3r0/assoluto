# Business / GTM audit — run 2026-07-03-1507 (VERIFICATION)

Auditor: business. 6th automated run, verification pass over
`docs/audit-runs/2026-07-03-1153/`. All six prior business findings were
tagged `manual` (operator/content decisions); this pass re-probes live
prod + code to confirm which still hold. Two marketing-copy fixes from
the prior run landed (commit `8a207b1`) — verified they did NOT change
plan/price/SLA meaning.

## Verification of the two landed fixes (commit 8a207b1)

- **EN "tax invoice" wording** — `pricing.html:165` now reads "valid
  Czech tax invoice" (was "tax doklad"). English-wording fix only; the
  billing obligation (IČO + address at first paid checkout) is
  unchanged. Held, no meaning drift.
- **SLA decimal separator** — `pricing.html:97` now renders
  `{% if locale == 'en' %}99.9{% else %}99,9{% endif %} %`; homepage FAQ
  msgid `99.9 percent` → `99.9 %%` (documented %%-escape). Still 99.9 %
  target in every locale. Held, no meaning drift.

## Passed checks (coherence confirmed this run)

- **Trial length** — pricing/FAQ "30 dní" (live) == `TRIAL_DAYS = 30`
  (`service.py:22`). Match.
- **Plan limits** — Starter 20 contacts / 2 GB and Pro 100 / 20 GB on the
  cards (`pricing.html:51,53,75,77`) == seed rows
  (`1003_billing.py:145-146`: `20/2048`, `100/20480`; prices `49000` /
  `149000` cents == 490 / 1 490 Kč). Orders "unlimited" == `NULL` cap.
  Match. (Seed read from migration; SSH psql not available in sandbox.)
- **Cancel flow** — FAQ "3 days to export" (`pricing.html:168`) ==
  `CANCEL_GRACE_DAYS = 3` (`service.py:149`), enforced by
  `enforce_canceled_subscriptions`. Match.
- **Annual billing** — "Annual billing on request" (`pricing.html:113`),
  unchanged from the 2026-04-25 fix. OK.
- **Self-host** — `LICENSE` is AGPL-3.0; `feature_platform` defaults
  `False` (`app/config.py:71`). OK.
- **Backups** — "kept 14 days" (`pricing.html:133`) == `KEEP_DAYS`
  default 14 (`scripts/backup.sh:29,68-69`). OK.
- **Status page** — `status_page_url` unset in prod; live pricing renders
  "status page na vyžádání" (on request). No false claim. OK.
- **Live price probe** — `curl https://assoluto.eu/pricing` returns `490`,
  `1 490`, `Kč`, `30 dní`, `48 h`, `12 h`, `14`. Matches template. OK.
- **Superlatives / trademark ™** — unchanged from prior run, still clean.

---

## Findings (all six persist from run 2026-07-03-1153 — status: OPEN)

### F-BIZ-001 — Fabricated testimonials presented as real customer quotes [PERSISTS]
- **Where**: `app/templates/www/index.html:358-401` (Testimonials section)
- **Severity**: P0
- **Auto-fixable**: no
- **Description**: STILL LIVE on https://assoluto.eu — the live homepage
  renders the "Majitel … Morava" attribution (confirmed via curl this
  run). The section header states the product is in early access and
  "Logos will appear here as soon as clients agree to be published" (i.e.
  zero published paying customers), yet three cards below render specific,
  metric-laden quotes with plausible firm attributions under the heading
  "What our first customers say". A prospect reads these as genuine
  testimonials. Invented endorsements presented as real consumer reviews
  are a banned unfair commercial practice under the EU Omnibus Directive
  (2005/29/EC, transposed into CZ Act 634/1992) — enforceable with fines,
  and a direct contradiction of the operator's own honesty stance. This
  was flagged P0 in the prior run and remains unaddressed.
- **Suggested fix**: Either (a) remove the three quote cards and keep only
  the honest "early access, logos coming" placeholder, or (b) relabel them
  unambiguously as illustrative scenarios ("Example outcome shops report /
  illustrative, not an actual customer") and drop the "What our first
  customers say" heading until a real, consented customer exists.
- **Evidence**: `index.html:364` `_("What our first customers say")` +
  `:367` "Logos will appear here as soon as clients agree to be
  published." + `:375` `"Five calls a day asking about orders turned into
  zero…"` attributed `:378` `_("Owner, metalwork shop — Morava, 12
  employees")`. Live `curl https://assoluto.eu/` returns `Majitel` /
  `Morava`. No `platform_subscriptions` customer rows exist; the
  disclaimer itself states none are published.

### F-BIZ-002 — Support-time promises with no autoresponder or SLA tracker (solo founder) [PERSISTS]
- **Where**: `app/templates/www/pricing.html:54,78,96`;
  `app/templates/www/contact.html:17,32,58,77`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: Unchanged. Starter card "Email support (48 h)", Pro
  "Priority email support (12 h)", Enterprise "Priority support (4 h
  business hours, written SLA)"; the contact page instead promises "reply
  within 1 working day". Internal drift (48 h/12 h vs. "1 working day")
  plus an unhonourable 12 h/4 h window for a single founder with no
  autoresponder and no ticket/SLA tracker — a trust/refund liability the
  first time it's missed.
- **Suggested fix**: (1) Reconcile the numbers — standardise on "1 working
  day" (Starter) / "same working day" (Pro), phrased as targets not
  guarantees, everywhere. (2) Add a mailbox autoresponder on
  team@assoluto.eu confirming receipt + expected window. (3) Keep the hard
  "4 h business hours, written SLA" only inside actual Enterprise
  contracts, not as a public card bullet, until staffable.
- **Evidence**: `pricing.html:54` `_("Email support (48 h)")`, `:78`
  `_("Priority email support (12 h)")` vs `contact.html:32`
  `_("Reply within 1 working day")`. No `send_*`/autoresponder task in
  `app/tasks/` (grep empty); no SLA-tracking model in the codebase.

### F-BIZ-003 — No trial-nurture email cadence (only expiry job exists) [PERSISTS]
- **Where**: `app/tasks/periodic.py` (`expire_demo_trials`),
  `app/tasks/email_tasks.py`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: Unchanged. The only trial automation is
  `expire_demo_trials`, which flips lapsed trials to canceled. There is no
  welcoming/activation cadence (day-1 onboarding, day-7 check-in, day-25
  "trial ends in 5 days, add a card"). `grep -rlE "send_.*trial|nurture|
  welcome" app/tasks/` returns nothing. For a 30-day no-card trial this is
  the single biggest free→paid conversion lever and it's entirely absent.
- **Suggested fix**: Operator action — add a small scheduled cadence
  (reuse the SMTP sender + Jinja templates in `app/email/`): day-1 nudge,
  day-7 activation check, day-25 add-card prompt. Gate each on the tenant
  still being on a trial subscription so canceled/paid tenants skip.
- **Evidence**: `expire_demo_trials` present; no matching send task.

### F-BIZ-004 — Founder name lives only on the legal imprint, not where prospects look [PERSISTS]
- **Where**: `app/templates/www/imprint.html`;
  `app/templates/www/contact.html`; `app/templates/www/index.html`
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Unchanged. "Václav Mudra" appears only on /imprint (a
  §8 legal disclosure). Contact + homepage speak entirely in first-person
  plural ("Czech team", "team@assoluto.eu") with no human name. For a
  solo-founder B2B SaaS selling to conservative Czech manufacturers, a
  visible "who's behind this" on the homepage/contact is a proven trust
  lever. Not a promise violation → P2, but a real conversion gap.
- **Suggested fix**: Add a short "Who's behind Assoluto" line to the
  contact page (and/or homepage final-CTA subtext): founder name, one
  sentence, direct email.
- **Evidence**: `contact.html` and `index.html` contain no personal name —
  only `team@assoluto.eu` (`contact.html:32` renders no "Václav").

### F-BIZ-005 — "Book a 15-min demo" CTA goes to a contact form, not a booking flow [PERSISTS]
- **Where**: `app/templates/www/index.html:42-45` → `/contact`;
  `app/templates/www/contact.html:43-45`
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Unchanged. Homepage secondary CTA "Book a 15-min demo"
  links to `/contact`, where the demo path is "Write 'demo' in the
  message" and wait for a reply. Not a 404 and not a raw mailto (so not P1
  per rubric), but "Book" implies instant self-serve scheduling the async
  form doesn't meet — friction at the highest-intent click.
- **Suggested fix**: Soften copy to "Ask for a 15-min demo" / "Request a
  demo" to match reality, or wire a real Cal.com/Calendly link once the
  founder can commit slots. Keep label and destination aligned.
- **Evidence**: `index.html:45` `_("Book a 15-min demo")` with
  `href="/contact"` (`:42`); `contact.html:45`
  `_("Write \"demo\" in the message")`.

### F-BIZ-006 — No stated refund / withdrawal policy despite refund-capable code [PERSISTS]
- **Where**: `app/templates/www/pricing.html` FAQ;
  `app/templates/www/terms.html`;
  `app/platform/billing/webhooks.py` (`handle_charge_refunded`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: Unchanged. Billing code fully supports refunds
  (`handle_charge_refunded` flips invoices to `refunded` /
  `partially_refunded`), but no marketing/legal surface states a refund or
  withdrawal position — grep for refund/vrácení/odstoupení across
  terms.html + pricing.html returns nothing. Checkout requires IČO (B2B),
  so the consumer 14-day withdrawal right likely doesn't apply → low
  severity, but the silence means a refund request has no reference policy
  and the "cancel = access to period end, no pro-rata refund" stance is
  only implied.
- **Suggested fix**: Add one FAQ line to pricing/terms: monthly plans
  non-refundable mid-period (access kept to period end); annual/manual
  invoices refunded pro-rata at operator discretion. State it once so the
  refund code has a policy behind it.
- **Evidence**: `pricing.html:168` cancel FAQ covers export + GDPR erasure
  but not refunds; refund webhook handling exists with no customer-facing
  policy. grep for refund terms across terms/pricing returns empty.

---

## Summary

Verification pass: 6 findings carried forward, 0 resolved, 0 regressed,
0 new. The two prior copy fixes (tax-invoice wording, SLA decimal) held
without changing plan/price/SLA meaning. All coherence checks pass; the
open items are content/operator decisions (`manual`), led by the P0
fabricated-testimonials liability which is still live on prod.

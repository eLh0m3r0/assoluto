# Business / GTM audit — run 2026-07-03-1153

Auditor: business. Scope: marketing ↔ code coherence, operator-action
surfaces, honesty. Prior run: `docs/audit-runs/2026-05-09-0931/`.

## Passed checks (no finding)

- **Trial length** — pricing/FAQ "30 days" == `TRIAL_DAYS = 30`
  (`app/platform/billing/service.py:22`). Match.
- **Plan limits** — Starter (3 users / 20 contacts / unlimited orders /
  2 GB) and Pro (15 / 100 / unlimited / 20 GB) on the pricing cards match
  the `platform_plans` seed in `migrations/versions/1003_billing.py:145-146`
  (`49000`/`149000` cents, `max_storage_mb` 2048/20480) with the Starter
  order cap dropped by `1006_drop_starter_orders_cap.py`. Match. (Seed read
  from migration, not prod psql — SSH not available from this sandbox.)
- **Cancel flow** — FAQ "3 days to export" == `CANCEL_GRACE_DAYS = 3`
  (`service.py:149`) enforced by `enforce_canceled_subscriptions`
  (`app/tasks/periodic.py:266-284`). Match.
- **Annual billing** — pricing reads "Annual billing on request"
  (`pricing.html:113`). Unchanged from the 2026-04-25 fix. OK.
- **Self-host** — `LICENSE` is AGPL-3.0; `FEATURE_PLATFORM` defaults
  `False` (`app/config.py:71`); self_hosted.html AGPL copy correct. OK.
- **Backups** — "Daily backups, 14-day retention" == `PORTAL_KEEP_DAYS`
  default 14 in `scripts/backup.sh:29,68-69`. OK.
- **Status page** — `status_page_url` unset in prod; live pricing correctly
  renders "status page na vyžádání" (on request). Consistent, no false
  claim. OK.
- **Live price probe** — `curl https://assoluto.eu/pricing` returns `490`,
  `1 490`, `Kč`. Matches head template. OK.
- **Founder identity present** — live `/imprint` renders "Václav Mudra"
  as Provozovatel; footer links to it. Not absent (see F-BIZ-004 for the
  softer conversion note).
- **Superlatives** — no unsupported "best/fastest/leading" claims found in
  index/pricing/features/self_hosted.
- **Trademark symbol** — README asserts common-law ™ (filing in progress);
  correct current state, will flip to ® on ÚPV grant. No action now.

---

## Findings

### F-BIZ-001 — Fabricated testimonials presented as real customer quotes
- **Where**: `app/templates/www/index.html:358-401` (Testimonials section)
- **Severity**: P0
- **Auto-fixable**: no
- **Description**: The section header says the product is in early access
  and "Logos will appear here as soon as clients agree to be published" —
  i.e. there are no published paying customers yet. Directly below, three
  cards render specific, metric-laden quotes with plausible attributions
  ("Owner, metalwork shop — Morava, 12 employees", "Five calls a day
  turned into zero") under the heading "What our first customers say". A
  prospect reads these as genuine customer testimonials. Invented
  endorsements presented as real consumer reviews are a banned unfair
  commercial practice under the EU Omnibus Directive (2005/29/EC as
  amended, transposed into CZ Act 634/1992) — enforceable with fines, and
  a direct contradiction of the operator's own honesty stance.
- **Suggested fix**: Either (a) remove the three quote cards entirely and
  keep only the honest "early access, logos coming" placeholder, or
  (b) relabel them unambiguously as illustrative scenarios ("Example: the
  kind of outcome shops report" / "Illustrative, not an actual customer")
  and drop the "What our first customers say" heading until a real,
  consented customer exists. Do not attribute invented quotes to
  unnamed-but-specific firms.
- **Evidence**: `index.html:364` `_("What our first customers say")` +
  `:367` "Logos will appear here as soon as clients agree to be
  published." + `:375` `"Five calls a day asking about orders turned into
  zero..."` attributed `:378` `_("Owner, metalwork shop — Morava, 12
  employees")`. No `platform_subscriptions` customer rows referenced; the
  disclaimer itself states none are published.

### F-BIZ-002 — Support-time promises with no autoresponder or SLA tracker (solo founder)
- **Where**: `app/templates/www/pricing.html:54,78,96`;
  `app/templates/www/contact.html:32,58,77`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: The Starter card promises "Email support (48 h)", the
  Pro card "Priority email support (12 h)", and Enterprise "Priority
  support (4 h business hours, written SLA)". The contact page instead
  promises "reply within 1 working day". For a single founder there is no
  autoresponder, no ticket/SLA tracker, and no way to honour a 12 h
  priority window (incl. overnight/weekend) reliably. This is both an
  internal drift (48 h/12 h on pricing vs. "1 working day" on contact) and
  an unrealistic commitment that becomes a trust/refund liability the
  first time it's missed.
- **Suggested fix**: (1) Reconcile the numbers — standardise on "1 working
  day" (Starter) / "same working day" (Pro) and phrase them as targets,
  not guarantees, everywhere. (2) Add a simple mailbox autoresponder on
  team@assoluto.eu confirming receipt + expected reply window — the
  cheapest way to keep the promise credible. (3) Keep the hard "4 h
  business hours, written SLA" only inside actual Enterprise contracts,
  not as a public card bullet, until it's staffable.
- **Evidence**: `pricing.html:54` `_("Email support (48 h)")`, `:78`
  `_("Priority email support (12 h)")` vs `contact.html:32`
  `_("Reply within 1 working day")`. No `send_*`/autoresponder task in
  `app/tasks/` and no SLA-tracking model in the codebase.

### F-BIZ-003 — No trial-nurture email cadence (only expiry job exists)
- **Where**: `app/tasks/periodic.py` (`expire_demo_trials:208`),
  `app/tasks/email_tasks.py`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: The only trial-related automation is `expire_demo_trials`,
  which flips lapsed trials to canceled. There is no welcoming/activation
  cadence (e.g. day-1 "here's how to invite your first client", day-7
  check-in, day-25 "trial ends in 5 days, add a card"). Grep of
  `app/tasks/` finds no `send_*trial*` / `*nurture*` / `*welcome-trial*`
  job. For a 30-day no-card trial this is the single biggest lever on
  free→paid conversion and it's entirely absent.
- **Suggested fix**: Operator action — add a small scheduled cadence
  (reuse the SMTP sender + Jinja email templates in `app/email/`): day 1
  onboarding nudge, day 7 activation check, day 25 "trial ending +
  add-card" prompt. Gate each on the tenant still being on a trial
  subscription so canceled/paid tenants are skipped.
- **Evidence**: `periodic.py:208` `async def expire_demo_trials`; no
  matching send task exists (`grep -rE "send_.*trial|nurture" app/tasks`
  returns nothing but the expiry job and doc strings).

### F-BIZ-004 — Founder name lives only on the legal imprint, not where prospects look
- **Where**: `app/templates/www/imprint.html:15`;
  `app/templates/www/contact.html`; `app/templates/www/index.html`
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: "Václav Mudra" appears only on /imprint (a §8 legal
  disclosure). The homepage and contact page speak entirely in
  first-person-plural "team@assoluto.eu" / "Czech team", with no human
  face or name. For a solo-founder B2B SaaS selling to conservative Czech
  manufacturers, a visible "who's behind this" (founder name + one line of
  story) on the homepage or contact page is a proven conversion/trust
  lever. Not a promise violation, so P2, but a real gap.
- **Suggested fix**: Add a short "Who's behind Assoluto" line to the
  contact page (and/or a final-CTA subtext on the homepage): founder name,
  one sentence, direct email. Reuse the imprint value if wired to config,
  or hard-copy it in the editorial template.
- **Evidence**: `imprint.html:15` `<dd>{{ operator_name }}</dd>` (renders
  "Václav Mudra" live). `contact.html` and `index.html` contain no
  personal name — only `team@assoluto.eu`.

### F-BIZ-005 — "Book a 15-min demo" CTA goes to a contact form, not a booking flow
- **Where**: `app/templates/www/index.html:42-46` → `/contact`;
  `app/templates/www/contact.html:43-45`
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The homepage secondary CTA reads "Book a 15-min demo"
  but links to `/contact`, where the demo path is "Write 'demo' in the
  message" and wait for a reply. It does not 404 and is not a raw mailto
  (so not P1 per rubric), but "Book" sets an expectation of instant
  self-serve scheduling that the async contact form doesn't meet — a
  friction point exactly at the highest-intent click.
- **Suggested fix**: Either soften the copy to "Ask for a 15-min demo"
  / "Request a demo" to match the form reality, or (better for conversion)
  wire a real booking link (Cal.com/Calendly) once the founder can commit
  slots. Keep the button label and the destination behaviour aligned.
- **Evidence**: `index.html:45` `_("Book a 15-min demo")` with
  `href="/contact"` (`:42`); `contact.html:45`
  `_("Write \"demo\" in the message")`.

### F-BIZ-006 — No stated refund / withdrawal policy despite refund-capable code
- **Where**: `app/templates/www/pricing.html` FAQ; `app/templates/www/terms.html`;
  `app/platform/billing/webhooks.py:373` (`handle_charge_refunded`)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The billing code fully supports refunds
  (`handle_charge_refunded` flips invoices to `refunded` /
  `partially_refunded`), but no marketing/legal surface states a refund or
  withdrawal position. Terms only address that downtime gives no
  compensation claim (`terms.html:114`). Because checkout requires IČO
  (B2B), the consumer 14-day withdrawal right likely doesn't apply, so
  this is low-severity — but the silence means a refund request has no
  reference policy, and the "cancel = keep access to period end, no
  pro-rata refund" stance is only implied, never stated.
- **Suggested fix**: Add one FAQ line to pricing/terms: monthly plans are
  non-refundable mid-period (you keep access to period end); annual/manual
  invoices refunded pro-rata at the operator's discretion. State it once so
  the refund code has a policy behind it.
- **Evidence**: `pricing.html:168` cancel FAQ mentions export + GDPR
  erasure but not refunds; `webhooks.py:373-410` implements full/partial
  refund handling with no corresponding customer-facing policy.

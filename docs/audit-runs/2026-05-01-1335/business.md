# Business audit — run 2026-05-01-1335

Cross-check of marketing copy on https://assoluto.eu against the
code paths that fulfil those promises. Live-probed the rendered
Czech copy via `curl`. Ten findings filed.

---

### F-BIZ-001 — Backup retention drift: marketing says 14 days, script defaults to 30

- **Where**: `app/templates/www/pricing.html` line 133 + `index.html` line 424, 428; vs `scripts/backup.sh` line 11 + 26
- **Severity**: P0
- **Auto-fixable**: yes
- **Description**: Pricing page trust strip says "Daily backups — kept 14 days, restorable on request." Both index FAQs ("Where is my data stored?", "Is it secure? What if the server fails?") repeat the 14-day number. Terms of Service §SLA also commits to "daily database backups retained for 14 days". The backup script's documented default is `PORTAL_KEEP_DAYS=30`, and the rotation `find … -mtime +30 -delete` will keep 30 days unless the operator overrides the env var. If the prod cron/systemd unit doesn't pass `PORTAL_KEEP_DAYS=14`, we are storing more PII than the Privacy Policy + Terms commit to retaining — a GDPR Art. 5(1)(e) "storage limitation" exposure, not just a marketing fib. Either change the marketing to "kept 30 days" (operationally easier, but rewrites Terms) or change the script default to 14 and verify the prod env var matches.
- **Suggested fix**: Change `KEEP_DAYS="${PORTAL_KEEP_DAYS:-30}"` → `:-14` in `scripts/backup.sh` AND verify `/etc/assoluto/env` either omits `PORTAL_KEEP_DAYS` or sets it to 14. Ssh into prod and `ls -la /backups/portal-*.sql.gz*` to confirm only 14 files are present today.
- **Evidence**:
  - Marketing (live, prod): `<strong>Denní zálohy</strong> — uchované 14 dní, obnova na vyžádání.`
  - `scripts/backup.sh:26`: `KEEP_DAYS="${PORTAL_KEEP_DAYS:-30}"`
  - `scripts/backup.sh:65`: `find "${BACKUP_DIR}" -name 'portal-*.sql.gz' -type f -mtime "+${KEEP_DAYS}" -delete`
  - `app/templates/www/terms.html` SLA clause: `daily database backups retained for 14 days`

---

### F-BIZ-002 — Status page promise is conditional, but no hard verification it's set in prod

- **Where**: `app/templates/www/pricing.html` line 145
- **Severity**: P1
- **Auto-fixable**: no (operator action)
- **Description**: Live curl of `https://assoluto.eu/pricing` returns the fallback string "UptimeRobot 24/7, status page na vyžádání." That means `settings.status_page_url` is empty in production env. The marketing copy degrades gracefully, so this is not a P0 broken promise — but the index.html FAQ #5 still says verbatim "monitor uptime 24/7" without any conditional, and the pricing trust strip's promise of "real" monitoring is undermined when prospects then ask "okay, where's the status page?" and have to email. Either set `STATUS_PAGE_URL=https://stats.uptimerobot.com/<token>` in `/etc/assoluto/env` (free UptimeRobot public status page takes 10 minutes to wire), or accept the current copy is fine — but pick one and move on; today it reads as a "we're not quite there yet" tell on the pricing page.
- **Suggested fix**: Operator action: create UptimeRobot public status page, copy URL into `/etc/assoluto/env`, redeploy. No code change required.
- **Evidence**:
  - Live prod: `<strong>Monitoring dostupnosti</strong> — UptimeRobot 24/7, status page na vyžádání.`
  - `app/config.py`: `status_page_url: str = Field(default="", alias="STATUS_PAGE_URL")`
  - Template branch: `{% if status_page_url %}…{% else %}…status page on request.{% endif %}`

---

### F-BIZ-003 — Demo CTA goes to /contact form, not a real booking link

- **Where**: `app/templates/www/index.html` line 45 (`Book a 15-min demo`); `app/templates/www/contact.html` lines 43-46
- **Severity**: P1
- **Auto-fixable**: no (operator action; needs Calendly/Cal.com account)
- **Description**: Hero CTA "Book a 15-min demo" links to `/contact`, which renders a passive form with the instruction `Write "demo" in the message`. For B2B SME sales the gap between a CTA promising a calendar booking and reality (a free-text contact form + the founder having to email back to find a slot) is a measurable conversion-rate killer. It also creates a "we don't have time / aren't real" perception when the rest of the page is polished. Either (a) wire a real Cal.com / Calendly link directly into the hero button + the contact-page card, or (b) downgrade the CTA copy to "Ask for a demo" / "Get in touch for a demo" so it matches reality.
- **Suggested fix**: Operator action: stand up a free Cal.com event (15-minute Google Meet), put the URL behind both the homepage CTA and the contact-page demo card. Until then, change "Book a 15-min demo" → "Ask for a 15-min demo" in `index.html` and the hero subtitle.
- **Evidence**:
  - `index.html:45`: `{{ _("Book a 15-min demo") }}` (link `href="/contact"`)
  - `contact.html:43-46`: `<p class="text-sm font-semibold">15-minute demo</p>… <p class="mt-1 text-xs">Write "demo" in the message</p>`

---

### F-BIZ-004 — No founder identity on homepage / contact / footer (only buried in /imprint)

- **Where**: `app/templates/www/www_base.html` (footer), `index.html`, `contact.html`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: Václav Mudra's name appears only on the legally-required `/imprint` page. The contact page says "Czech team" / "We reply in Czech or English" — plural fictional team. For a 50-200 EUR/mo B2B SaaS, founder-led trust outperforms generic-team copy: prospects buy from a person, not a "team". The pricing FAQ even says "real people, not a chatbot" but the marketing surface offers no real person. Add a small "About the founder" card to the contact page and/or a one-line "Built by Václav Mudra in Děčín, CZ" to the footer. This is a P1 conversion gap, not a legal issue.
- **Suggested fix**: Add a small founder card to `contact.html` aside (photo + 2 sentences + LinkedIn link) and a one-line attribution above the footer's legal links in `www_base.html` footer. Drop the "Czech team" plural framing — say "Czech support, run by Václav personally" until there's a second person.
- **Evidence**:
  - `contact.html:64-66`: `<p class="text-sm font-semibold">Czech team</p> <p>We reply in Czech or English. Servers in the EU. Invoicing under Czech rules.</p>`
  - Imprint (live prod): `<dd>Václav Mudra</dd><dd>Lidická 2020/2, 405 02 Děčín</dd>`
  - `www_base.html:79-90`: footer is legal-only, no founder mention.

---

### F-BIZ-005 — No trial-nurture email cadence exists

- **Where**: `app/tasks/email_tasks.py`, `app/tasks/periodic.py`
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: Defined email tasks: `send_invitation`, `send_email_verification`, `send_staff_invitation`, `send_password_reset`, `send_order_comment`, `send_order_submitted`, `send_order_status_changed`. Periodic jobs handle `expire_demo_trials` and `enforce_canceled_subscriptions` — i.e. the *punitive* end of the trial — but there is no welcoming side. A 30-day trial without day-1 ("here's how to invite your first client"), day-7 ("how's it going? here's what most shops set up next"), and day-25 ("trial ends in 5 days — here's what happens, here's how to upgrade or cancel") emails leaves money on the table and surprises users with the day-30 cancel. Industry expectation for SME SaaS is at minimum a day-25 reminder so there's no "you charged me without warning" complaint. This is the single biggest activation/conversion lever you currently lack.
- **Suggested fix**: Add `send_trial_welcome` (fired from signup), `send_trial_nudge` (periodic job, fires once at trial day 7 if user has 0 orders), and `send_trial_ending_reminder` (periodic job, fires at trial-end - 5 days). Templates are 30 minutes each. Persist a `last_nurture_step_sent_at` column on `platform_subscriptions` to avoid double-sends.
- **Evidence**:
  - `app/tasks/email_tasks.py` defines no `send_trial_*` / `send_nurture_*` / `send_welcome_*`.
  - `app/tasks/periodic.py:208`: `expire_demo_trials` is the only trial-aware periodic; it cancels, doesn't nudge.

---

### F-BIZ-006 — Testimonial figcaptions present but no logos / first-customer story

- **Where**: `app/templates/www/index.html` lines 358-401
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The testimonials section is honest about pre-launch ("Logos will appear here as soon as clients agree to be published") but already shows three styled quote cards with attribution like "Owner, metalwork shop — Morava, 12 employees". A skeptical SME prospect reads "Early access" + ungrounded quotes as fabricated — worse than no testimonials. Two options: (a) remove the three pre-written quotes entirely until they're real, leaving just the "early access" headline + one CTA; (b) replace at least one with an explicit "founder note" / personal story from Václav about why he's building this (talks to a real person, increases trust without faking customers). Once the first paying customer ships, swap to a real quote with a logo placeholder.
- **Suggested fix**: Delete the three placeholder figure cards, replace with one large "Why I'm building Assoluto" founder quote + photo. Keep the "Early access" badge. Plan to swap to real customer quote once first paying customer signs.
- **Evidence**:
  - `index.html:367`: `Logos will appear here as soon as clients agree to be published.`
  - Quote 3, line 393: `Big ERPs wanted hundreds of thousands… With Assoluto we were running in an afternoon for 490 CZK a month.` — attributed to a fictional Prague co-owner.

---

### F-BIZ-007 — "Czech support reply within 24h" promise has no SLA tracker / autoresponder

- **Where**: `app/templates/www/contact.html` lines 32, 58, 77
- **Severity**: P1
- **Auto-fixable**: no
- **Description**: Three separate copies on the contact page promise "Typical reply within 24 hours" / "We reply within 24 hours" / "Thanks — we will reply within 24 hours." There is no autoresponder configured, no helpdesk, no SLA tracker. As a single-founder operation this commits Václav to checking email 7 days a week (24h, not 1 working day). For weekends, holidays, and travel that promise will break the first time a prospect sends a Friday-evening enquiry and gets a Monday-morning reply. The contact-page subhead actually says "within one working day" — the inner cards over-promise vs the subhead. Fix: align all three card subtexts to "within one working day" so the SLA matches reality, and configure an SMTP autoresponder for `team@assoluto.eu` confirming receipt + restating the working-day SLA so the prospect doesn't wonder whether the form actually went through.
- **Suggested fix**: Edit the three `Typical reply within 24 hours` / `we will reply within 24 hours` strings → `Reply within one working day`. Set up Brevo / SMTP-server-level autoresponder with a one-line "Got your message — we'll reply within one working day" confirmation.
- **Evidence**:
  - `contact.html:17`: `we get back to you within one working day.`
  - `contact.html:32`: `Typical reply within 24 hours`
  - `contact.html:58`: `We reply within 24 hours.`
  - `contact.html:77`: `we will reply within 24 hours.`

---

### F-BIZ-008 — No refund policy stated; pricing FAQ is silent on refunds

- **Where**: `app/templates/www/pricing.html` FAQ block (lines 154-170), `app/platform/billing/webhooks.py` (`charge.refunded` handler exists)
- **Severity**: P2
- **Auto-fixable**: no
- **Description**: The webhook handler `handle_charge_refunded` exists in `app/platform/billing/webhooks.py`, meaning the system is wired to receive Stripe refund events — but no marketing copy or Terms page tells the customer when/why we'd issue a refund (or won't). The "What happens when I cancel?" FAQ explains data export but is silent on whether the in-progress month is refunded. For 490–1490 CZK monthly plans this is small-stakes but the absence of policy language is itself a trust signal — Czech SME owners read absence as "they'll keep my money." Add one line to the cancel FAQ: "Cancelling stops the next charge; the current month is not prorated. For accidental double-charges or annual-plan refund requests, email team@assoluto.eu."
- **Suggested fix**: Add one explicit refund-policy line to the cancel FAQ in both `pricing.html` and `index.html`.
- **Evidence**:
  - `app/platform/billing/webhooks.py`: `"charge.refunded": handle_charge_refunded,`
  - `pricing.html:167` cancel FAQ does not mention refunds; neither does `index.html:430`.

---

### F-BIZ-009 — Two unverified superlative claims in homepage feature copy

- **Where**: `app/templates/www/index.html` lines 33, 117, 152
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Per the honesty-check rubric, flag superlatives without evidence. The hero subhead "Without Excel, without a massive ERP" is fine — comparison, not superlative. But three specific copy claims overreach: (a) line 152 "Five calls a day turn into zero" — this is presented as a feature outcome, not a customer quote, so it's an unsubstantiated outcome promise. (b) line 117 "Built specifically for manufacturing SMEs" is fine. (c) line 33 "Orders, drawings, production status — in one place, 24/7" is fine. The feature outcome on line 152 should either be hedged ("Most calls about order status disappear") or moved into the testimonials block where attribution makes it credible.
- **Suggested fix**: Edit `index.html:152` `Five calls a day turn into zero.` → `Most "where's my order?" calls go away on their own.` (matches the demonstrated outcome of public status, doesn't promise zero).
- **Evidence**:
  - `index.html:152`: `The client sees on their own: DRAFT · SUBMITTED · QUOTED · CONFIRMED · IN PRODUCTION · READY · DELIVERED. Five calls a day turn into zero.`

---

### F-BIZ-010 — Annual-billing offer copy is correct but inconsistent across surfaces

- **Where**: `app/templates/www/pricing.html` lines 113-115, 165
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: The pricing-page annual callout says "typically two months free" (i.e. ~16 % discount). The pricing FAQ #2 ("How does billing actually work?") just says "Annual plan: arranged on request — bank transfer with a Czech invoice, net-14" — silent on the discount. The index FAQ ("Can I pay by bank transfer…") is also silent on the discount. A prospect comparing the two pages may wonder if the discount is real or pricing-only marketing. Mention the "typically two months free" in both FAQ answers so the offer reads consistently. Annual billing is now correctly framed as "on request" everywhere — that 2026-04-25 audit fix has stuck.
- **Suggested fix**: Add `(typically two months free for the year)` to the FAQ answers in both `pricing.html:165` and `index.html` "Can I pay by bank transfer" answer.
- **Evidence**:
  - `pricing.html:114`: `we'll send you a custom offer (typically two months free).`
  - `pricing.html:165` FAQ: `Annual plan: arranged on request — bank transfer with a Czech invoice, net-14.` (no discount mention)

---

## What's correct (verified, no finding filed)

- **Trial length**: `TRIAL_DAYS=30` in `app/platform/billing/service.py:22` matches every "30 days" / "30-day trial" string across pricing.html, index.html, schema.org JSON-LD.
- **Cancel grace**: `CANCEL_GRACE_DAYS=3` in both `service.py:135` and `periodic.py:46` matches the "3 days to export your data" copy in pricing FAQ, index FAQ, terms.html, and the platform/billing/dashboard.html cancel modal.
- **Plan limits**: Migration `1003_billing.py:144-147` seeds Starter at 3 users / 20 contacts / 2048 MB, Pro at 15 / 100 / 20480 MB. Pricing card copy matches exactly. Order limits left NULL on every plan, which matches pricing card "Unlimited orders" copy.
- **Pricing numbers (live prod)**: 490 Kč Starter, 1 490 Kč Pro confirmed via `curl https://assoluto.eu/pricing`.
- **Annual billing on request**: Pricing page says exactly that ("Annual billing on request" callout) — the 2026-04-25 audit fix is intact.
- **Self-host AGPL pitch**: `LICENSE` file is GNU Affero GPL v3 (line 1-2). Self-hosted page consistently brands as AGPL-3.0. `FEATURE_PLATFORM=false` is the default in `.env.example`, confirming opt-in.
- **Trademark notice**: README §"Trademark" correctly uses `™` / "common-law / unregistered" framing with note that ÚPV filing is in progress. Will need a `™` → `®` flip across README + footer once the registration grants.
- **Imprint legal disclosure**: Live `/imprint` correctly renders the §8 Act 480/2004 fields (Václav Mudra, Lidická 2020/2 Děčín, IČO 09989978), with COI ADR + EU ODR links. No legal-disclosure gap.
- **Refund + past_due plumbing**: Both `charge.refunded` and `invoice.payment_failed` Stripe webhook handlers exist (`app/platform/billing/webhooks.py`); `past_due` status is set correctly. Code-level refund handling is fine; only the marketing-copy gap (F-BIZ-008) remains.


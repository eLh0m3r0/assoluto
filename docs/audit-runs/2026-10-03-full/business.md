# Business / go-to-market audit — 2026-10-03-full

Auditor: `business-auditor` · Commit: `015496f` (= `production`) · Severity scale: BRIEF.md
(P0 money wrong / prod broken · P1 blocks selling or payments · P2 noticeably hurts · P3 cosmetic).

**Method.** Read every `app/templates/www/*` page and the Czech catalog strings behind them, traced each
promise into code (`app/platform/billing/*`, `app/tasks/periodic.py`, `app/platform/usage.py`,
`app/routers/www.py`, `scripts/backup.sh`, `docker/Caddyfile`), and probed production **read-only**:
GET on the public site, `ssh assoluto-deploy` for env-var *presence* (secrets redacted), the cron
table, `/home/deploy/backups`, `lsblk`, and `SELECT`s against `platform_plans`,
`platform_subscriptions`, `tenants`, `platform_identities` and `platform_invoices`. Nothing was written to prod.

## Production facts the findings rest on (2026-10-03)

| Fact | Value |
|---|---|
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` / `STRIPE_WEBHOOK_SECRET` | **all unset** (in `/etc/assoluto/env` and in the running `assoluto-web-1` container) → `settings.stripe_enabled == False` → billing runs in **demo mode** |
| `TRIAL_NURTURE_ENABLED` | `true` (sending; markers in `tenants.settings._trial_nurture_sent`) |
| `STATUS_PAGE_URL`, `SENTRY_DSN`, `RCLONE_REMOTE`, `BACKUP_GPG_RECIPIENT`, `PLATFORM_OPERATOR_DIC` | unset |
| `S3_ENDPOINT_URL` | `https://nbg1.your-objectstorage.com` (Hetzner Object Storage, Nuremberg), **not** Backblaze |
| `platform_plans` | starter 49 000 h / 3 users / 20 contacts / ∞ orders / 2048 MB; pro 149 000 h / 15 / 100 / ∞ / 20480; all `stripe_price_id` NULL |
| Subscriptions | 2 `active` starter (no Stripe id: operator's own `assoluto` + `4mex`), 10 `trialing`, 8 `canceled` |
| `platform_invoices` | **0 rows, ever** |
| Signups since 2026-08-23 | 15 of 15 have unverified email, never logged in, 0 contacts, 0 orders, shell-company names (`silverline-llc`, `johnson-consulting`, …), mail.ru/gmail/outlook.fr |
| Backups | cron `0 3 * * *` → `/home/deploy/backups` on the **same** disk; log says `UNENCRYPTED` every night; no off-site copy; root disk 88 % full |
| Disk | `/dev/sda1 ext4`, no LUKS/dm-crypt |
| Uptime workflow | cron `*/15`, but the last five runs were 3.5–4.7 h apart (GitHub throttling) |
| `/status`, `/security`, `/.well-known/security.txt`, `/dpa` | 404 |

## Rechecked, now coherent (do not re-report)

| Check | Result |
|---|---|
| Trial length | `TRIAL_DAYS = 30` (`billing/service.py:22`) = all copy. (Stale "fresh 14-day window" comment at `service.py:304`, nothing user-facing.) |
| Plan limits | `/pricing` cards = `platform_plans` rows on prod exactly; "Unlimited orders" true (`max_orders_per_month` NULL). |
| Cancel grace | `CANCEL_GRACE_DAYS = 3` (`service.py:149`) + `enforce_canceled_subscriptions` = "3 days to export". |
| Refund copy | "No mid-period refunds" matches code (no automated refund; `charge.refunded` only records). |
| Annual billing | Still "on request" (`pricing.html:114-115`), as fixed on 2026-04-25. |
| AGPL / opt-in SaaS | `LICENSE` AGPL-3.0, GitHub reports agpl-3.0; `FEATURE_PLATFORM` defaults `False` (`config.py:71`). |
| VAT at checkout | resolved (2026-07-26 fix): `automatic_tax` follows `PLATFORM_OPERATOR_DIC`; pricing states "not VAT-registered". Leftover wording is in BIZ-15. |
| Data export | resolved: `GET /app/admin/export` (`tenant_admin.py:688`) fulfils the Terms §8 "CSV / ZIP" promise. |
| Pro card → Pro trial | resolved in code (`signup.py` → `plan_code=selected_plan`). Terms still say Starter (BIZ-19). |
| Notification preferences claim | resolved by PR #7 ("Each user sets what they want to be notified about" is now true). |
| Trial nurture | resolved as a mechanism (enabled, sending). Targeting and content problems are in BIZ-09 and BIZ-14. |

---

### BIZ-01 — Production billing runs in demo mode: "Upgrade" gives the plan away free, says it worked, and then cuts the customer off
- Severity: P0
- Status vs 2026-07-26: persisted (FIXES "Operator action… STRIPE_PRICE_* unset"), and worse than recorded: `STRIPE_SECRET_KEY` is unset too, so this is the demo flow, not a failing checkout.
- Evidence: prod env and container have no `STRIPE_SECRET_KEY`, so `config.py:193-195` makes `stripe_enabled` False. `create_checkout_session` then returns `success_url` (`billing/service.py:314-318`). `start_checkout` sets the plan with `status="demo"` (`routers/billing.py:297-300`), and the dashboard flashes "Předplatné bylo úspěšně aktualizováno." (`billing.py:186-187`) next to an amber "Demo mode (Stripe not configured)" badge that the customer can see (`billing/dashboard.html:64-67`). The trial clock is left untouched. `expire_demo_trials` cancels `status IN ('trialing','demo')` at the original `trial_ends_at` (`periodic.py:243-250`), and `enforce_canceled_subscriptions` deactivates the tenant 3 days later. Tenants with no `trial_ends_at` (the two manual `active` rows, including the only real customer `4mex`) never expire, so one click gives Pro free indefinitely. `enterprise` is not in `HIDDEN_PLAN_CODES` (`service.py:49`), so a hand-made POST to `/platform/billing/checkout/enterprise` gives unlimited caps. `platform_invoices` has 0 rows.
- Dopad: A prospect who decides to pay (the `trial_ending` mail sends them to `/platform/billing`) gets a success message and pays nothing. At the old trial end their portal, and their clients' access, goes dark with no warning. That is the worst possible first billing experience and a likely chargeback or complaint. Plan caps (the only upgrade trigger) can be skipped for free by anyone. Revenue is structurally zero.
- Návrh: (1) Operator: create Stripe live products and set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER` and `STRIPE_PRICE_PRO`, then run one real card end to end. (2) Code guard: when `APP_ENV=production` and `FEATURE_PLATFORM` is on, refuse the demo path in `start_checkout`/`post_verify_checkout`. Show "Online payment is being set up — email team@assoluto.eu and we'll invoice you", log an error, and never set `status="demo"`. Add `enterprise` to the non-checkout set. Hide the demo badge from tenants and show it to platform admins only.
- Effort: S (operator) + S (guard)
- Auto-fixable: yes for the code guard; no for the Stripe account and keys

### BIZ-02 — A Starter trialist has no button to pay for Starter; the "choose a plan" email leads to a dead end
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: in `platform/billing/dashboard.html:140-181` the plan matching `current_plan` shows only the text "Current plan", with no form. The trial is created on the chosen plan (Starter by default), so a trialing Starter tenant sees "Current plan" on Starter, "Upgrade to Pro" and "Contact us". There is nothing like "Keep Starter / add card" anywhere on the page (grep finds no `trialing` CTA except the date at line 54). The only Starter checkout is the one-shot "Finish setting up Starter" on `verify_email.html:14-26`. `trial_ending.txt` says "pick a plan before then: Choose a plan → /platform/billing". After expiry, `plan_id` stays Starter (`periodic.py:244`), so a lapsed Starter tenant can also only buy Pro. No test covers a Starter checkout from the dashboard (only `tests/test_billing_details.py` mentions the URL).
- Dopad: Your most common buyer, a small shop on the 490 Kč tier, cannot convert after day 0. They either pay 3× for Pro or churn. Once BIZ-01 is fixed this becomes the main leak in the paid funnel.
- Návrh: For `status in ('trialing','canceled')`, or a current plan with no `stripe_subscription_id`, render the current-plan card as a primary "Pokračovat na {{ plan.name }} — zadat kartu" POST to `/platform/billing/checkout/{code}`. Add a test: a trialing starter tenant gets a form posting to `/checkout/starter`.
- Effort: S
- Auto-fixable: yes

### BIZ-03 — "Change plans any time, prorated to the day; downgrade at next period" is not what the code does
- Severity: P1
- Status vs 2026-07-26: persisted (F-07)
- Evidence: `pricing.html:168` says "The difference is prorated down to the day. Downgrade happens at the start of the next billing period." The only plan-change path is `start_checkout`, which calls `create_checkout_session` and opens a **new** Stripe subscription. `stripe.Subscription.modify` exists only with `cancel_at_period_end=True` (`service.py:235-238`). No proration, no scheduled downgrade. The Stripe Customer Portal button (`dashboard.html:73-84`) only helps if the operator has turned on plan switching in the Stripe dashboard, which the code does not guarantee.
- Dopad: The first live upgrade bills the customer twice every month. The pricing page is binding (Terms §4 "form part of these Terms").
- Návrh: Before going live, implement upgrade/downgrade with `Subscription.modify(items=[{id, price}], proration_behavior='create_prorations')` for upgrades and a subscription schedule for downgrades. Until then, change the FAQ to "Plan changes: email us and we'll switch you the same day".
- Effort: M (needs Stripe test mode)
- Auto-fixable: no (Stripe account); the interim copy change is yes

### BIZ-04 — The homepage's "first customers" testimonials and the "Nejoblíbenější" badge are not backed by any customer
- Severity: P1
- Status vs 2026-07-26: new. The panel noted "customers the founder cannot name"; the dates below go further.
- Evidence: `index.html:362-396` has the heading "What our first customers say" and three quotes credited to a 12-person metal shop (Morava), a 22-person CNC shop (Vysočina) and an 8-person sheet-metal shop (Prague). They arrived in commit `1c353a1` on **2026-04-24**, described as "persona copy… pain-point interviews embedded in the copy". The first non-test tenant (`4mex`) was created on **2026-05-04**. Production holds exactly one external tenant with real use (`4mex`: 3 staff, 5 contacts, 24 orders). The second quote praises revision control, which does not exist (BIZ-10). `index.html:311` puts "Most chosen" (CS "Nejoblíbenější", live today) on Pro, while 0 subscriptions have ever been Pro. *Unverified:* whether any quote paraphrases a real interviewee. Only the founder can confirm.
- Dopad: Made-up endorsements are misleading advertising under §2976–2977 OZ and zákon 40/1995 Sb. A competitor or a sceptical prospect can expose them. Being caught here destroys exactly the trust the site is built on ("A honest price list"). It also hides the real asset: one live customer.
- Návrh: Remove the three quotes and the "Most chosen" badge now (use "Doporučujeme", as `/pricing` already does). Replace them with a founder note ("Proč jsem Assoluto postavil" with name, photo and LinkedIn) and, once 4mex agrees, one named mini case study with real numbers (orders/month, share of clients who log in, calls avoided).
- Effort: S
- Auto-fixable: yes (removal and badge); no (case study, needs customer consent)

### BIZ-05 — "Encrypted at rest" and "encrypted backups" are false, and "daily backups" sit on the same disk as the database
- Severity: P1
- Status vs 2026-07-26: new (encryption); persisted (F-01 second half, attachments not backed up; F-25 unencrypted dumps)
- Evidence: the claims are in `index.html:424` and the JSON-LD ("The PostgreSQL database is encrypted at rest"), `pricing.html:126` ("Hetzner DE, encrypted at rest"), privacy table `privacy.html:80` ("All Customer data (encrypted at rest)"), `privacy.html:97` ("encrypted database backups") and `privacy.html:175` §9 ("encrypted backups"). On prod: `lsblk -f` shows plain `ext4` on `/dev/sda1` with no crypt layer. `/home/deploy/assoluto-backup.log` prints "UNENCRYPTED — set BACKUP_GPG_RECIPIENT" every night. Dumps go to `/home/deploy/backups` on the same disk, and `RCLONE_REMOTE` is unset in both the env and the cron line, so nothing leaves the VPS. The attachment bucket has no backup at all (`scripts/backup.sh` only dumps Postgres).
- Dopad: These are security statements in the privacy policy, which customers append to their own Art. 28 records. A CTO reviewer who asks "show me" gets a no. Commercially, one lost or compromised VPS takes the database and all 14 "backups" with it, so "restorable on request" (`pricing.html:134`) cannot be delivered when it matters.
- Návrh: Operator: set `BACKUP_GPG_RECIPIENT` and `RCLONE_REMOTE` (a second provider or region with versioning and object lock) in the cron line, and add an `rclone copy` of the attachment bucket. Copy, until that is done: "Databáze se zálohuje denně (14 dní)". Remove "encrypted at rest" for the VPS. Keep it for object storage only after checking Hetzner's documentation. Change §9 "encrypted backups" to the truth.
- Effort: S
- Auto-fixable: yes (copy); no (operator keys and remote)

### BIZ-06 — Uptime story ("UptimeRobot 24/7", "redundant infrastructure", "99.9 % SLA") has nothing behind it
- Severity: P1
- Status vs 2026-07-26: persisted (panel: "Nothing a buyer needs is visible"); new evidence on monitoring cadence
- Evidence: `pricing.html:146` says "UptimeRobot 24/7, status page on request" (`STATUS_PAGE_URL` unset, no UptimeRobot in repo or env). `index.html:428` says "Hetzner runs redundant infrastructure… monitor uptime 24/7. Target uptime: 99.9 %". The Enterprise card at `pricing.html:98` says "✓ SLA 99,9 %", and Terms §8 (`terms.html:117`) "targets 99.9 % monthly uptime measured at the application HTTP layer". In reality: `.github/workflows/uptime.yml` (its own header: "best-effort… not an SLA meter"), with the last five runs at 04:41, 10:04, 14:44, 18:19 and 21:40 UTC, about every 4 h. One VPS, one Postgres container, `SENTRY_DSN` unset, `/status` 404.
- Dopad: An outage can go unnoticed for hours. Nothing can show what uptime was actually achieved, so the 99.9 % Enterprise SLA cannot be sold honestly or defended in a dispute. B2B buyers read a missing status page as "nobody is watching".
- Návrh: Set up a free UptimeRobot or Better Stack monitor (1–5 min) with a public status page, set `STATUS_PAGE_URL` (the template already switches to the link), and point `/status` at it. Change the copy to "monitoring každých 5 min, veřejná status stránka". Drop "redundant infrastructure". Change the Enterprise bullet from a number to "SLA dle dohody" until there are 3 months of measured history.
- Effort: S
- Auto-fixable: yes (copy); no (monitor account)

### BIZ-07 — The subprocessor table names Backblaze for drawings and backups, but production uses Hetzner Object Storage and makes no off-site backups
- Severity: P2
- Status vs 2026-07-26: regressed. The July fix added a Backblaze row from the docs, and prod does not match it.
- Evidence: `privacy.html:96-99` lists "Backblaze B2 (Backblaze Inc.) — Object storage for uploaded files and encrypted database backups — EU (DE)". Prod has `S3_ENDPOINT_URL=https://nbg1.your-objectstorage.com` (Hetzner) and `RCLONE_REMOTE` unset. Backblaze Inc. is a US company, and no transfer mechanism is stated (unlike the Stripe row). The `index.html:424` FAQ says data lives "**exclusively** on Hetzner servers", while Brevo (FR) receives every email body including order titles and comments.
- Dopad: The Art. 28(2) annex customers sign is wrong in a way anyone can check. It also overstates where drawings live, and pairing a US vendor with an EU location looks careless to a DPO.
- Návrh: Change the Hetzner row to "VPS + Object Storage (Nuremberg)". Remove the Backblaze row, or keep it marked "off-site backup (planned)" only once BIZ-05 makes it real, with an SCC note. Change "exclusively" to "primárně… e-maily doručuje Brevo (FR)". Bump the version and date (see BIZ-19).
- Effort: S
- Auto-fixable: yes

### BIZ-08 — Terms and Privacy promise deletion 30 days after deactivation; no code deletes anything
- Severity: P1
- Status vs 2026-07-26: persisted (F-26, narrowed). Now confirmed with data on prod.
- Evidence: `terms.html:62` says "after 30 days the data is permanently deleted", and `privacy.html:123` says the same. The scheduled jobs in `periodic.py` (auto-close, stale invites, stripe events, expire trials, enforce canceled, nurture) do not include a tenant purge. On prod, `test-a`, `test-b` and `testfirma` were deactivated in late May and are still fully present (users, contacts, orders) four months later. S3 objects of deactivated tenants are never removed. `features.html:191` claims "Audit trail… 3 years retention", while privacy says "lifetime of the tenant + up to 3 years", and nothing enforces either.
- Dopad: A binding storage-limitation promise is broken (GDPR Art. 5(1)(e)), and for end-client data you are the processor. This is the first thing a DPA reviewer asks about after "where is it". Bot tenants (BIZ-09) also pile up indefinitely.
- Návrh: Add a `purge_deactivated_tenants` daily job: tenants with `is_active=false` for more than 30 days lose their DB rows (except invoices, which stay 10 years) and their S3 prefix, and the purge is logged to the platform audit. Add a `--dry-run` CLI first. Align features.html with privacy on audit retention.
- Effort: M
- Auto-fixable: yes (code); the operator should review a dry run before turning it on

### BIZ-09 — The trial funnel is all bots, the nurture emails go to unconfirmed addresses, and the MRR tile shows 5 880 Kč of revenue that does not exist
- Severity: P1
- Status vs 2026-07-26: new (bot funnel, nurture targeting); persisted (panel: MRR counts trialing/demo)
- Evidence: all 15 signups since 2026-08-23 have `email_verified_at IS NULL` and `last_login_at IS NULL`, plus shell-company names and mail.ru/gmail. Each one received verification, day1, day7 and "ending" mails: `_run_trial_nurture` (`periodic.py:462-498`) selects on subscription status and `role == TENANT_ADMIN` with no verification filter. Signup has a honeypot and rate limits, but none of the `is_disposable_email` / `looks_like_bot_local_part` filters the contact form uses (`www.py:139-142`). MRR (`platform_admin.py:231-243`) sums `status IN ('active','trialing','demo')`: 10 bot trials plus the 2 manual tenants make 5 880 Kč, against 0 invoices.
- Dopad: (1) The founder's dashboard shows traction that is not there, which is how BIZ-01 survived five audits. (2) Up to 4 unsolicited emails per address someone else typed in. That is an email-bombing relay that hurts the Brevo sender reputation your clients' order notifications depend on. (3) Real conversion is unmeasurable because the denominator is all bots.
- Návrh: Send nurture only when the owner identity's `email_verified_at IS NOT NULL`. Expire unverified signups after 7 days with no nurture. Reuse the contact-form spam filters on signup. Compute MRR from `status='active' AND stripe_subscription_id IS NOT NULL` (plus a manual "invoiced" flag), and show trials as a separate number of *verified* trials.
- Effort: S
- Auto-fixable: yes

### BIZ-10 — Feature claims still ahead of the code: "you quote the price **and deadline**", "the right **revision** every time"
- Severity: P1
- Status vs 2026-07-26: persisted (F-30; panel "revision control has no schema")
- Evidence: `features.html:31` says "You quote the price and deadline", and `features.html:16` "drawings, revisions, deadlines". `Order.promised_delivery_at` (`models/order.py:92`) is read by the CSV, PDF, detail page, export and `sla_service.py`, but nothing writes it: no form field, no service parameter, and no order edit route at all (`orders.py` routes: list, csv, new, create, pdf, detail, items, transition, comment, assign). So `/app/admin/sla` shows 0 % for every tenant. `index.html:145` says "The right revision next to the right order — every time", but `models/attachment.py` has no revision, supersedes or is_current column.
- Dopad: The deadline is the information a supplier's client wants most. A trialist who looks for "set deadline" and finds nothing, or opens an SLA report full of zeros, decides the product is unfinished. The revision line is the headline differentiator, and today it is the least true sentence on the site.
- Návrh: Short term (copy): "Oceníte zakázku" and drop "revisions" until it is built. Product: a `promised_delivery_at` input on the QUOTED transition plus a staff order-edit form. That lights up the SLA dashboard, the PDF and the customer email at once. Revision chains (`supersedes_id`, `is_current`) as the next differentiator.
- Effort: S (copy) / M (deadline + edit) / M (revisions)
- Auto-fixable: yes (copy); no (feature scope decision)

### BIZ-11 — Three different support promises, no tracker, and contact-form leads can vanish silently
- Severity: P2
- Status vs 2026-07-26: persisted (panel: 48 h/12 h vs pricing); new (lead handling)
- Evidence: homepage cards at `index.html:301` and `:324` say "Email support (48 h)" and "Priority email support (12 h)" (live). `/pricing` at `pricing.html:55` and `:79` says "reply within 1 working day" and "same working day". `/contact` says "Odpovídáme do 1 pracovního dne". `contact_submit` (`www.py:184-204`) only emails `team@assoluto.eu` through `_safe_send`, which swallows failures. Nothing is saved to the DB. There is no `Reply-To` header anywhere in `app/email/sender.py:144-150`, so replying means copying the address out of the body. The prospect gets no confirmation email.
- Dopad: Inconsistent promises read as "nobody owns this". One SMTP hiccup and an Enterprise lead is lost without anyone knowing. For a solo founder, the reply time is the product demo.
- Návrh: One promise everywhere: "odpověď do 1 pracovního dne" (Pro: "týž pracovní den"). Save contact submissions to a small `platform_leads` table, set `Reply-To: <visitor>` on the forwarded mail, and send the visitor a short confirmation signed by Václav.
- Effort: S
- Auto-fixable: yes

### BIZ-12 — "Request a 15-min demo" is a contact form where you type "ukázka"; there is nothing to click without signing up
- Severity: P2
- Status vs 2026-07-26: persisted (panel priority 6)
- Evidence: the `index.html:45` CTA links to `/contact`. The live contact page says "15minutová ukázka… Do zprávy napište ‚ukázka'". There is no booking link. `/security`, `/dpa` and `/status` return 404. The hero is an HTML mock (`_hero_mock.html`) with invented clients, and `index.html` / `features.html` contain no screenshots.
- Dopad: A prospect who wants to see the product has to write an email and wait a day. The panel's prospect persona stopped exactly here. Each extra step loses a share of the few real visitors you get.
- Návrh: A Cal.com/Calendly 15-minute slot linked directly from the CTA. Three real screenshots of the seeded demo tenant. Later, a read-only `demo.assoluto.eu` with printed credentials and a nightly reset.
- Effort: S (booking + screenshots) / M (public demo tenant)
- Auto-fixable: no (booking account, screenshot choice)

### BIZ-13 — The supplier's own customers see "Payment overdue" and "Subscription ended" banners
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app_base.html:8-40` renders the `past_due` and `canceled` banners for any `principal`, deliberately ("Visible to ANY tenant user (so a contact can prod the admin…)"). The status is set for every request in `deps.py:219-247, 289`. A customer contact therefore reads "Your most recent invoice failed. Stripe is retrying…" or "Your access ends after the data-export grace period… contact us within 30 days" on their supplier's portal.
- Dopad: Your customer's payment trouble is shown to *their* customers. That is embarrassing enough to cause churn, and contacts cannot act on it anyway. Small shops do have card failures.
- Návrh: Show the banners to staff only. For contacts, show nothing, or a neutral "Portál bude dočasně nedostupný od {date}" only in the final 3-day grace window.
- Effort: S
- Auto-fixable: yes

### BIZ-14 — Packaging fits a Czech job shop poorly: seat-based caps, a 3× jump, 2 GB for CAD, a free unlimited tier shown first, and no warning before the wall
- Severity: P2
- Status vs 2026-07-26: persisted (panel "Community first", "metered on headcount"); new detail
- Evidence: Starter allows 3 staff, 20 contacts and 2 GB. Pro allows 15, 100 and 20 GB at 1 490 Kč (`migrations 1003`; prod rows). A shop with 5–50 client firms at about 2 contacts each needs 10–100 contacts, so Starter covers roughly 10 client firms. With 50 MB per file allowed (`features.html:87`), STEP/DWG sets fill 2 GB after about 40–100 drawings. The trigger to upgrade is the 4th staff user or the 21st contact, and it shows up as a hard 402 (`errors/plan_limit.html`). The only advance warning is a set of usage bars on `/platform/billing`, which no one visits. `pricing.html:23-39` shows Community ("Free · No user limits") as card #1. No annual price is published, only "on request".
- Dopad: The step from 490 to 1 490 Kč lands when the customer hires someone, not when they get more value. The free card sends technically capable prospects away from SaaS. Running out of storage mid-order upsets users more than it sells upgrades.
- Návrh (founder decision): (a) Price on active client firms, with unlimited staff (e.g. Starter ≤ 10 firms, Pro ≤ 50). (b) Raise Starter storage to at least 10 GB, since Hetzner Object Storage costs far less than 490 Kč/TB. (c) Show an in-app banner at 80 % of any cap with "Upgrade". (d) Move Community to the bottom or to `/self-hosted`, and sell hosted on operations (backups, monitoring, onboarding). (e) Publish an annual price ("10 měsíců za 12").
- Effort: S (banner, card order) / decision (prices)
- Auto-fixable: no

### BIZ-15 — Leftover "daňový doklad" wording contradicts the non-VAT-payer position, and "invoice emailed the same day" has no code behind it
- Severity: P2
- Status vs 2026-07-26: persisted remainder of the VAT fix
- Evidence: the CS Terms §4 (`messages.po:5853-5854`) say annual plans are paid "s českou fakturou (**daňovým dokladem**)". The billing dashboard tooltip reads "Daňový doklad (CZ-compliant)" (`messages.po:3405`). The billing-details page says "na každém daňovém dokladu" (`:3429`). Privacy §2/§3 says "povinné údaje českého daňového dokladu", "Vystavování daňových dokladů" (`:5189`, `:5236`). Meanwhile the imprint says "Neplátce DPH" and `/pricing` says "dostanete fakturu, nikoli daňový doklad". `pricing.html:167` promises "invoice emailed the same day", but no email template or task sends invoices (`app/email/templates` has no invoice template). The Czech PDF is download-only, and the Stripe receipt is not a faktura.
- Dopad: The binding Terms say you will issue a document a non-VAT payer cannot issue, and buyers' accountants notice. The "emailed invoice" promise will be broken on the first payment.
- Návrh: Replace with "faktura" / "doklad" everywhere (Terms, privacy, billing UI, all 3 locales). Either email the generated PDF from `handle_invoice_paid`, or change the copy to "faktura ke stažení v Předplatném".
- Effort: S
- Auto-fixable: yes

### BIZ-16 — No lifecycle email beyond the three nurture steps, and the nurture ignores whether the trialist has done anything
- Severity: P2
- Status vs 2026-07-26: new (nurture now ships, the gaps around it remain)
- Evidence: `app/email/templates` has `trial_day1`, `trial_day7` and `trial_ending` only. There is no "trial ended, 3 days to export", no "account deactivated", no cancellation confirmation and no win-back. `_due_nurture_stage` (`periodic.py:377-401`) is purely time-based, so day7 says "If a client is already placing orders — ignore this" to everyone. The app shows no trial countdown (`app_base.html` banners cover only `past_due` and `canceled`). Platform admin shows no activation metric (first contact invited, first order, contact logins), although `customer_contacts.last_login_at` now exists. There is no referral mechanism and no funnel analytics, by design (no cookies).
- Dopad: The tenant goes dark at day 33 without a direct warning. Activation, the one predictor of conversion, cannot be seen or steered.
- Návrh: (1) Send emails on expiry and deactivation, and confirm cancellations. (2) Branch day7 on activation: no customer yet → "pozvěte prvního klienta"; customer but no order → a different tip. (3) Show "Zkušební verze: zbývá N dní" to tenant admins in the app. (4) Platform admin: per-tenant columns for invited contacts, contacts logged in within 30 days, and orders in the last 30 days. (5) Count signups and conversions server-side, without cookies.
- Effort: M
- Auto-fixable: yes (1–4); (5) needs a choice of tool

### BIZ-17 — Trust surfaces a B2B buyer checks are missing: founder, /security, security.txt, DPA, status
- Severity: P2
- Status vs 2026-07-26: persisted (panel priority 6)
- Evidence: Václav Mudra appears only on `/imprint` (with IČO 09989978 and the Děčín address, which is good). There is no About or founder section and no photo or LinkedIn on index, contact or footer (`www_base.html`). `/security`, `/.well-known/security.txt` and `/dpa` return 404. The DPA is "on request" in 3 places (pricing, terms §7, privacy §1). `SECURITY.md` is reachable only through GitHub.
- Dopad: For a 490–1 490 Kč tool from a sole trader, the buyer's question is "who is behind this and will he be here next year?" A face, a published DPA and a security page answer that cheaply. Every 404 adds an email round-trip.
- Návrh: Add a founder block on the homepage and `/contact` ("Václav Mudra, Děčín — píšu kód i odpovídám na e-maily"). Publish `/security` (built from SECURITY.md plus RLS, Argon2id and the subprocessor table), `/.well-known/security.txt`, and a `/dpa` PDF.
- Effort: S
- Auto-fixable: yes (pages); no (photo and DPA text sign-off)

### BIZ-18 — The Enterprise card promises a custom domain ("portal.yourfirm.cz") that hosting cannot serve
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `pricing.html:96` says "Your own domain (portal.yourfirm.cz)". Tenants are resolved only by header, subdomain of the apex, or the default slug (`deps.py:78-98`). There is no custom-domain field on `Tenant`. Caddy serves only `{$PORTAL_DOMAIN}, *.{$PORTAL_DOMAIN}` (`docker/Caddyfile`). Session cookies and the CSP `form-action` assume the apex (CLAUDE.md §10).
- Dopad: The first Enterprise conversation turns into "that will take a while". It also signals a promise made without checking.
- Návrh: Remove the bullet, or write "vlastní doména (on-premise / na vyžádání)". Build it (`tenants.custom_domain`, Caddy on-demand TLS with an `ask` endpoint, cookie domain handling) only when a deal needs it.
- Effort: S (copy) / L (feature)
- Auto-fixable: yes (copy)

### BIZ-19 — Legal page housekeeping: version never bumped, Terms say a Starter-only trial, the EU ODR link is dead, the self-host instructions don't work
- Severity: P3
- Status vs 2026-07-26: new / persisted (panel self-host; F-43)
- Evidence: Terms and Privacy both say "Version 1.0 · effective from May 1, 2026" (`terms.html:9`, `privacy.html:9`), despite the VAT changes and a new subprocessor row in July. Privacy §4/§10 promise 30 days' notice for subprocessor changes. `terms.html:56` says "30-day free trial of the **Starter** plan", but the Pro card now starts a Pro trial. `imprint.html:45-47` links the EU ODR platform, which was shut down on 20 July 2025 (Reg. (EU) 2024/3228), plus ČOI consumer ADR although Terms §1 excludes consumers. `self_hosted.html:80-82` says "docker-compose.prod.yml ready for nginx + TLS" with a single-file command, but the file needs the base compose, uses Caddy and hard-requires `PORKBUN_API_KEY` (`docker-compose.prod.yml:150-151`). `cookies.html:61` lists an `sme_theme` cookie, but the theme lives in `localStorage["theme"]` (`app.js:170`). The footer GitHub link goes to the old repo name (`elh0m3r0/sme-client-portal`, which 301-redirects). Trademark is unregistered (NOTICE.md "ÚPV filing in progress"); no ™/® appears on the site.
- Dopad: Small credibility cuts each time a careful reader checks. The legal pages promise a change process they don't follow.
- Návrh: Bump to v1.1 with a changelog line. Terms §4: "trial of the plan selected at signup". Remove the ODR row. Fix the self-host copy (Caddy, base + prod compose, any DNS provider). Fix the cookie row and the repo URL. Operator: file at ÚPV (class 9/42) and record the filing number in NOTICE.md.
- Effort: S
- Auto-fixable: yes (except the ÚPV filing)

---

## Summary

| Severity | Count | IDs |
|---|---|---|
| P0 | 1 | BIZ-01 |
| P1 | 8 | BIZ-02, 03, 04, 05, 06, 08, 09, 10 |
| P2 | 9 | BIZ-07, 11, 12, 13, 14, 15, 16, 17, 18 |
| P3 | 1 | BIZ-19 |

**Commercial read.** Assoluto has taken no money: 0 invoices, and Stripe is not even in live-key mode. It has one real external user (`4mex`), and every self-serve trial for six weeks came from bots. The fastest path to first revenue is not a feature. It is BIZ-01 + BIZ-02 (make it possible to pay, for the plan the customer is on), BIZ-09 (see the real funnel), then a one-day truth pass covering BIZ-04/05/06/07/10/15/18, so the first paying customer does not catch a false claim. Packaging (BIZ-14) is a founder decision best made after about 10 discovery calls. Until then, "always true" beats "cheaper".

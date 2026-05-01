# Business audit — run 2026-05-01-1455 (verification of 1335)

Re-ran the 15 mandatory marketing-↔-code coherence + honesty checks
against the four claimed fixes from the prior run. Live-probed CS, EN
and DE renderings of /pricing, /contact, /, /imprint on
https://assoluto.eu. Plus a fresh sweep for new business surfaces
introduced since 1335.

Resolved: 4 (F-BIZ-001, F-BIZ-007, F-BIZ-009, F-BIZ-010).
Persisted as expected (operator action / deferred): 6 (F-BIZ-002,
F-BIZ-003, F-BIZ-004, F-BIZ-005, F-BIZ-006, F-BIZ-008).
New: 1 (F-BIZ-011 fuzzy EN catalog).

---

### F-BIZ-001 — Backup retention drift (RESOLVED)

- **Where**: `scripts/backup.sh:29`
- **Status**: resolved in `cda17c6`
- **Evidence**:
  - `KEEP_DAYS="${PORTAL_KEEP_DAYS:-14}"` — default flipped 30→14
  - Header comment now references "the marketing + Terms commitment of 'daily backups, 14 days retention' and the GDPR Art. 5(1)(e) storage limitation."
  - Pricing live (CS): `<strong>Denní zálohy</strong> — uchované 14 dní, obnova na vyžádání.`
  - Pricing live (EN): `<strong>Daily backups</strong> — kept 14 days, restorable on request.`
  - Operator must still ensure `/etc/assoluto/env` either omits `PORTAL_KEEP_DAYS` (default applies = 14 ✓) or sets it to 14. Cannot SSH-verify from this audit context — operator action item carried over.

---

### F-BIZ-002 — Status page URL not configured (PERSISTED, expected)

- **Where**: `app/templates/www/pricing.html` trust strip
- **Status**: persisted (operator action, no code change scheduled)
- **Evidence**: Live EN: `<strong>Uptime monitoring</strong> — UptimeRobot 24/7, status page on request.` — fallback branch still active, `STATUS_PAGE_URL` empty in prod env.

---

### F-BIZ-003 — Demo CTA goes to /contact form (PERSISTED, expected)

- **Where**: `app/templates/www/index.html` hero CTA
- **Status**: persisted (operator action, needs Cal.com / Calendly account)
- **Evidence**: Live homepage scrape — five `/contact` href targets including the hero `Book a 15-min demo` button. No Cal.com / Calendly link.

---

### F-BIZ-004 — Founder identity not on homepage / contact / footer (PERSISTED, expected)

- **Where**: `app/templates/www/www_base.html` footer, `index.html`, `contact.html`
- **Status**: persisted (operator decision)
- **Evidence**:
  - `curl https://assoluto.eu/` — zero matches for `Mudra|Václav|founder|Built by`.
  - `curl https://assoluto.eu/contact` — zero matches.
  - `/imprint` still correctly carries `Václav Mudra · Lidická 2020/2 · Děčín` (legally required, not marketing surface).

---

### F-BIZ-005 — No trial-nurture email cadence (PERSISTED, expected)

- **Where**: `app/tasks/email_tasks.py`, `app/tasks/periodic.py`
- **Status**: persisted (substantive feature, deferred)
- **Evidence**: `grep -nE "send_trial|send_welcome|send_nurture" app/tasks/*.py` returns nothing. Punitive `expire_demo_trials` periodic still the only trial-aware job.

---

### F-BIZ-006 — Testimonial placeholders still on homepage (PERSISTED, expected)

- **Where**: `app/templates/www/index.html` testimonials section
- **Status**: persisted (operator decision — waiting for first real customer)
- **Evidence**: Live homepage carries `Early access` badge intact; three placeholder figcaptions unchanged.

---

### F-BIZ-007 — "Czech support reply within 24h" SLA copy (RESOLVED)

- **Where**: three msgids on `app/templates/www/contact.html`
- **Status**: resolved in `82d5458` via translation layer
- **Verification (live)**:
  - CS: `Odpovídáme do 1 pracovního dne` (twice on contact card subtexts) and meta-description / hero subhead aligned to `1 pracovního dne`. Form-submit success card translates to `Děkujeme — ozveme se do 1 pracovního dne.`
  - DE: `Antwort binnen 1 Arbeitstag`, `GPG-Schlüssel auf Anfrage. Antwort binnen 1 Arbeitstag.`, success card msgstr `Vielen Dank — wir melden uns binnen 1 Arbeitstag.`
  - EN: `Reply within 1 working day`, `GPG key on request. We reply within 1 working day.`, success card msgstr `Thanks — we will reply within 1 working day. For anything urgent, email us directly at team@assoluto.eu.`
- **Note**: msgids in `contact.html` deliberately left as English source ("...within 24 hours") — the fix lives in the .po msgstrs (CLAUDE.md §7). Re-extracting the catalog without care could re-fuzzy these — keep an eye on it.
- **Carry-over operator action**: SMTP autoresponder for `team@assoluto.eu` still not configured; the SLA-side commitment matches reality only if Václav actually replies within 1 working day end-to-end. Not a P1 anymore now that copy is honest.

---

### F-BIZ-008 — No refund policy in pricing FAQ (PERSISTED, expected)

- **Where**: `app/templates/www/pricing.html` FAQ + `index.html` FAQ
- **Status**: persisted (operator decision)
- **Evidence**: `curl https://assoluto.eu/pricing | grep -iE "refund|money.back|prorat"` returns nothing. `charge.refunded` Stripe handler still wired in code but no marketing-surface explanation.

---

### F-BIZ-009 — Superlative "Five calls a day turn into zero" (RESOLVED)

- **Where**: `app/templates/www/index.html:152` (now `index.html:158` per .po `#:` ref)
- **Status**: resolved in `82d5458`
- **Verification (live)**:
  - EN: `Most "where's my order?" calls go away on their own.`
  - CS: `Většina dotazů „kde je má zakázka?” se …` (line truncated by grep but msgstr in .po is complete)
  - DE: `Die meisten „wo ist mein Auftrag?"-Anrufe lösen sich von selbst.`
  - The customer testimonial on line 375 ("Five calls a day asking about orders turned into zero. I finally have time for the shop floor.") legitimately stays — it's quoted attribution, not a feature-outcome promise. Note this also means F-BIZ-006 still applies to that fictional quote.

---

### F-BIZ-010 — Annual-billing discount mention added to FAQs (RESOLVED)

- **Where**: `pricing.html:165` FAQ + `index.html` FAQ + JSON-LD
- **Status**: resolved in `82d5458`
- **Verification (live)**:
  - CS pricing FAQ: `Měsíční: karta přes Stripe… Roční: na vyžádání — převodem s českou fakturou, splatnost 14 dní, zpravidla dva měsíce zdarma za rok.`
  - DE pricing FAQ: `… typischerweise zwei Monate kostenlos pro Jahr.`
  - EN pricing FAQ: `…typically two months free for the year.` (matches the trust-strip callout)
  - JSON-LD `acceptedAnswer.text` on `index.html:508` also includes `typically two months free for the year` — Google will pick up the discount in rich-result FAQ snippets too. Bonus SEO win.

---

### F-BIZ-011 — 175 fuzzy entries in the EN catalog (NEW, P2)

- **Where**: `app/locale/en/LC_MESSAGES/messages.po` (175 `#, fuzzy` markers; CS = 0, DE = 0)
- **Severity**: P2
- **Auto-fixable**: yes (manually clear fuzzy flags after reviewing each msgstr)
- **Description**: `pybabel update` mechanically marked unrelated msgstrs as fuzzy when extract picked up English-source strings. Examples currently sitting in the catalog with nonsense msgstrs:
  - `msgid "Message sent" msgstr "Manage tenants"` (contact.html:76)
  - `msgid "Write to us" msgstr "Switch to"` (contact.html:81)
  - `msgid "Submit" msgstr "Submitted"`, `msgid "Confirm" msgstr "Confirmed"`, `msgid "Cancel" msgstr "Cancelled"` (action verbs in `app/i18n_messages.py` and product/order forms)
  - `msgid "Order" msgstr "Orders"`, `msgid "Product" msgstr "In production"` (palette results)
  - `msgid "Verify email" msgstr "Verify your email"` (email template)
  - 168 more.
  Today gettext correctly skips fuzzy entries → users see the msgid (English source) → no visible regression. **But** the next time someone runs `pybabel update` and either (a) clears the fuzzy flag manually because they think the msgstr looks right, or (b) ships a tooling change that promotes fuzzies to active, prospects will see "Manage tenants" on the contact page success card. CLAUDE.md §7 already warns about i18n-extract foot-guns; this catalog is now sitting on a tripwire.
- **Suggested fix**: Walk the EN .po with `msgattrib --no-fuzzy` to dump the active set, then for each currently-fuzzy entry either (a) set `msgstr ""` (forces fallback to msgid, which is the desired behaviour for English) and remove the `#, fuzzy` flag, or (b) write the correct EN msgstr. Probably 30 minutes of mechanical work. Add a CI guard mirroring `tests/test_cs_catalog_health.py` (`71c75a4`) but for EN, e.g. `test_en_catalog_no_fuzzy_with_nonidentity_msgstr`. Consider whether the EN .po even needs to exist — `messages.pot` already provides the source strings; some projects skip a `locale/en/` directory and rely on gettext-not-found fallback.
- **Evidence**:
  - `grep -c "^#, fuzzy" app/locale/{en,cs,de}/LC_MESSAGES/messages.po` → `en:175`, `cs:0`, `de:0`
  - `app/locale/en/LC_MESSAGES/messages.po:3001-3017` (the contact-page block above)
  - Live curl confirms gettext skips fuzzies today: `<h2 …>Write to us</h2>` ships, not `Switch to`.

---

## What's still correct (verified, no finding)

- **Trial length**: `TRIAL_DAYS=30` in `app/platform/billing/service.py` matches all "30 days" / "30-day trial" copy across pricing.html, index.html, JSON-LD.
- **Cancel grace**: `CANCEL_GRACE_DAYS=3` in both `service.py` and `periodic.py` matches the "3 days to export your data" copy on pricing FAQ, index FAQ, terms.html, and the platform/billing/dashboard.html cancel modal.
- **Plan limits**: Migration `1003_billing.py:144-147` seeds Starter at 3 users / 20 contacts / 2048 MB, Pro at 15 / 100 / 20480 MB; pricing card copy still matches exactly.
- **Pricing numbers (live prod)**: `490` Kč Starter, `1 490` Kč Pro confirmed via `curl https://assoluto.eu/pricing`.
- **Annual billing on request**: pricing copy unchanged from 2026-04-25 audit fix.
- **Self-host AGPL pitch**: `LICENSE` is GNU Affero GPL v3, self-hosted page brands as AGPL-3.0, `FEATURE_PLATFORM=false` is the default in `.env.example`.
- **Trademark notice**: still ™, README still refers to common-law / unregistered with note that ÚPV filing is in progress.
- **Imprint**: Live `/imprint` correctly renders Václav Mudra / Lidická 2020/2 Děčín / IČO with COI ADR + EU ODR links.
- **Refund + past_due plumbing**: `charge.refunded` and `invoice.payment_failed` Stripe webhook handlers exist; only the marketing-copy gap (F-BIZ-008) remains.
- **Backup retention default**: `scripts/backup.sh` now defaults to 14 days; comment block explicitly references the marketing/Terms commitment + GDPR Art. 5(1)(e).


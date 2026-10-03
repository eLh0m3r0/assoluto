# UX audit — 2026-10-03-full

- **Auditor**: ux-auditor (Claude), read-only against production `015496f`
- **Live surfaces**: https://assoluto.eu (CS/EN/DE), https://4mex.assoluto.eu (login, reset, index)
- **Method**: Chrome MCP was **not available** (ToolSearch found no `claude-in-chrome` tools, the same as the last three runs). I used three fallbacks instead:
  1. `curl` with `Accept-Language: cs|en|de`, plus HTML and text extraction on 12 apex pages × 3 locales and 3 tenant pages.
  2. **Headless Chromium** (`chrome-headless-shell` from the local Playwright cache). It rendered real screenshots at 390 px and 1280 px, in light mode and in dark mode (`--blink-settings=preferredColorScheme=0`). Evidence is in [`ux-evidence/`](ux-evidence/).
  3. A **static template review** of `app/templates/**` for the logged-in experience. No credentials were provided, so this run made no authenticated walk.
- **No mutating request was sent.** I filled in no forms and submitted nothing.
- **Severity scale**: from `BRIEF.md` (P0 data/money/prod, P1 blocks sales/payments, P2 noticeably worsens the product, P3 cosmetic).

## Index

| ID | Sev | Status | Title |
|---|---|---|---|
| UX-01 | P1 | new | After email verification the main button goes to billing details and Stripe Checkout, which contradicts "30 days free, no card" |
| UX-02 | P2 | new | On a phone the homepage hero mock-up is broken: the content is squeezed into a 180 px column and half the table is cut off |
| UX-03 | P2 | new | The hero mock-up sells features the app does not have: a "Due" column, a "3 waiting on you" counter, an items summary |
| UX-04 | P2 | persisted (F-39) | Signup, login, verification and billing error messages are hard-coded in Czech, so EN/DE prospects get Czech errors |
| UX-05 | P2 | new | Rows in the order, product and asset lists can't be opened with a keyboard or middle-click (no `<a>`, only a JS `data-href`) |
| UX-06 | P2 | new | Deleting an order item or a drawing (attachment) takes one click with no confirmation |
| UX-07 | P2 | persisted (panel) | Bulk status change has no confirmation, yet it emails every contact on up to N orders |
| UX-08 | P2 | new | The orders list loses the "Responsible / Assigned to me" filter when you page |
| UX-09 | P2 | new | Dashboard "Recent activity" shows raw enums (`in_production → delivered`, `ORDER`, `attachment.upload`) |
| UX-10 | P2 | new | Inconsistent Czech terms: Objednávky vs. Nová zakázka, Klienti vs. zákazníci, Majetek vs. Sklad |
| UX-11 | P2 | persisted (F-30) | "Promised delivery" can't be filled in anywhere: the order detail always shows "—" and the SLA item in the main menu is always zero |
| UX-12 | P2 | persisted (panel) | No work queue: the orders list has no due date, no sorting and no "waiting on me", and the dashboard is just three counters |
| UX-13 | P2 | persisted (panel) | The client detail page has no orders for that client and no "New order for this client" button |
| UX-14 | P2 | new | `customer_contacts.last_login_at` is stored but never shown, so the supplier still can't tell whether a client ever logs in |
| UX-15 | P2 | new | The tenant landing page `4mex.assoluto.eu/` doesn't name the supplier and says "Zákaznický portál pro SME" |
| UX-16 | P2 | persisted (panel) | Support response times contradict each other: 48 h / 12 h on the homepage vs. 1 working day / same day on the pricing page |
| UX-17 | P2 | new | Static assets always carry `?v=0.1.0` and send no `Cache-Control`, so after a deploy browsers can serve stale CSS/JS for days |
| UX-18 | P2 | new | Inputs with no label (placeholder only) on the item form, the orders filter, contact invites, comments and the upload form |
| UX-19 | P2 | new | The homepage says "Nebo napište / zavolejte" (write or call us), but no phone number appears anywhere on the site |
| UX-20 | P2 | unverified | Social proof: "Nejoblíbenější" (most popular) plus anonymous testimonials during an early-access phase |
| UX-21 | P3 | new | Anglicisms in customer-facing Czech: "workspace", "SME", "Staff / admin role", "k jinému tenantovi" |
| UX-22 | P3 | new | The mobile menu has no theme toggle, and the main navigation has no active state (`aria-current`) |
| UX-23 | P3 | new | The status stepper sits below the items and attachments, and the current step has no `aria-current` |
| UX-24 | P3 | new | The Confirmed and Delivered badges share one colour |
| UX-25 | P3 | new | Czech typography: closing quotes `”` (should be `“`) and straight `"` in the testimonials |
| UX-26 | P3 | new | Homepage copy says "Osmikrokový proces" (8-step process) but lists 7 statuses; the features page lists 8 |
| UX-27 | P3 | new | The signup form doesn't show which plan you chose (`?plan=pro` survives only in a hidden field) |
| UX-28 | P3 | new | Small copy and grammar issues: "naše managed hosting", the notification-prefs help text, attachment size always in KB |
| UX-29 | P3 | persisted (F-UX-001, changed) | hreflang has been removed, but EN/DE still have no indexable URL, so Google only sees CS |

**Count**: P0 0 · P1 1 · P2 19 · P3 9 (29 total).

## Re-verification of previous UX findings

| Previous ID | Status | Evidence |
|---|---|---|
| F-UX-001 (hreflang pointing to one URL) | **resolved (variant)**: misleading hreflang removed. The underlying problem (EN/DE not indexable) persists, see UX-29 | `curl https://assoluto.eu/ \| grep rel=` → canonical only |
| F-UX-003 (different inputs in tenant vs. platform auth) | **resolved** | Both `email` inputs: `rounded-lg focus:ring-2 dark:bg-slate-800` |
| F-UX-A-001 (audit `None`) / F-UX-A-002 (`\uXXXX` in JSON) | **resolved** | `app/templating.py:196` `json.dumps(..., ensure_ascii=False)` |
| F-UX-A-003 ("staff", "tenantu", "grace") | **resolved for the reported strings**. Residual customer-facing anglicisms: UX-21 | `grep "staff uživatele\|grace období" cs/messages.po` → 0 |
| F-39 (Czech errors in Python) | **persisted** → UX-04 | `_t(request` in `signup.py` / `platform_auth.py` = 0 |
| Panel: bulk without confirmation, no work queue, client without orders, support SLA, "Pro → Starter trial" | Pro → Starter: resolved (FIXES.md). The rest **persisted** → UX-07, UX-12, UX-13, UX-16 | see the findings |

---

## Findings

### UX-01 — After email verification the main button goes to billing details and Stripe Checkout, which contradicts "30 days free, no card"
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: Pricing card CTA = `Spustit 30denní zkušební období` → `/platform/signup?plan=pro` (live, `_pricing_cs.html`). After verification, `app/templates/platform/verify_email.html:14-32` renders the **primary** button `{{ _("Finish setting up") }} {{ selected_plan|capitalize }}` → `POST /platform/billing/post-verify-checkout/{plan}`. `app/platform/routers/billing.py:573-579` redirects to `/platform/billing/details` (IČO, address) and then creates a Stripe Checkout session. The free trial sits behind a secondary grey "Skip for now" button. The pricing FAQ says "Kartu zadáte, teprve až se po 30 dnech rozhodnete pokračovat" (you enter a card only after 30 days, if you decide to continue). Not verified: how this behaves while `STRIPE_PRICE_*` is unset in prod. `create_checkout_session` → `BillingError` → `HTTPException(500, "Checkout failed: …")` (billing.py:600), i.e. a raw error at the moment of highest intent.
- Dopad: A Czech SME owner who picked Pro "with no card" gets a request for IČO and payment card right after verification. That breaks the main promise of the landing page and costs conversions at the bottom of the funnel. If Stripe isn't configured, they hit a 500.
- Návrh: For a trial, make the primary CTA "Continue to the portal" and show the plan only as "Your 30-day Pro trial is running until DD.MM." Move the "Set up payment now" step to the billing dashboard or the end-of-trial email. If checkout fails, show a friendly flash, not a 500.
- Effort: S
- Auto-fixable: yes

### UX-02 — On a phone the homepage hero mock-up is broken: the content is squeezed into a 180 px column and half the table is cut off
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/templates/www/_hero_mock.html:15` `grid grid-cols-[180px_1fr]` (no `sm:` prefix). The sidebar `<aside>` is `hidden sm:block`, so below 640 px the main content falls into the first column, which is 180 px wide. Screenshot [`ux-evidence/home-390.png`](ux-evidence/home-390.png) (390 px, headless Chromium) shows only the "#" and "Klient" columns visible, the "Stav" (status) and "Termín" (due) columns cut off, and the right half of the window empty. At 1280 px it's fine ([`home-1280.png`](ux-evidence/home-1280.png)).
- Dopad: A prospect arriving from Google on a phone sees a broken "screenshot" as the first image of the product. That's the one visual meant to explain the value in 5 seconds.
- Návrh: Use `grid-cols-1 sm:grid-cols-[180px_1fr]`. On mobile, also hide the "Položky" (items) column in the mock-up or show a card list.
- Effort: S
- Auto-fixable: yes

### UX-03 — The hero mock-up sells features the app does not have: a "Due" column, a "3 waiting on you" counter, an items summary
- Severity: P2
- Status vs 2026-07-26: new (follows the panel's "every screenshot is a drawing")
- Evidence: `_hero_mock.html:61,63` has the columns `Items` and `Due`. The header reads "12 aktivních · 3 čekají na vás" (12 active · 3 waiting on you), and there is a "Kovárna Vlček napsala k ORD-0142" (Kovárna Vlček wrote about ORD-0142) notification panel. The real `app/templates/orders/list.html:92-104` has the columns Number, Name, Client, Status, Responsible, Total, Created: no due date, no item count, no "waiting on you". The homepage still has no real screenshot or video.
- Dopad: The buyer sees one product during the trial and gets another. Trust drops exactly when they compare. The mock-up is also the only "screenshot" on the site.
- Návrh: Either add a requested-delivery column and a "waiting on you" counter to the list (see UX-12, the cheaper option), or redraw the mock-up from the real list. Add 2–3 real screenshots or a 60 s video of the order detail page with the stepper.
- Effort: M
- Auto-fixable: no (product decision: build it vs. change the mock-up)

### UX-04 — Signup, login, verification and billing error messages are hard-coded in Czech, so EN/DE prospects get Czech errors
- Severity: P2
- Status vs 2026-07-26: persisted (F-39)
- Evidence: `app/platform/routers/signup.py:162-163,202,214,324,335,349,363,381` (e.g. `"Tato subdoména je již obsazená."`, `"E-mail byl úspěšně ověřen."`). `_t(request` count in `signup.py` = 0 and in `platform_auth.py` = 0. The templates on these pages are translated (live EN title `Create portal · Assoluto`), so a Czech sentence appears inside an English page.
- Dopad: A German or English prospect who mistypes during signup doesn't understand the error. That's the top of the funnel for every non-CZ market.
- Návrh: Wrap the strings in `_t(request, "…")`, run pybabel extract/update/compile per CLAUDE.md §7, and extend `test_template_i18n_coverage.py` to scan `app/**/*.py`.
- Effort: M
- Auto-fixable: yes

### UX-05 — Rows in the order, product and asset lists can't be opened with a keyboard or middle-click
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/templates/orders/list.html:110`, `products/list.html:31` and `assets/list.html:35` render `<tr data-href=…>` with no `<a>` in the row (the order number is plain text). `app/static/js/app.js:59-70` listens only for `click`. A middle-click doesn't fire `click` (only `auxclick`), so the "middle-click opens a new tab" promise in the comment doesn't work. There's also no focus target for Tab or Enter. The client list does use a real `<a>` (`customers/list.html:18`).
- Dopad: Office staff can't open several orders in tabs (an everyday workflow). Keyboard and screen-reader users can't reach an order detail from the list at all.
- Návrh: Make the order number (or title) a real `<a href>`, keep `data-href` as a convenience, and add an `auxclick` handler.
- Effort: S
- Auto-fixable: yes

### UX-06 — Deleting an order item or a drawing takes one click with no confirmation
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/templates/orders/_item_row.html:94-101` (Delete item) and `orders/detail.html:291-296` (Delete attachment) have no `data-confirm`, while customer/user deactivation and product deletion do (`customers/detail.html:134`, `products/detail.html:30`). The red "Smazat" (delete) link sits right next to the autosaving inputs.
- Dopad: A mis-click permanently removes a drawing revision or a priced line. That's exactly the "správná revize u správné zakázky" (right revision on the right order) promise the homepage sells.
- Návrh: Add `data-confirm="{{ _('Delete this item?') }}"` and `data-confirm="{{ _('Delete file %(name)s?') }}"`, or at least move delete into a ⋯ menu.
- Effort: S
- Auto-fixable: yes

### UX-07 — Bulk status change has no confirmation, yet it emails every contact on up to N orders
- Severity: P2
- Status vs 2026-07-26: persisted (product panel "[major] Bulk status change is the only transition path with no confirmation")
- Evidence: `app/templates/orders/list.html:78` `<form … action="/app/orders/bulk/transition" data-bulk-form>` has no `data-confirm`. The single-order stepper confirms skips and backward moves (`orders/detail.html:31,376,401`). The "Set status to…" dropdown also offers `Cancelled` (`app/routers/orders.py:185-195`).
- Dopad: Two clicks can cancel 20 orders and send the customers 20 emails. That's the most expensive operator mistake in the app, and the only one without a guard.
- Návrh: Add `data-confirm` with the count and target status ("Move 7 orders to Cancelled? Customers will be notified."). Ideally show the number of emails that will go out.
- Effort: S
- Auto-fixable: yes

### UX-08 — The orders list loses the "Responsible / Assigned to me" filter when you page
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/templates/orders/list.html:186` `_base_qs = "&q=" … "&status=" … "&customer=" …`. The `assigned` parameter, rendered by the filter at lines 49-58, is missing. Page 2 of "Assigned to me" therefore shows everyone's orders.
- Dopad: An operator going through their own queue suddenly sees other people's orders without noticing. The filter looks active in the form (it isn't preserved in the URL), and they work through the wrong list.
- Návrh: Add `~ "&assigned=" ~ filters.assigned`, or better, build the query string from `request.query_params` minus `page`.
- Effort: S
- Auto-fixable: yes

### UX-09 — Dashboard "Recent activity" shows raw enums and unmapped action codes
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/templates/dashboard/index.html:138` prints `{{ _b }} → {{ _a }}` straight from the audit diff (`in_production → delivered`, in font-mono). Line 150 shows `{{ event.entity_type }}` in uppercase (`ORDER`, `CUSTOMER`). The `activity_verbs` map is missing `order.assigned`, `attachment.upload`, `user.role_changed`, `user.disabled`, `user.reactivated`, `tenant.settings_updated`, `tenant.data_exported`, `user.profile_updated`, `contact.password_changed`. These fall back to the raw code (`activity_verbs.get(event.action, event.action)`). Activity rows don't link to the entity.
- Dopad: The first thing a staff member sees after logging in is developer jargon in a Czech UI. It looks unfinished.
- Návrh: Run the statuses through the same `label_map` as `_status_badge.html` (or include the badge), translate `entity_type`, add the missing verbs, and link to `/app/orders/{id}` etc.
- Effort: S
- Auto-fixable: yes

### UX-10 — Inconsistent Czech terms: Objednávky vs. Nová zakázka, Klienti vs. zákazníci, Majetek vs. Sklad
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/locale/cs/LC_MESSAGES/messages.po`: `Orders` → "Objednávky", `New order` → "Nová zakázka", `Order status` → "Stav objednávky". The catalogue has 80 msgstrs with "objednáv*" and 10 with "zakáz*". The marketing site says "Zakázky", "zákazníci" and "Materiál zákazníka na skladě" / "Sklad". The app says "Klienti" and `Assets` → "Majetek" (which reads like fixed assets in accounting). It's visible even in the hero mock-up: the heading "Objednávky" next to a "Nová zakázka" button.
- Dopad: For a job shop, "zakázka" (a production job) and "objednávka" (a purchase order) mean different things. Mixing them confuses both the owner and the client's purchasing clerk. "Majetek" doesn't match what the marketing sold ("customer material in your warehouse").
- Návrh: Pick one glossary (suggested: Zakázky / Zákazníci / Materiál zákazníka) and apply it across the CS catalogue and the marketing templates. Put it in CLAUDE.md as an i18n rule.
- Effort: M
- Auto-fixable: no (decision on the glossary)

### UX-11 — "Promised delivery" can't be filled in anywhere: the order detail always shows "—" and the SLA item in the main menu is always zero
- Severity: P2
- Status vs 2026-07-26: persisted (F-30 / panel blocker; the founder deferred it)
- Evidence: `grep -rn promised_delivery app/` → the only writes are reads in `orders.py:402` (CSV), `detail.html:155` (display), `pdf_service.py:253`, `sla_service.py`. No form, no route. `app_base.html` shows "SLA" in the **main** staff menu next to Orders. The features page promises "✓ Oceníte a stanovíte termín" (you price it and set the date).
- Dopad: The UX cost is separate from the business cost. Every staff member sees an always-empty SLA screen and a permanent "—" box on every order, which reads as a broken app.
- Návrh: If SLA is deferred, at least hide `/app/admin/sla` from the main menu and remove the "Promised delivery" card, until a date field exists in the order header or appears when moving to QUOTED. The proper fix is that date input.
- Effort: S (hide) / M (input)
- Auto-fixable: yes (hiding) / no (feature)

### UX-12 — No work queue: no due date, no sorting and no "waiting on me" in the list, and the dashboard is three counters
- Severity: P2
- Status vs 2026-07-26: persisted (panel "[major] There is no work queue")
- Evidence: `orders/list.html:92-104` (no delivery-date column, no column sorting, no date filter). The staff dashboard (`dashboard/index.html`) shows three counters, Recent activity and Recent orders, with no "Submitted, waiting for a quote", "Due this week" or "Assigned to me" section. The clerk on the customer side doesn't see "Quoted — waiting for your confirmation" either.
- Dopad: The owner still has to open orders one by one to find out what's urgent. The phone keeps ringing ("kde je má zakázka?", where's my order) because the customer doesn't see an action prompt.
- Návrh: (1) Add a "Requested delivery" column with sorting to the list. (2) Add a "Needs your action" block to the dashboard: staff get SUBMITTED + unassigned, contacts get QUOTED. (3) Add saved filter tabs.
- Effort: M
- Auto-fixable: no

### UX-13 — The client detail page has no orders for that client and no "New order for this client" button
- Severity: P2
- Status vs 2026-07-26: persisted (panel "[major] The client page shows no orders")
- Evidence: `app/templates/customers/detail.html` has the sections Notes, permissions, Contacts and an invite form, with no order list. `orders_new_form` (`app/routers/orders.py:427-447`) doesn't accept `?customer=` for prefilling.
- Dopad: The typical call ("what's the state of things for Kovárna Vlček?") takes 3+ clicks through the order filter instead of one page.
- Návrh: Add the last 10 orders with a link to `/app/orders?customer={id}` and a "New order" button with `?customer=` prefilled.
- Effort: S
- Auto-fixable: yes

### UX-14 — `customer_contacts.last_login_at` is stored but never shown
- Severity: P2
- Status vs 2026-07-26: new (the 07-26 fix added the column, but no UI)
- Evidence: `grep -rn last_login app/templates` → only `www/privacy.html:125`. `customers/detail.html:116-120` shows only the states active / invited / disabled.
- Dopad: FIXES.md calls this "the number that says whether a customer portal is working at all", but the supplier can't see it. They can't tell which client is actually using the portal and which one to call.
- Návrh: Add "Last login: 3 days ago / never" next to each contact, and optionally a "Last activity" column in the client list.
- Effort: S
- Auto-fixable: yes

### UX-15 — The tenant landing page `4mex.assoluto.eu/` doesn't name the supplier and says "Zákaznický portál pro SME"
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Live `curl https://4mex.assoluto.eu/` → title `Assoluto · Domů`, H1 "Zákaznický portál pro SME", 0 occurrences of "4MEX". Template `app/templates/index.html:14`. The login page does show "4MEX s.r.o.".
- Dopad: A client contact who types in the supplier's address lands on a generic Assoluto page that doesn't confirm they're in the right place. That feeds the "the invitation looks like phishing" problem the panel described. "SME" is an English acronym in Czech copy.
- Návrh: Show "{{ tenant.name }} — zákaznický portál" plus one sentence about what the client can do there, or simply redirect to `/auth/login`.
- Effort: S
- Auto-fixable: yes

### UX-16 — Support response times contradict each other between the homepage and the pricing page
- Severity: P2
- Status vs 2026-07-26: persisted (panel "[minor] Support response times contradict themselves")
- Evidence: Live CS/EN homepage: Starter "E-mailová podpora (48 h)" / "Email support (48 h)", Pro "(12 h)". `/pricing`: Starter "odpověď do 1 pracovního dne" (reply within 1 working day), Pro "tentýž pracovní den" (same working day). The homepage Starter tagline is "Pro malou dílnu", while pricing says "Malá dílna". The badge is "Nejoblíbenější" / "Most chosen" on the homepage but "Doporučujeme" / "Recommended" on pricing.
- Dopad: The buyer compares the two pages and finds a contradiction in a contractual promise (SLA). That undermines trust right at the purchase decision.
- Návrh: Put the plan cards in one shared partial for `index.html` and `pricing.html`, so the two pages can't drift apart.
- Effort: S
- Auto-fixable: yes

### UX-17 — Static assets always carry `?v=0.1.0` and send no `Cache-Control`, so browsers can serve stale CSS/JS after a deploy
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Live: `app.css?v=0.1.0`, `app.js?v=0.1.0` and `htmx.min.js?v=0.1.0` on every page. The response has `etag` and `last-modified: Wed, 19 Aug 2026` but **no** `Cache-Control`, so browsers fall back to heuristic freshness (≈10 % of the file's age). `app_version` = `__version__` = `pyproject.toml` `version = "0.1.0"`, which never changes. The comment in `app/templates/base.html:16-21` says "that happens on every new deploy", which isn't true.
- Dopad: After a deploy with new Tailwind classes, returning users can see a layout without styles for new elements, or old JS calling a changed endpoint, for hours or days. On top of that, assets are never cached long-term, so every visit pays for a revalidation round trip.
- Návrh: Version by git SHA or a content hash (e.g. `APP_BUILD_SHA` from CI), and serve `/static` with `Cache-Control: public, max-age=31536000, immutable`.
- Effort: S
- Auto-fixable: yes

### UX-18 — Inputs with no label (placeholder only)
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `orders/detail.html:205,221,223,228,234` (the add-item form: product, quantity, unit, price, description: no `<label>` and no `aria-label`). `:310,312` (file upload, item select). `:444` (comment textarea). `orders/list.html:28-58` (search, status, client, responsible). `customers/detail.html:161,163` (contact invite). The inline item row does have `aria-label` (`_item_row.html:53,63`), so the pattern is known.
- Dopad: Screen readers announce "edit text" with no name. Once a user types, the placeholder disappears and they lose context. In the 6-field item form, "Množství" (quantity) and "Cena/jedn." (unit price) are especially easy to mix up.
- Návrh: Add visible `<label>`s (or at least `aria-label`) to every form field. Add a test that fails when an `<input>` has no label and no `aria-label`.
- Effort: S
- Auto-fixable: yes

### UX-19 — The homepage says "Nebo napište / zavolejte" (write or call us), but no phone number appears anywhere on the site
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Live `/` CS line 853 "Nebo napište / zavolejte". `grep -c 'tel:\|+420'` = 0 on `/`, `/contact` and `/imprint`. The "15min demo" leads to a contact form with the instruction "Do zprávy napište 'ukázka'" (write 'ukázka' in the message): no calendar, no slot picker.
- Dopad: The target customer (a Czech job-shop owner, who per the landing page itself wants to "stop picking up the phone" but buys over the phone) can't find a number. The demo request takes an extra round trip by email.
- Návrh: Either add a phone number (imprint plus contact page) or change the CTA to "Napište nám". Make the demo a booking link (Cal.com / Google Calendar appointment slots).
- Effort: S
- Auto-fixable: no (the operator decides on a phone number / calendar)

### UX-20 — Social proof: "Nejoblíbenější" (most popular) plus anonymous testimonials during an early-access phase
- Severity: P2
- Status vs 2026-07-26: new; **unverified** whether the testimonials are real
- Evidence: Live `/` CS: Pro badge "Nejoblíbenější" / EN "Most chosen". The "Co říkají naši první zákazníci" (what our first customers say) section has 3 anonymous quotes ("Majitel zámečnické firmy — Morava, 12 zaměstnanců", …) next to the line "Assoluto je v early-access fázi. Loga zde přibydou…" (logos will be added here…).
- Dopad: A skeptical buyer reads "most chosen" with no customers and anonymous quotes as marketing fiction. If the quotes aren't from real customers, under Czech consumer-protection rules (Act 634/1992) that's a misleading commercial practice.
- Návrh: Change the badge to "Doporučujeme" (as on /pricing). Either back the testimonials up (first name, company with consent) or replace them with a concrete case or a founder's statement.
- Effort: S
- Auto-fixable: no (the founder must confirm where the quotes came from)

### UX-21 — Anglicisms in customer-facing Czech
- Severity: P3
- Status vs 2026-07-26: new (residual after F-UX-A-003)
- Evidence: CS PO: `Sign in to workspace` → "Přihlášení do workspace" (live 4mex login, [`login-dark-390.png`](ux-evidence/login-dark-390.png)). `Customer portal for SMEs` → "…pro SME". `Staff / admin role` → "Staff / admin role" (dashboard onboarding). `The invitation belongs to a different tenant.` → "Pozvánka patří k jinému tenantovi." (shown to the client contact). The platform-admin anglicisms (tenant, support) are operator-only and acceptable.
- Dopad: Small, but it reads as unpolished, especially on the client side.
- Návrh: "Přihlášení do portálu {{ name }}", "pro malé a střední firmy", "Role: člen týmu / administrátor", "Pozvánka patří k jinému portálu."
- Effort: S
- Auto-fixable: yes

### UX-22 — The mobile menu has no theme toggle, and the main navigation has no active state
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `app/templates/base.html:62` `#theme-toggle` lives only in `#site-nav` (`hidden md:flex`). The mobile drawer (`:105-125`) has only `self.nav()` and the language switcher. The `app_base.html` nav links all share the same classes, with no `aria-current="page"` and no highlight.
- Dopad: On a phone you can't switch theme. On desktop the user doesn't see which section they're in.
- Návrh: Add the toggle to the drawer, and add `aria-current` plus an active class based on `request.url.path`.
- Effort: S
- Auto-fixable: yes

### UX-23 — The status stepper sits below the items and attachments, and the current step has no `aria-current`
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `orders/detail.html:329-411`. The "Order status" section with the primary CTA (e.g. "Potvrdit" / Confirm for the client) comes after the items table and attachments. On an order with 15 lines, both the clerk and the operator have to scroll to the most important action. The current node (`state == "current"`) only differs visually, with no `aria-current="step"`.
- Dopad: More clicks and scrolling on the most frequent action, and accessibility suffers.
- Návrh: Move the stepper (or at least the primary CTA) under the order header and add `aria-current="step"`.
- Effort: S
- Auto-fixable: yes

### UX-24 — The Confirmed and Delivered badges share one colour
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `orders/_status_badge.html:6,9` both use `bg-emerald-100 text-emerald-800`.
- Dopad: When scanning the list, "to be produced" (Confirmed) looks the same as "done" (Delivered).
- Návrh: Give Confirmed e.g. violet or blue, and keep emerald for Delivered.
- Effort: S
- Auto-fixable: yes

### UX-25 — Czech typography: closing quotes and straight quotes
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: Live CS homepage: 8× `„…”` (U+201D) and 0× the correct `„…“` (U+201C). The testimonials use straight `"…"` (3 lines). The DE page mixes `„…“` (3) and `„…”` (5).
- Dopad: Cosmetic. A Czech reader notices it.
- Návrh: Replace `”` with `“` in CS/DE msgstrs and wrap the testimonials in `„…“`.
- Effort: S
- Auto-fixable: yes

### UX-26 — "Osmikrokový proces" (8-step process) next to a list of 7 statuses on the homepage
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: Live `/` CS: "Osmikrokový proces od poptávky k dodání" (8-step process from enquiry to delivery) and, in the next card, "ROZPRACOVÁNO · ODESLÁNO · OCENĚNO · POTVRZENO · VE VÝROBĚ · HOTOVO · DODÁNO" (7). `/features` lists 8 (including UZAVŘENO, closed). Meanwhile, the hero pricing says "Spustíte do 30 minut" (live in 30 minutes) and /features says "15 minut".
- Dopad: Small inconsistencies in the numbers.
- Návrh: Add UZAVŘENO to the homepage list, and unify 15 vs. 30 minutes.
- Effort: S
- Auto-fixable: yes

### UX-27 — The signup form doesn't show which plan you chose
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `curl /platform/signup?plan=pro` → `<input type="hidden" name="plan" value="pro">`, while the visible text says only "30denní zkušební verze zdarma." (30-day free trial). Screenshot [`signup-dark.png`](ux-evidence/signup-dark.png).
- Dopad: The user doesn't know whether they're starting Starter or Pro (the panel's earlier complaint about a Pro → Starter mix-up). There's no "change plan" link.
- Návrh: Add a chip like "Plán: Pro · 30 dní zdarma · změnit".
- Effort: S
- Auto-fixable: yes

### UX-28 — Small copy and grammar issues
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: (a) `/features` CS: "Nebo použijte naše managed hosting" → should be "náš spravovaný hosting". (b) `_notification_prefs.html` CS: "Odškrtnutím se daná notifikace vypne úplně — nic ji znovu nezapne." This sounds like it can't be undone. The intent is "we won't turn it back on automatically". (c) `orders/detail.html:279` always shows size in KB ("4812.3 KB" for a 4.7 MB STEP file). (d) The comment-author fallback and others are fine.
- Dopad: Cosmetic and minor confusion.
- Návrh: (a) fix the grammar. (b) "Vypnuté upozornění vám nepošleme za žádných okolností — dokud si ho sami nezapnete." (c) add a `filesizeformat` filter.
- Effort: S
- Auto-fixable: yes

### UX-29 — EN/DE have no indexable URL
- Severity: P3
- Status vs 2026-07-26: persisted (F-UX-001, changed: the misleading hreflang was removed)
- Evidence: `curl https://assoluto.eu/` without Accept-Language → `lang="cs"`. `?lang=de` doesn't switch the language. `/de` → 404. Switching works only through the `/set-lang` cookie. The sitemap lists only CS URLs.
- Dopad: The German market (DE translation is complete) is invisible to search engines.
- Návrh: Add `/en/`, `/de/` prefixes or `?lang=` with per-variant canonicals and hreflang.
- Effort: M
- Auto-fixable: no

---

## Walkthrough log

- **Setup**: ToolSearch "claude-in-chrome" → no tools. Fell back to curl plus local `chrome-headless-shell` (Playwright cache) for screenshots. The first attempt used desktop Chrome `--headless=new`, which clamps the window to about 500 px, so that screenshot is discarded and the 390 px shots come from headless-shell.
- **Health**: `/healthz` and `/readyz` → 200. `/robots.txt` and `/sitemap.xml` → 200 (sitemap has 9 URLs including `/imprint`). An apex 404 with `Accept: text/html` returns a branded HTML page (8.7 KB); without that header it returns JSON (fine).
- **Apex marketing CS/EN/DE** (`/`, `/pricing`, `/features`, `/self-hosted`, `/contact`, `/terms`, `/privacy`, `/cookies`): 24/24 → 200, TTFB 0.08–0.24 s. Page weight: homepage HTML 73 KB plus CSS 75 KB, htmx 51 KB and app.js 13 KB raw (gzip on); og:image 195 KB → 200. Translation check: no Czech-diacritic lines in EN/DE outside proper nouns, no identical CS=EN lines outside code and brand, no `%(var)s` leaks, no literal `&nbsp;`. Titles and meta descriptions are localised for all three locales. Canonical present; hreflang removed (→ UX-29). Content findings: UX-02, UX-03, UX-16, UX-19, UX-20, UX-25, UX-26, UX-28.
- **Mobile 390 px** (headless): `/` has no horizontal overflow on the page itself, hamburger works, but the hero mock-up is broken (UX-02). `/pricing` stacks the cards cleanly; Community is first, ahead of the paid plans (a panel finding, not filed again). `/features` and `/contact` render without overflow.
- **Dark mode** (headless, `preferredColorScheme=dark`): `4mex/auth/login` and `/platform/signup` show readable inputs, visible borders and legible placeholders. A static scan of every template for `bg-white` / `text-slate-900` / `border-*-300` without a `dark:` variant found only 2 hits, both deliberate white CTA buttons on a brand-coloured strip.
- **Console**: headless-shell with `--enable-logging` logged no JS or CSP errors on `/`, `/pricing`, `/features`, `/contact`, `/platform/signup` or `4mex/auth/login`. These are only GPU warnings, so this is partial evidence, not a DevTools capture.
- **Auth surfaces**: `4mex/auth/login` (CS/EN) → 200, `autocomplete=username/current-password` OK. `4mex/auth/password-reset` → 200, the copy doesn't reveal whether an account exists. `4mex/` index → UX-15. Tenant and platform input styles are now the same (F-UX-003 resolved).
- **Platform**: `/platform/login`, `/platform/signup`, `/platform/check-email` and `/platform/password-reset` → 200 in all three locales. Signup: the honeypot `website` has `tabindex=-1 autocomplete=off` inside an `aria-hidden` wrapper and isn't reachable by Tab. Fields carry `autocomplete` organization/name/username/new-password, `minlength=8`, `required`, and an `sr-only` "(povinné)" (required) on the company name. Tab order follows the DOM (company → subdomain → name → email → password → terms → submit). `?plan=pro` is passed through as hidden → UX-27. **Not submitted.** Post-signup flow reviewed statically → UX-01.
- **Test tenants**: `test-a`, `test-b` and `testfirma` → 404 (gone, as in the previous run). Only `4mex` exists.
- **Authenticated tenant (`/app/*`)**: **no authenticated walk, credentials not provided.** Static review instead: `dashboard/index.html` (UX-09, UX-12), `orders/list.html` (UX-05, UX-07, UX-08, UX-18), `orders/detail.html` + `_item_row.html` (UX-06, UX-11, UX-18, UX-23), `orders/form.html` (labels OK, `min=today` on the date), `customers/detail.html` (UX-13, UX-14), `_notification_prefs.html` (fieldset/legend OK, all checkboxes have labels; copy → UX-28), `me/profile.html` (labels OK, GDPR block present, deletion confirmed), `_status_badge.html` (UX-24), `base.html` / `app_base.html` (UX-17, UX-22), `_flash.html` (role=alert/status OK).
- **Platform admin**: no operator credentials. Static only: `platform/admin/tenants.html:22-24` has the Plan / Billing status / Period ends columns, `subscription_edit.html` has quick actions, and destructive actions (deactivate, revoke support) carry `data-confirm`. Not verified live.
- **Would have tested but couldn't**: an end-to-end authenticated walk (dashboard, order with the stepper, contact view, `/app/me/profile`), mobile 390 px of an app page, and live console/network inside `/app`. A Chrome-MCP or Playwright session with a test tenant needs re-seeding (`python -m scripts.create_tenant test-a …`), or credentials for `4mex`.
- **Time**: about 25 min.

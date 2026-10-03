# Business logic audit — 2026-10-03

Perspective 3 of the [BRIEF](BRIEF.md). Commit audited: `015496f` (`main` = `production`).

**Method.** For each core domain object I wrote down the invariants a supplier
or a customer would rely on (table below), then tried to break each one by
tracing services, routers, templates and scheduled jobs. Where the trace was
not conclusive I ran throwaway probes against the local Postgres through the
real HTTP stack (`tenant_client` + `_seed` from `tests/test_orders_item_autosave.py`;
probe files lived in the session scratchpad, nothing was added to the repo).
Probe results are quoted inline as **Reproduced**.

**Severity for `IDEA-` entries** = expected business value (P1 = would change
a buying decision / save hours per week; P3 = nice to have).

## Summary

| Severity | LOGIC | IDEA |
|---|---|---|
| P1 | 6 | 4 |
| P2 | 14 | 5 |
| P3 | 4 | 3 |

| id | sev | status | title |
|---|---|---|---|
| [LOGIC-1](#logic-1) | P1 | new | A customer contact can take down the supplier's order list with one `unit_price=NaN` POST |
| [LOGIC-2](#logic-2) | P1 | new | Quote acceptance is not bound to an amount — the customer can "confirm" a price they never saw |
| [LOGIC-3](#logic-3) | P1 | new | The supplier's SaaS billing state is shown to the supplier's *customers* (402 "Upgrade plan", overdue banner, 404 after cut) |
| [LOGIC-4](#logic-4) | P1 | new | Trial ends silently by default and the tenant is hard-cut 3 days later |
| [LOGIC-5](#logic-5) | P1 | new | Currency is hard-coded CZK end-to-end — a German tenant cannot quote in EUR |
| [LOGIC-6](#logic-6) | P1 | persisted (F-07) | Plan change still opens a second Stripe subscription; deletion webhook does not check which subscription died |
| [LOGIC-7](#logic-7) | P2 | new | Quotes can be sent and confirmed with unpriced or zero items; customers can submit empty orders |
| [LOGIC-8](#logic-8) | P2 | new | Plan caps on users and contacts are bypassed by disable → invite → reactivate |
| [LOGIC-9](#logic-9) | P2 | new | Last-admin guard counts support-access and never-accepted admins — a tenant can end up with no usable admin |
| [LOGIC-10](#logic-10) | P2 | new | The 14-day invite purge ignores "Resend invite" — a re-sent link dies days later and the contact silently vanishes |
| [LOGIC-11](#logic-11) | P2 | new | Customer activity feed reveals internal comments and staff assignment |
| [LOGIC-12](#logic-12) | P2 | new | Order header is immutable — no way to fix customer, title, requested date or set a promised date |
| [LOGIC-13](#logic-13) | P2 | new | Attachment deletion is unaudited and allowed after confirmation — the drawing the customer agreed to can disappear without trace |
| [LOGIC-14](#logic-14) | P2 | new | Customer-specific price does not take precedence over the shared SKU; picker silently truncates at 200 |
| [LOGIC-15](#logic-15) | P2 | new | Customer roles are cosmetic and a customer cannot be blocked / archived |
| [LOGIC-16](#logic-16) | P2 | new | Every timestamp in UI, PDF and filters is UTC; Europe/Prague appears nowhere |
| [LOGIC-17](#logic-17) | P2 | new | Order PDF / quote states no VAT basis and rounds half-even |
| [LOGIC-18](#logic-18) | P2 | persisted (F-38) | Manual subscription editor: still untested and crashable; "set active" never expires; no tenant reactivation |
| [LOGIC-19](#logic-19) | P2 | persisted (F-30, F-32) | SLA dashboard still structurally dead and still counts cancelled orders |
| [LOGIC-20](#logic-20) | P2 | new | Support-access user counts against the tenant's paid user cap and receives tenant mail |
| [LOGIC-21](#logic-21) | P3 | persisted (F-31, by decision) | Backfill still fabricates `delivered_at` on DRAFT → CLOSED jumps |
| [LOGIC-22](#logic-22) | P3 | new | Every staff move — including corrections and "back to Draft" — emails the customer, with no reason field |
| [LOGIC-23](#logic-23) | P3 | new | An owner of two tenants can only ever bill the first one |
| [LOGIC-24](#logic-24) | P3 | new | Customer-owned material: movements accept another customer's order, ADJUST can go negative, nothing is audited |
| [IDEA-1](#idea-1) | P1 | new | "Needs action" queues on the staff dashboard |
| [IDEA-2](#idea-2) | P1 | new | Quote follow-up: reminder + expiry for unconfirmed quotes |
| [IDEA-3](#idea-3) | P1 | new | Reorder / duplicate a previous order |
| [IDEA-4](#idea-4) | P1 | new | Promise date on confirmation + overdue alerts (also revives the SLA report) |
| [IDEA-5](#idea-5) | P2 | new | Quote email that carries the total, the PDF and a one-click confirm |
| [IDEA-6](#idea-6) | P2 | new | Price memory: suggest last price per customer × product when quoting |
| [IDEA-7](#idea-7) | P2 | new | Paste / CSV import of line items (BOM) |
| [IDEA-8](#idea-8) | P2 | new | Drawing revisions: supersede, mark current, require re-confirmation |
| [IDEA-9](#idea-9) | P2 | new | Customer admins invite their own colleagues |
| [IDEA-10](#idea-10) | P3 | new | Weekly open-orders statement to each customer |
| [IDEA-11](#idea-11) | P3 | new | Auto-consume customer material when the order enters production |
| [IDEA-12](#idea-12) | P3 | new | Lead-time estimate from history |

Prior items re-verified and **resolved**: F-13 (unpriced jump no longer stamps
0.00 — `_recompute_quoted_total` keeps NULL, `order_service.py:626-642`) and
F-14 (`add_item` now refreshes the cache, `order_service.py:465`). Both are
pinned by tests in `tests/test_audit_2026_07_26_orders.py`.

## Invariants checked

| Domain | Invariant a real user relies on | Holds? |
|---|---|---|
| Order / quote | What the customer confirms is a specific, complete price | **no** — LOGIC-2, LOGIC-7 |
| Order / quote | Money fields are finite numbers; one bad row cannot break a page | **no** — LOGIC-1 |
| Order / quote | Totals carry the right currency and VAT basis | **no** — LOGIC-5, LOGIC-17 |
| Order | Header data (customer, dates) can be corrected; promised date is captured | **no** — LOGIC-12, LOGIC-19 |
| Order | Status history and transitions are serialised; double-click = one move | yes (F-33 fix, `order_service.py:721`) |
| Order | Milestone stamps are truthful | partly — LOGIC-21 |
| Customer | Supplier controls who can order, confirm, and when a client is blocked | **no** — LOGIC-15 |
| Customer | Internal staff information never reaches a contact | **no** — LOGIC-11 |
| Customer | An invited contact stays invited until they accept or staff remove them | **no** — LOGIC-10 |
| Catalog | Customer-specific price wins for that customer; other customers' prices stay private | partly — LOGIC-14 |
| Attachments | The drawing a customer confirmed stays retrievable, deletions are traceable | **no** — LOGIC-13 |
| Notifications | Consent absolute; nobody silenced by soft filters | yes (verified `_select`, `notification_service.py:166-209`) |
| Plans | Usage ≤ cap unless grandfathered by a downgrade | **no** — LOGIC-8, LOGIC-20 |
| Plans | A plan-limit wall is shown to the payer, not the payer's customer | **no** — LOGIC-3 |
| Subscription | One tenant = one Stripe subscription; cancellation ends billing | **no** — LOGIC-6 |
| Subscription | Trial end is announced before access is cut | **no** — LOGIC-4 |
| Subscription | Operator edits leave a coherent, expiring state | **no** — LOGIC-18 |
| Tenant | A tenant always has ≥ 1 usable admin who can reach billing | **no** — LOGIC-9, LOGIC-23 |

---

## Defects

### LOGIC-1 — A customer contact can take down the supplier's order list with one `unit_price=NaN` POST
- Severity: P1
- Status vs 2026-07-26: new
- Evidence:
  - `app/routers/orders.py:922-927` parses `unit_price` with bare `Decimal(...)`; `Decimal("NaN")`, `Decimal("Infinity")` and negatives all pass. The only guard for contacts is `can_set_prices` (`orders.py:912`).
  - `app/services/order_service.py:408-465` (`add_item`) has **no** sign or finiteness check — unlike `update_item`, which rejects negatives at `:566`. `_recalculate_line_total` (`:399-405`) multiplies NaN through; Postgres `numeric` accepts `'NaN'`, so `line_total` and the cached `quoted_total` become NaN.
  - `app/templating.py:96` — `money_major` does `int(q)` on the NaN → `ValueError`. `orders/list.html:141` and `orders/detail.html:143` both render it.
  - Same parse in `app/routers/products.py:104-109` for `default_price` (staff-only), which then propagates into every order line using the product.
  - **Reproduced:** contact `jan@acme.cz` (customer with legacy `{}` permissions ⇒ `can_set_prices=True`) POSTs `unit_price=nan` → 303; staff `GET /app/orders` → `ValueError: cannot convert NaN to integer` (500). `/app` and the CSV stay up. A staff `unit_price=-500` line is accepted and the order total becomes `-500.00`. `quantity=Infinity` → unhandled `decimal.InvalidOperation` (500).
- Dopad: the supplier's main working screen 500s for every staff member as long as the poisoned order is on page 1; the order detail page 500s too, so the item cannot be removed through the UI and there is no order delete. Any customer whose permissions were created before the form defaults changed (empty `{}` = all allowed, `customer_permissions.py:19-33`) can do this. Negative lines also let a contact with price rights produce a negative order total; `update_item` then refuses to edit the line it allowed to be created.
- Návrh: one `_parse_money()` helper used by add/patch/product routes: reject non-finite (`d.is_finite()`), cap magnitude to the column precision, decide explicitly whether negative "discount" lines are allowed (if yes, allow them in `update_item` too; if no, reject in `add_item`). Make `money_major` return "—" on non-finite input as defence in depth. One-off data check: `SELECT … WHERE unit_price = 'NaN' OR line_total = 'NaN'`.
- Effort: S
- Auto-fixable: yes (the negative-line policy needs a one-word founder decision; default to "reject" if none)

### LOGIC-2 — Quote acceptance is not bound to an amount — the customer can "confirm" a price they never saw
- Severity: P1
- Status vs 2026-07-26: new
- Evidence:
  - Staff may edit items while the order is QUOTED (`order_service.py:605-607`, `STAFF_ITEM_EDIT_STATES`); every edit re-sums `quoted_total` (`:579`). No notification event exists for "quote changed".
  - The contact's confirm is `POST /app/orders/{id}/transitions/confirmed` with no body (`orders.py:1318-1344`); `transition_order` (`order_service.py:697-793`) records `from/to` only — neither the history row (`OrderStatusHistory`, `models/order.py:144-165`) nor the audit `after` payload stores the total that was accepted.
  - Staff can also move CONFIRMED → QUOTED → re-price → CONFIRMED themselves (`STAFF_ALLOWED_TRANSITIONS`, `order_service.py:135-137`); the customer receives two status emails without amounts (`email/templates/order_status_changed.txt`).
  - The detail card labels the live sum "Quoted (confirmed)" with tooltip "Confirmed total price filled in by staff on transition to Quoted" (`orders/detail.html:139-144`), but since F-14's fix it is recomputed on every `add_item` in any editable state, including the customer's own DRAFT.
- Dopad: customer opens the quote (100 000 Kč), staff corrects a line to 120 000 Kč a minute later, customer clicks Confirm on the stale page → the order is CONFIRMED at 120 000 Kč and items lock. In a dispute neither side can prove what was agreed: the only evidence is a stream of `order.item_updated` audit rows. For a B2B quote portal this is the core commercial record.
- Návrh: (1) snapshot on confirmation — add `confirmed_total` + `confirmed_at` (+ `confirmed_by_*`) to `orders`, write them in `transition_order` when landing on CONFIRMED, and put the amount in the history note / audit `after`; (2) optimistic check — the confirm form posts the `quoted_total` (or an items hash) it rendered; reject with "the quote changed, please review" if it differs; (3) relabel the card "Current total" before QUOTED and show the snapshot after.
- Effort: M (one migration)
- Auto-fixable: yes

### LOGIC-3 — The supplier's SaaS billing state is shown to the supplier's *customers* (402 "Upgrade plan", overdue banner, 404 after cut)
- Severity: P1
- Status vs 2026-07-26: new
- Evidence:
  - `attachment_service.py:114-117` calls `ensure_within_limit(... "storage_mb")` for **every** uploader, contacts included (`routers/attachments.py:119-126` catches only `AttachmentTooLarge` / `AttachmentError`). `PlanLimitExceeded` falls through to the global handler `app/main.py:498-535`, which renders `errors/plan_limit.html`: "Your current plan allows {limit} MB of storage … Upgrade your plan" with an **Upgrade plan** button to `/platform/billing`. The handler only logs (`main.py:500-506`); the supplier is never told a customer's drawing bounced.
  - Same for `metric="orders"` (`order_service.py:363-367`) the moment any plan gets an orders cap again (today NULL per migration `1006`), and for any tenant that downgrades below current storage (no pre-downgrade warning on `platform/billing/dashboard.html:158-171`).
  - `app_base.html:9-38`: the past-due banner ("Your most recent invoice failed … check your payment method") and the canceled banner ("Export now or contact us within 30 days") render for **any** principal; the code comment says this is deliberate ("so a contact can prod the admin").
  - After the grace window `enforce_canceled_subscriptions` (`tasks/periodic.py:266-354`) sets `tenants.is_active=false`; `deps.py:122-123` then returns a bare 404 "Tenant not found" to contacts too — their order history and drawings vanish without explanation.
- Dopad: the supplier is embarrassed in front of the very clients the portal is meant to impress ("your supplier can't pay their software bill"), and — worse — a customer's revised drawing is rejected while nobody at the supplier learns about it. That is exactly the "scrap metal" failure the notification redesign was built to prevent.
- Návrh: when the principal is a contact, never block on the supplier's plan for storage/orders — accept and flag (`over_limit` soft state + email to tenant admins), or block with a neutral "upload failed, the supplier has been notified" page and actually notify them. Restrict both billing banners to staff (admins get the CTA, operators a neutral line). For a deactivated tenant serve a branded "This portal is temporarily unavailable — contact {tenant name}" page instead of 404.
- Effort: M
- Auto-fixable: yes (the "accept over cap for contacts" choice needs a founder nod; the banner/404 parts do not)

### LOGIC-4 — Trial ends silently by default and the tenant is hard-cut 3 days later
- Severity: P1
- Status vs 2026-07-26: new
- Evidence:
  - The only pre-expiry warning is `send_trial_nurture_emails`, gated on `TRIAL_NURTURE_ENABLED` which defaults to `False` (`app/config.py:86`, `.env.example:81`, `tasks/periodic.py:418-420`). Whether production has it on is **unverified** (operator env).
  - Stripe's `customer.subscription.trial_will_end` handler is a log-only placeholder (`billing/webhooks.py:396-407`).
  - Inside the tenant app nothing shows a trial: `_stash_subscription_status` feeds only the `past_due` / `canceled` banners (`app_base.html:14-38`); "Trial until" exists only on the apex billing page (`platform/billing/dashboard.html:54-55`), which staff rarely visit.
  - `expire_demo_trials` flips `trialing → canceled` (`periodic.py:242-251`); the canceled banner gives no date; `enforce_canceled_subscriptions` deactivates the tenant at `current_period_end + 3 d` (`periodic.py:303-323`).
- Dopad: a trial user's first sign that the trial ended is a red banner, and three days later the portal (and all their customers' access) is gone. This is the highest-leverage conversion moment in the funnel and the product says nothing. Lost revenue, plus the LOGIC-3 embarrassment.
- Návrh: (1) in-app countdown for staff while `status='trialing'` (≤ 7 days: amber, admin CTA to billing); (2) wire `trial_will_end` and the `ending` nurture stage independently of the copy-approval flag (the `trial_ending` template already exists), or flip the flag in prod once copy is approved; (3) put the hard-cut date in the canceled banner.
- Effort: S
- Auto-fixable: yes (enabling the flag in prod = operator)

### LOGIC-5 — Currency is hard-coded CZK end-to-end — a German tenant cannot quote in EUR
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: `Order.currency` defaults to `"CZK"` (`models/order.py:95-97`) and nothing ever writes it — `create_order` has no currency parameter (`order_service.py:334-396`), there is no order edit route. `Product.currency` defaults to CZK and the product forms have no currency field (`routers/products.py:95-100`, `:270-275`; `product_service.py:67, 97`). No tenant-level default currency exists. Adding a product priced in another currency copies its number into a CZK order without conversion (`orders.py:965-970`). PDF, CSV and list all print the order currency (`pdf_service.py:336-370`, `orders.py:404`).
- Dopad: the DE locale is shipped and DE SMEs are a named target market, but every German tenant's quotes, PDFs and exports read "Kč". It also blocks any CZ supplier exporting to DE/AT customers in EUR — common for Czech job shops.
- Návrh: `tenants.settings["default_currency"]` (CZK/EUR) chosen at signup and in Portal settings; `Customer.currency` override; stamp `order.currency` at creation from customer → tenant; constrain catalog lines to the order currency (or block mismatches). No FX needed for v1.
- Effort: M
- Auto-fixable: no (product decision: per-tenant vs per-customer currency)

### LOGIC-6 — Plan change still opens a second Stripe subscription; deletion webhook does not check which subscription died
- Severity: P1
- Status vs 2026-07-26: persisted (F-07)
- Evidence: `platform/routers/billing.py:255-280` still calls `create_checkout_session` for any plan with no check of `current_sub.stripe_subscription_id`; `billing/service.py:364-368` always `mode="subscription"`. The dashboard still renders an Upgrade/Downgrade checkout form per plan (`platform/billing/dashboard.html:158-171`). New aggravating detail: `handle_subscription_upserted` overwrites the single local `stripe_subscription_id` with whichever subscription fired last (`webhooks.py:261`), and `handle_subscription_deleted` (`webhooks.py:309-337`) marks the tenant `canceled` **without comparing the incoming subscription id** — so once two subscriptions exist, cancelling/expiring the orphan schedules a hard-cut of a tenant who is still paying for the other one.
- Dopad: double billing on the first plan change after Stripe goes live; and the reverse failure (paid tenant cut off) via the deletion handler.
- Návrh: as in F-07 — when a live Stripe subscription exists, route plan changes through `stripe.Subscription.modify(items=[…], proration_behavior="create_prorations")` or the Billing Portal; reserve Checkout for tenants without one. In `handle_subscription_deleted` ignore events whose `data.id != subscription.stripe_subscription_id`.
- Effort: M
- Auto-fixable: no (needs a Stripe test account to verify proration)

### LOGIC-7 — Quotes can be sent and confirmed with unpriced or zero items; customers can submit empty orders
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `transition_order` (`order_service.py:697-761`) has no precondition for any target. Landing on QUOTED just recomputes (`:749-751`). `_recompute_quoted_total` sums only priced lines (`:636-641`), so a partially priced order shows a partial total as if complete; the PDF prints that as "Subtotal" (`pdf_service.py:362-371`). **Reproduced:** staff adds one unpriced line, moves the order to QUOTED → 303; contact confirms → 303, order is `confirmed` with `quoted_total = None`, items now locked for everyone (`_ensure_item_editable`, `:610-623`). Contact creates an order with no items and submits it → 303.
- Dopad: a customer can commit to a job with no price at all, and the supplier must walk the order backwards (which emails the customer, LOGIC-22) to fix it. Empty submissions land in the supplier's queue as noise and fire an "order submitted" mail.
- Návrh: guard in `transition_order`: SUBMITTED requires ≥ 1 item; QUOTED / CONFIRMED require ≥ 1 item and every line priced (staff may override with an explicit "send anyway" confirm, recorded in the note). Contact-side confirm should always enforce it.
- Effort: S
- Auto-fixable: yes

### LOGIC-8 — Plan caps on users and contacts are bypassed by disable → invite → reactivate
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: caps are checked only at invite time (`auth_service.py:304-306` contacts, `:404-406` users). Usage counts `is_active` rows (`platform/usage.py:70-76`). Reactivation sets `is_active=True` with no check: staff `routers/tenant_admin.py:266-277`, contacts `routers/customers.py:424-436`. `ensure_within_limit` also takes no lock (`usage.py:112-152`), so two concurrent invites at cap-1 both pass. Reactivation also accepts GDPR-erased rows (no `_gdpr_erased_at` check), which then count toward the cap and receive notification mail at `erased-…@erased.invalid` (`gdpr_service.py:200-231`, `notification_service.py:238`).
- Dopad: Starter (3 users / 20 contacts, `migrations/versions/1003_billing.py:145`) can be stretched indefinitely: invite 20, disable 20, invite 20, reactivate 20. Direct revenue leak once billing is live.
- Návrh: call `ensure_within_limit(..., delta=1)` in both reactivate routes (flash the friendly limit message rather than 402); refuse to reactivate erased rows; take `pg_advisory_xact_lock(hash(tenant_id))` inside `ensure_within_limit`.
- Effort: S
- Auto-fixable: yes

### LOGIC-9 — Last-admin guard counts support-access and never-accepted admins — a tenant can end up with no usable admin
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `_other_active_admins_exist` (`tenant_admin.py:59-71`) and the self-erase guard (`tenant_admin.py:760-784`) count any `role=TENANT_ADMIN, is_active=True` row. The support grant creates exactly such a row with `password_hash=None` (`platform/service.py:463-470`); revoking it flips `is_active=False` (`:550`). An invited-but-never-accepted admin also has `password_hash=None`.
- Dopad: while an operator holds support access (or while a co-admin invite is pending), the real owner can demote, disable or GDPR-erase themselves; once support is revoked or the invite lapses the tenant has no admin. Billing resolution requires a tenant-admin membership (`platform/routers/billing.py:61-100`), so nobody can pay or cancel, and the tenant drifts into the LOGIC-4 hard cut.
- Návrh: count only admins who can actually log in: `password_hash IS NOT NULL` and not a support user (join `platform_tenant_memberships.access_type != 'support'`, or add `users.is_support` to stay inside core per CLAUDE.md §6).
- Effort: S
- Auto-fixable: yes

### LOGIC-10 — The 14-day invite purge ignores "Resend invite" — a re-sent link dies days later and the contact silently vanishes
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `cleanup_stale_invited_contacts` hard-DELETEs contacts with `invited_at <= now-14d AND accepted_at IS NULL` (`tasks/periodic.py:130-169`). `invited_at` is written only at creation (`auth_service.py:315`); `customers_contact_resend_invite` (`routers/customers.py:443-513`) mints a fresh 7-day token (`routers/public.py:53`) but never touches `invited_at`. No audit row, no notice to the supplier.
- Dopad: staff resend on day 13 → customer clicks on day 15 → "invalid invitation", and the contact (name, phone, role, locale) has disappeared from the customer card. The supplier re-types it and does not know why. Customers on holiday for two weeks are routine in CZ/DE summers.
- Návrh: set `contact.invited_at = now` on resend; instead of DELETE, mark expired (keep the row, show "Invitation expired — resend") or at least audit the purge per tenant.
- Effort: S
- Auto-fixable: yes

### LOGIC-11 — Customer activity feed reveals internal comments and staff assignment
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `_apply_principal_scope` (`services/audit_service.py:213-236`) gives contacts every `entity_type='order'` event for their orders — including `order.comment_added` with `is_internal=true` (written by `order_service.py:885-898`) and `order.assigned` (`:949-959`). `dashboard/index.html:120-132` renders actor name + verb + order number (unknown actions fall back to the raw key). The code elsewhere is explicit that contacts must not see "the supplier's internal staff list, let alone who is working on what" (`routers/orders.py:672-673`, `:1481-1482`). **Reproduced:** after staff posts an internal comment and self-assigns, the contact's dashboard shows "Staff User order.assigned 2026-000001" and "Staff User okomentoval/a 2026-000001". The comment body is not shown.
- Dopad: the customer learns that internal discussion about their order happened and who owns it, and sees a "comment" they cannot find on the order — erodes trust and contradicts the stated rule.
- Návrh: for contacts, exclude `order.assigned`, item-level events if desired, and any `order.comment_added` whose `diff.after.is_internal` is true (JSONB predicate in `_apply_principal_scope`). Add a test next to `tests/test_activity_feed.py`.
- Effort: S
- Auto-fixable: yes

### LOGIC-12 — Order header is immutable — no way to fix customer, title, requested date or set a promised date
- Severity: P2
- Status vs 2026-07-26: new (root cause of F-30)
- Evidence: the only writers of `customer_id`, `title`, `notes`, `requested_delivery_at` are `create_order` (`order_service.py:371-381`); grep finds no other assignment in `app/routers` or `app/services`. There is no order delete route. `requested_delivery_at` is not validated against today (`orders.py:483-486`). Staff creating an order fires `ORDER_CREATED` to the chosen customer's contacts immediately (`orders.py:515-540`).
- Dopad: a mis-picked customer cannot be corrected — the order can only be cancelled, the other customer has already been emailed the title, and the number is burned. Customer asks "can we move delivery to next week?" — nowhere to record it. The promised-date field the SLA report depends on has no input (LOGIC-19).
- Návrh: staff "Edit order" form (title, notes, requested date, promised date; customer only while DRAFT/SUBMITTED and with a confirm), audited via `diff_from_models`; contacts may edit title/notes/requested date in DRAFT.
- Effort: M
- Auto-fixable: yes

### LOGIC-13 — Attachment deletion is unaudited and allowed after confirmation — the drawing the customer agreed to can disappear without trace
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `routers/attachments.py:220-252` — staff may delete any attachment in any status; contacts may delete **any** attachment (including supplier-uploaded ones) while DRAFT (`:238-240`). Neither path writes an audit event (upload does, `attachment_service.py:140-155`); the S3 object is deleted best-effort (`:242-247`). Uploads are allowed in every status with no revision concept.
- Dopad: in a job shop the drawing *is* the specification. After a dispute ("you made it to the old drawing") there is no record of which file existed at confirmation or who removed it.
- Návrh: audit `attachment.deleted` (filename, size, uploader, order status); after CONFIRMED forbid hard delete — soft-delete ("withdrawn") and keep the object; contacts may delete only their own uploads. Pairs with IDEA-8.
- Effort: S
- Auto-fixable: yes

### LOGIC-14 — Customer-specific price does not take precedence over the shared SKU; picker silently truncates at 200
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `create_product` allows the same SKU once shared and once per customer (`product_service.py:76-87`) — the intended "special price for this customer" pattern. `search_products` returns **both** rows for that customer, ordered by name, `limit=200` from the detail page (`product_service.py:34-57`, `routers/orders.py:711-714`). Nothing prefers the customer row. The staff add-item path is not customer-scoped at all (`orders.py:949-955`) and does not check `is_active`, so a deactivated product, or customer B's product with B's price, can land on A's order.
- Dopad: the contact (or a hurried staff member) picks the shared row and the order silently carries the list price instead of the negotiated one; once a catalog exceeds 200 items, products later in the alphabet are simply missing from the picker. Customer B's special price can be printed on A's PDF.
- Návrh: when a customer-scoped product exists for a SKU, hide the shared one for that customer; replace the 200-row dump with server-side search; scope the staff path to `customer_id IS NULL OR = order.customer_id` and `is_active`.
- Effort: S
- Auto-fixable: yes

### LOGIC-15 — Customer roles are cosmetic and a customer cannot be blocked / archived
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `CustomerContactRole.CUSTOMER_ADMIN` is documented as "can invite other contacts" (`models/enums.py:18`) but the only use of the role is a notification-scope default (`services/notification_prefs.py:118`); contacts can be invited only by staff (`routers/customers.py:282-300`). Any contact of any role can submit orders and confirm quotes of any amount (`order_service.py:103-113`). `Customer.is_active` exists (`models/customer.py:58-60`) but no route ever sets it to False, and `get_current_principal` / `create_order` never check it (`deps.py:292-300`, `order_service.py:352-361`).
- Dopad: the supplier cannot model "the buyer may confirm, the engineer may only upload drawings", cannot put a non-paying client on credit hold, and cannot archive a former client — every contact must be disabled one by one, and the customer still appears everywhere.
- Návrh: `Customer` archive / "orders blocked" toggle enforced in `create_order` and contact transitions (with a clear message to the contact); per-contact flags `can_confirm_quotes` (optionally with an amount limit) and `can_invite` honoured server-side. Delivers IDEA-9 on the same model.
- Effort: M
- Auto-fixable: no (product decision on role model)

### LOGIC-16 — Every timestamp in UI, PDF and filters is UTC; Europe/Prague appears nowhere
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: grep finds no `ZoneInfo`/`Europe/Prague` in `app/`. asyncpg returns `timestamptz` as UTC; templates `strftime` it directly: comments and status history (`orders/detail.html:432`, `:512`), PDF created/submitted (`pdf_service.py:164-173, 247-251`). `delivered_at = date.today()` (`order_service.py:755`) and backfill `now.date()` (`:682`) use the container's/UTC date. Order number year uses UTC (`order_service.py:305-307`); monthly order usage starts at UTC midnight (`platform/usage.py:79-83`); list/CSV date filters compare `created_at` to a bare date (`order_service.py:198-205`).
- Dopad: every time a Czech or German user reads is 1–2 h off; an order delivered at 00:30 local is stamped the previous day (SLA "on time" vs "late" flips); orders created 00:00–01:00 on 1 January are numbered with the previous year.
- Návrh: a `tenant.timezone` (default Europe/Prague), a `localdt` Jinja filter used everywhere a datetime is printed, and a `tenant_today()` helper for date stamps, numbering and filters.
- Effort: M
- Auto-fixable: yes

### LOGIC-17 — Order PDF / quote states no VAT basis and rounds half-even
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `pdf_service.py:362-371` prints one "Subtotal" line ("No tax field exists on the model"); neither the PDF nor the detail page says whether prices are with or without VAT. `_recalculate_line_total` uses `quantize(Decimal("0.01"))` with the default context rounding ROUND_HALF_EVEN (`order_service.py:399-405`): 0.5 × 0.25 = 0.125 → 0.12, whereas Czech/German commercial practice rounds half-up (0.13).
- Dopad: a B2B customer reading "Subtotal 120 000 Kč" cannot tell if 21 % / 19 % comes on top — a classic source of disputes; occasional one-cent differences against the customer's ERP.
- Návrh: tenant setting "prices are net / gross" + VAT rate, printed on PDF and detail ("bez DPH" / "zzgl. MwSt."); optional VAT line. Use `rounding=ROUND_HALF_UP` in line-total quantisation.
- Effort: S (label + rounding) / M (VAT line)
- Auto-fixable: yes for rounding and label; VAT line needs a decision

### LOGIC-18 — Manual subscription editor: still untested and crashable; "set active" never expires; no tenant reactivation
- Severity: P2
- Status vs 2026-07-26: persisted (F-38)
- Evidence: `platform_admin.py:535-536` still does `int(quick_action.split(":", 1)[1])` unguarded; grep of `tests/` for `subscription_edit` / `extend_trial` → no hits. Additionally: `set_active` (`:551-559`) sets `status="active"`, `trial_ends_at=None`, `current_period_end=now+30d`, but no job ever acts on an `active` subscription without a Stripe id (`expire_demo_trials` only touches `trialing/demo`, `enforce_canceled_subscriptions` only `canceled`, `periodic.py:247, 309`) — the tenant is free forever. None of the quick actions re-enable a tenant already hard-cut (`tenants.is_active` untouched), so "extend trial" on a lapsed tenant shows `trialing` while the portal still 404s; a separate Reactivate click is needed (`platform_admin.py:333-339`). Finally a manual tenant with `trial_ends_at=None` that later goes through Stripe Checkout receives a fresh 30-day Stripe trial (`billing/service.py:354-357`).
- Dopad: operator grants unintended free service; customer reports "you extended my trial but I still can't log in".
- Návrh: F-38's tests and try/except; make `set_active` require an explicit end date and let `expire_demo_trials` handle `active AND stripe_subscription_id IS NULL AND current_period_end < now`; quick actions that grant time also set `tenant.is_active=True`; pass `trial_period_days` only when the tenant never had a trial.
- Effort: S
- Auto-fixable: yes

### LOGIC-19 — SLA dashboard still structurally dead and still counts cancelled orders
- Severity: P2
- Status vs 2026-07-26: persisted (F-30, F-32)
- Evidence: grep for `promised_delivery_at\s*=` in `app/` → no hits (still no writer); `sla_service.py` has no status predicate; the nav still links it (`app_base.html:50`). Excluded from the July fix scope by the founder.
- Dopad: as before — a nav-level report that always reads 0 %. Now additionally blocks IDEA-4.
- Návrh: fixed together with LOGIC-12 (promised date input) and IDEA-4; add `status NOT IN ('cancelled')` and a status-aware delivered predicate to `sla_service`. Until then hide the nav entry.
- Effort: S (hide) / M (fix)
- Auto-fixable: yes (hide); fix needs LOGIC-12

### LOGIC-20 — Support-access user counts against the tenant's paid user cap and receives tenant mail
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: the support grant inserts an active `TENANT_ADMIN` `User` (`platform/service.py:463-470`). `snapshot_tenant_usage` counts it (`platform/usage.py:70`), so a Starter tenant with 3 staff shows 4/3 and cannot invite until the grant is revoked. Trial nurture mails go to every active tenant admin (`periodic.py:488-507`) — i.e. to the operator. Order notifications include it as a "pending" candidate (`notification_service.py:238`, `:293`), and the reachability filter yields to it whenever all real staff have opted out of an event or are the actor.
- Dopad: the operator's own support visit pushes the customer to "upgrade"; the operator receives the customer's mail (trial nurture, occasionally order excerpts) — contrary to CLAUDE.md §6 "platform admin is an operator, not a super-user".
- Návrh: exclude support users from usage, from `_eligible_staff`, from nurture recipients, and from the last-admin count (LOGIC-9) — one predicate shared by all four.
- Effort: S
- Auto-fixable: yes

### LOGIC-21 — Backfill still fabricates `delivered_at` on DRAFT → CLOSED jumps
- Severity: P3
- Status vs 2026-07-26: persisted (F-31, kept by decision)
- Evidence: `_backfill_milestones` (`order_service.py:673-694`) stamps `delivered_at = now.date()` for any landing at or past DELIVERED; the July decision to keep stamps on backward moves is documented at `:686-694` and pinned by `tests/test_orders_transition_delivered_at.py`. Reopening a cancelled order does not clear `cancelled_at` (`:758-759`), so a live order can carry a cancellation date.
- Dopad: latent until SLA works (LOGIC-19); then an order closed straight from DRAFT (e.g. "cancelled by phone, closed for bookkeeping") reports an on-time delivery.
- Návrh: keep the decision, but in `sla_service` count an order only if it *currently* sits at DELIVERED/CLOSED and its history actually contains a DELIVERED entry; clear `cancelled_at` on reopen.
- Effort: S
- Auto-fixable: yes

### LOGIC-22 — Every staff move — including corrections and "back to Draft" — emails the customer, with no reason field
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `orders_transition` builds `build_order_status_changed` for every target except SUBMITTED (`orders.py:1359-1379`), regardless of direction; the route accepts no `note` even though `transition_order` stores one (`order_service.py:703, 771`). The mail body is just "Order X — {status}" (`email/templates/order_status_changed.txt`).
- Dopad: a mis-click DELIVERED followed by the correction sends the customer "Delivered" then "In production"; a cancellation arrives with no reason.
- Návrh: optional note on the stepper (required for CANCELLED and backward moves), included in the mail; a "don't notify customer" checkbox for corrections; suppress mails for moves back to DRAFT.
- Effort: S
- Auto-fixable: yes

### LOGIC-23 — An owner of two tenants can only ever bill the first one
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `_resolve_current_tenant` returns the first admin membership by `created_at` (`platform/routers/billing.py:61-100`, `platform/service.py:244-260`); every billing route uses it and none accepts a tenant parameter.
- Dopad: a group with two companies (or an identity invited and promoted in a second tenant) cannot pay, cancel or see invoices for the second tenant, which then expires (LOGIC-4). Frequency unverified.
- Návrh: billing routes take `tenant_id` (validated against the identity's non-support admin memberships); `/platform/select-tenant` links to billing per tenant.
- Effort: S
- Auto-fixable: yes

### LOGIC-24 — Customer-owned material: movements accept another customer's order, ADJUST can go negative, nothing is audited
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `routers/assets.py:235-249` passes any `reference_order_id` (no check that the order belongs to `asset.customer_id`; a non-existent UUID hits the FK and 500s). `add_movement` checks negative stock only for ISSUE/CONSUME (`asset_service.py:139-144`). No `audit_service.record` anywhere in the asset path.
- Dopad: the supplier is liable for the customer's material; a consume booked against the wrong customer's order, or an unexplained adjustment, cannot be traced.
- Návrh: validate the order's customer; forbid ADJUST below zero (or require a note); audit every movement.
- Effort: S
- Auto-fixable: yes

---

## Automation / feature proposals

### IDEA-1 — "Needs action" queues on the staff dashboard
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: the dashboard shows one "open orders" count over six statuses, the last 5 orders and the audit feed (`routers/dashboard.py:27-98`). All data for a work queue already exists: `status`, `submitted_at`, status history timestamps, `assigned_to_user_id`, `requested_delivery_at`.
- Dopad: today the supplier scans the order list by eye every morning. Four queues answer "what do I do now": *Submitted, not yet quoted* (age since `submitted_at`), *Quoted, waiting for customer > N days*, *Confirmed/In production past requested date*, *Ready, not delivered*. Plus "unassigned" (filter exists, `order_service.py:209-213`).
- Návrh: four `COUNT … GROUP BY` queries + links to pre-filtered list views; optional daily 07:00 digest mail per staff member via the existing scheduler and `order_digest` template (`notification_service.py:705-760`).
- Effort: M
- Auto-fixable: yes

### IDEA-2 — Quote follow-up: reminder + expiry for unconfirmed quotes
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: QUOTED orders sit forever; the only notification is the one status mail (LOGIC-22). `order_status_history` gives the time the order entered QUOTED.
- Dopad: unanswered quotes are lost revenue the supplier currently chases by phone. An automatic reminder after 3 and 7 days (to the contacts who would receive the status mail — reuse `resolve_contact_audience`) and an optional validity date ("valid until") that warns staff when a quote lapses.
- Návrh: `orders.quote_valid_until` (default tenant setting, e.g. 14 days), periodic job modelled on `send_trial_nurture_emails` with idempotency markers, new `NotificationEvent.QUOTE_REMINDER` (one enum member + template triple per §19).
- Effort: M
- Auto-fixable: yes

### IDEA-3 — Reorder / duplicate a previous order
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: no duplicate/reorder path exists (grep `reorder|duplicate|clone` in `app/routers`, `app/services` → none). `OrderItem` keeps `product_id`, description, unit, quantity, price (`models/order.py:114-141`).
- Dopad: job-shop customers reorder the same parts constantly; today each line is retyped. "Order again" on a delivered/closed order (contact and staff) creates a DRAFT with the same lines (prices re-taken from the current catalog / left blank for re-quote) and re-links the drawings.
- Návrh: `order_service.duplicate_order(source, actor)` reusing `create_order` + `add_item`; attachments copied by reference (new rows, same S3 key, or server-side copy).
- Effort: M
- Auto-fixable: yes

### IDEA-4 — Promise date on confirmation + overdue alerts (also revives the SLA report)
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: `promised_delivery_at` exists and is read by PDF/CSV/SLA but never written (LOGIC-19); `requested_delivery_at` is captured from the customer and then ignored.
- Dopad: the supplier commits a date when quoting/confirming (defaulting to the requested date), the customer sees it, and the system raises "due in 2 days / overdue" to the assignee. Turns the dead SLA dashboard into a real on-time metric — a strong sales argument ("prove your delivery reliability to your customers").
- Návrh: promised-date input on the QUOTED/CONFIRMED stepper step and in the order edit form (LOGIC-12); daily job emailing assignees about orders due/overdue; `NotificationEvent.DELIVERY_DATE_CHANGED` to the customer when it moves.
- Effort: M
- Auto-fixable: yes

### IDEA-5 — Quote email that carries the total, the PDF and a one-click confirm
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: the QUOTED mail is "Order X — Quoted" + link (`email/templates/order_status_changed.txt`); the PDF renderer exists (`services/pdf_service.py`).
- Dopad: the customer must log in to learn the price; many will not. Including total, line summary, the PDF and a signed confirm link (bound to the quoted amount, see LOGIC-2) shortens time-to-yes.
- Návrh: dedicated `order_quoted` template; PDF attachment via existing sender; signed token carrying `order_id` + `quoted_total` + `session_version`-style single-use guard (§14 pattern).
- Effort: M
- Auto-fixable: yes

### IDEA-6 — Price memory: suggest last price per customer × product when quoting
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: every past `OrderItem` has `product_id`, `unit_price`, and through the order a `customer_id`; the add-item path only knows the catalog `default_price` (`orders.py:965-970`).
- Dopad: quoting is the supplier's most repetitive task; showing "last quoted to this customer: 12,40 Kč (2026-08-14)" next to the price input (and pre-filling it for staff) removes lookups in old PDFs/ERP.
- Návrh: one indexed query `ORDER BY created_at DESC LIMIT 1` per product on the order's customer, rendered in `_item_row.html`.
- Effort: S
- Auto-fixable: yes

### IDEA-7 — Paste / CSV import of line items (BOM)
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: items are added one form POST at a time (`orders.py:882-994`).
- Dopad: customers send 30–200-line part lists from CAD/ERP exports; pasting a table (SKU/description, qty, unit) into a textarea with a preview would replace minutes of clicking per order.
- Návrh: parse TSV/CSV (Excel paste), match SKUs against the scoped catalog, preview, then batch `add_item` in one transaction.
- Effort: M
- Auto-fixable: yes

### IDEA-8 — Drawing revisions: supersede, mark current, require re-confirmation
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: attachments are a flat list; uploads are allowed in every status with no revision link (`routers/attachments.py:58-171`); deletion issues in LOGIC-13.
- Dopad: "made to the old drawing" is the most expensive mistake a job shop makes. Uploading a file as "new revision of X" marks the old one superseded, highlights the change to production staff (the attachment notification already exists) and, if the order is past QUOTED, asks the supplier whether the quote needs re-confirmation.
- Návrh: `order_attachments.supersedes_id` + `revision` label; UI badge "current"; optional transition back to QUOTED.
- Effort: M
- Auto-fixable: yes

### IDEA-9 — Customer admins invite their own colleagues
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: role exists but does nothing (LOGIC-15); every contact is invited by supplier staff (`routers/customers.py:282-300`).
- Dopad: removes a recurring support chore from the supplier and increases portal adoption on the customer side; still bounded by the plan's contact cap.
- Návrh: `/app/me/team` for `CUSTOMER_ADMIN` reusing `invite_customer_contact`, audited, staff notified.
- Effort: S
- Auto-fixable: yes

### IDEA-10 — Weekly open-orders statement to each customer
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: data is in `orders` (status, promised/requested dates); digest template exists (`notification_service.py:705-760`).
- Dopad: replaces the supplier's Friday "status of your orders" email/phone round; opt-out via notification prefs (new event, consent rule applies).
- Návrh: Monday job, one mail per contact with scope `all`, listing non-closed orders and their dates.
- Effort: S
- Auto-fixable: yes

### IDEA-11 — Auto-consume customer material when the order enters production
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `AssetMovement.reference_order_id` exists (`models/asset.py:86-89`) but movements are entered by hand (`routers/assets.py:208-257`).
- Dopad: material planned per order line (e.g. "2 m of customer's steel bar") is booked automatically on IN_PRODUCTION / DELIVERED, keeping the customer-visible stock accurate without extra clicks.
- Návrh: optional `order_items.asset_id` + `asset_qty`; consume in `transition_order` side effects (and in `_backfill_milestones`, per §18).
- Effort: M
- Auto-fixable: yes

### IDEA-12 — Lead-time estimate from history
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `submitted_at`, `delivered_at` and status history per order/customer/product are already stored.
- Dopad: when quoting, show "typical CONFIRMED → DELIVERED for this customer/part: 9 working days (last 10 orders)" to set a realistic promise date (IDEA-4). Cheap "smart" differentiator.
- Návrh: median over the last N orders, computed on the fly; displayed next to the promised-date input.
- Effort: S
- Auto-fixable: yes

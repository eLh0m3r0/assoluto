# What was fixed — 2026-07-26 audit

Six commits on top of the audit docs. `534 tests pass` (18 new), ruff clean,
migration `1007` applied. Every behaviour change is covered by a test that
asserts the *attack or defect*, not the fix, so re-narrowing the code later
breaks something loud.

Scope per the founder: **fix everything except the SLA cluster** — payments
are not live yet, so the SLA dashboard is not worth the work — with priority
on business logic and security.

## Fixed

### Security — auth, session, authorization
| id | what |
|---|---|
| F-04 | `/platform/billing` resolved the caller's tenant from the first row of an **unordered** membership query and accepted the `TENANT_ADMIN` user a support grant creates — an operator with support access on a paying tenant could cancel their subscription. Support memberships excluded, query ordered, duplicate resolver deleted. |
| F-06 | `_has_unverified_identity` matched on email alone, so signing up at `/platform/signup` with a staff member's address locked that person out of their own tenant with their own correct password. Scoped to a membership on that tenant. |
| F-23 | Both login branches checked `is_active` / `accepted_at` **before** `verify_password`, turning the form into an account-existence oracle. Password first; state is disclosed only to whoever proved it. |
| F-05 | Platform password-reset tokens carried only `identity_id` — replayable for the full 30-minute TTL. Now carry `session_version`, rejected if stale, bumped on consume. |
| F-20 | Platform session cookie had no version, so a completed reset left every existing session signed in. Added, and checked on every request. |
| F-21 | Platform reset accepted a password of any length while the tenant flow enforces 8 characters in six places. |
| F-40 | Logout only deleted the cookie; an already-captured token stayed valid for 14 days. Bumps `session_version`. |
| F-15 | `APP_SECRET_KEY` defaulted to `dev-insecure-secret-change-me` with no runtime guard. The prod compose asserts it, but that protects one deploy path, not the app — self-hosters running uvicorn had nothing. Refuses to boot in production. |

### Security — input handling and supply chain
| id | what |
|---|---|
| F-02 | ReportLab `Paragraph` takes a mini-HTML dialect, so order text was markup. Both effects reproduced: `M8 <b>bolt` 500s the supplier's whole PDF export, and `<img src="http://169.254.169.254/…">` makes the renderer fetch it server-side. Escaped every interpolated variable in both PDF services; emptied `trustedSchemes`/`trustedHosts`. Re-verified after the fix. |
| F-18 | Order-item creation looked products up by id alone — RLS scopes that to the tenant but **not** the customer, so a contact of customer A could attach and price-read a product scoped to customer B. Scoped, and `can_use_catalog` is now enforced server-side (it was a UI hint only). |
| F-29 | The customer edit form rendered `can_set_prices` with a `False` default while enforcement defaults every missing key to `True` — staff read a lock off a `{}` row that was not engaged. |
| F-17 | Thumbnails fed attacker bytes to Pillow, whose stock ceiling only warns below 2× ~89M pixels. `MAX_IMAGE_PIXELS` capped. |
| F-19 | Every GitHub Action ran from a **mutable tag**, including `appleboy/ssh-action`, which holds the production VPS SSH key. All pinned to commit SHAs; least-privilege `permissions:` added to the three workflows that had none. |
| F-03 | `python-multipart` 0.0.26 → 0.0.32; floor raised. `verify_csrf` calls `request.form()` on every mutating route, so the parser is reachable pre-auth. |
| F-22 | `/platform/password-reset` sent SMTP **inline** in an async handler — blocking, single worker, unauthenticated: ~36s stall per request. Moved to BackgroundTasks, same for staff resend-invite. |

### Business logic
| id | what |
|---|---|
| — | **A customer confirming a quote told nobody at the supplier.** `build_order_status_changed` resolved recipients only from `_contact_recipients`, so the most commercially loaded click in the product emailed the customer's own colleagues — including the actor. Now routes by who acted, and never emails the actor their own click. |
| F-14 | `add_item` never refreshed the cached `quoted_total` though `remove_item`/`update_item` both do, and the PDF prefers that cache over its own item table — so a Subtotal contradicted the lines directly above it. |
| F-13 | An empty `SUM` coalesced to 0, so jumping an unpriced order past QUOTED stamped `0.00` and the customer PDF printed "0 Kč" — reads as free, not as not-yet-quoted. NULL is the honest value. |
| F-33 | Transitions took no row lock: a double-click wrote two history rows and sent two emails. `SELECT … FOR UPDATE`. |
| F-35 | `bulk_transition` never forwarded `audit_actor`, so every bulk change was logged as `system` — the one operation most needing an explanation could not answer who did it. |
| F-09 | Stripe guarantees delivery, not order: a stale `updated` delivered after `deleted` wrote `active` over `canceled`, reviving a subscription nobody pays for. `canceled` is now terminal for that subscription id. |
| F-08 | `enforce_canceled_subscriptions` hard-cut the tenant and nothing ever set `is_active` back, so a customer who paid again still could not log in. |
| — | Clicking "Start 30-day trial" on the **Pro** card started a **Starter** trial (3 users / 20 contacts), disclosed only in the Terms. The chosen plan now drives the trial. |

### Privacy and honesty of claims
| id | what |
|---|---|
| F-11 | Erasure wrote the subject's name and email into `audit_events`, which is append-only by design — so an Art. 17 erasure left the person permanently readable via `/app/admin/audit`. Records the pseudonymous entity id; scrubs `actor_label` on self-erasure. |
| F-42 | No `Cache-Control` anywhere: authenticated HTML was disk-cacheable and survived logout on a shared machine. |
| — | **Built the export the Terms already promised.** `terms.html:123` states the "CSV / ZIP export endpoints are available at any time"; nothing implemented it. `GET /app/admin/export` now streams a ZIP of every table plus the attachment bytes. |
| — | `privacy.html` did not name the object-storage provider holding every drawing and every DB backup — an Art. 28(2) gap demonstrable from public repo text. |
| — | **VAT position was inconsistent across copy, Stripe and the invoice.** `pricing.html` stated no position at all; the FAQ promised a "valid Czech tax invoice"; and Stripe Checkout hard-coded `automatic_tax` + `tax_id_collection` to `True`, which would have added 21 % on top of the listed price — money a neplátce is not registered to collect. All three now follow `PLATFORM_OPERATOR_DIC`, the same single switch the invoice PDF already used: empty = price is final, no DPH, "Faktura" not "Daňový doklad". Setting it later flips checkout, invoice layout and document label together. |
| F-01 | `backup.sh` used `rclone sync`, a destructive mirror: an emptied or unmounted `/backups` propagated the deletion off-site and took the dump history with it. Now `copy`. |
| — | Added `customer_contacts.last_login_at` (staff had it since 0002). Nobody could tell an active client from one who opened the invite once — the number that says whether a customer portal is working at all. |

## Deliberately not changed

**F-31 — backward transitions retaining milestone stamps.** Reported as a
defect; it is a decision, pinned by `tests/test_orders_transition_delivered_at.py`
with an explanatory comment. The delivery date records when goods actually
left; clearing it on a correction would lose that or re-stamp a wrong one. I
reverted my own change after reading the test.

**F-18's price backfill is not gated on `can_set_prices`.** Applying the
supplier's own list price is not the contact choosing a number. My first
attempt over-reached and broke the e2e happy path, which is how it was caught.

**F-30, F-32 — the SLA cluster.** Excluded by the founder: payments are not
live, so a dashboard nobody reads is not worth the work. Note that
`promised_delivery_at` still has no writer anywhere, so the SLA report will
read 0 orders / 0 % for every tenant until that is addressed, and `/features`
still sells setting a deadline.

**F-16 — tokens in logs.** Not substantiated. No log call emitting a token or
URL was found; `invite_url`/`reset_url` in `email_tasks.py` are template
context. Left in the report marked as unproven rather than silently dropped.

**F-26 — "no retention job exists".** Overstated: three cleanup jobs do exist.
The real, narrower gap (no retention for `audit_events`, closed orders, or
S3 objects orphaned by deleted rows) remains open.

## Still open

Ordered by what I would do next.

1. **F-07 — plan change opens a second Stripe subscription.** The only path to
   Stripe for a plan change is `create_checkout_session`; there is no
   `Subscription.modify` for upgrades (the one that exists is cancel-only). A
   customer changing plan ends up billed twice. Not fixed here because it
   needs a live Stripe test account to verify proration, and getting it half
   right is worse than the current state. **Do this before billing goes live.**
2. **F-12 — platform `Identity` has no GDPR route.** `erase_identity` and
   `export_for_identity` exist in `gdpr_service` and are referenced by nothing
   outside a docstring.
3. **F-01, second half — the attachment bucket still has no backup.**
   `docs/BACKUP_RESTORE.md:13` calls this a settled operator decision, but the
   cited section of `PRELAUNCH_REVIEW_2026-04-25.md` is an unresolved TODO,
   not a decision. Losing the bucket today is unrecoverable. Operator action:
   enable bucket versioning + object lock, and add an `rclone copy` of the
   attachment bucket.
4. **F-36 — no error tracking of any kind.** A 500 at 3am is invisible.
5. **F-10, F-41 — attachment upload does a blocking boto3 PUT on the loop and
   reads the whole body into memory before checking the size limit.**
6. **F-34 — scheduled jobs write no audit events**, including the auto-close
   of orders and the hard-cut of tenants.
7. **F-24, F-25, F-27, F-28, F-37, F-38, F-39, F-43** — see `findings.md`.
   Several need a product or operator decision rather than code.

Operator action that no code change can cover: `STRIPE_PRICE_STARTER` /
`STRIPE_PRICE_PRO` are still unset in production (open across four audits), so
paid checkout silently no-ops.

# E2E audit — 2026-07-26

**Audited commit**: `b94348a` (tip of `origin/main`). Production runs `d65161b`,
one docs-only commit behind, so what was read is effectively what is deployed.

**Baseline**: 518 tests pass (74 s), ruff clean. The engineering is genuinely
good — RLS tenant isolation, Argon2id, single-use tenant recovery tokens, an
audited platform support-access grant, append-only audit log, trilingual legal
copy better than most enterprise vendors. None of that is in question below.

Two independent audits ran:

* [findings.md](findings.md) — 43 security / correctness / ops findings from 8
  parallel static lenses.
* [product-panel.md](product-panel.md) — 5 grounded personas (prospect, daily
  operator, client contact, CTO+DPO, operator-investor) + a synthesis judge.

**➡ [FIXES.md](FIXES.md) — what was actually fixed, what was deliberately not
changed and why, and what is still open.** 26 findings plus 6 product defects
are fixed across six commits; 534 tests pass. Read that first if you only read
one file.

## Coverage and honesty note

The security run's verification layer was cut short: 52 of 66 verifier agents
died on an account session limit. **No findings were lost** — all 8 finder
agents and the dedupe agent completed, and verifiers only judge findings that
already exist. The dead verifiers were replaced by hand-checking the source.

Every finding carries its true status. Of 43: **31 confirmed**, 4 partial or
mitigated, 2 overstated by the reporter, 1 not substantiated, 5 unverified.
The product panel completed fully (6/6); its load-bearing claims were
re-verified by hand and two were corrected.

This matters more than the headline count. Hand-verification killed or narrowed
seven claims — for example F-15 (weak `APP_SECRET_KEY` default) is real in code
but `docker-compose.prod.yml:60` asserts `${APP_SECRET_KEY:?}`, so it only bites
self-hosters running uvicorn directly; and F-26 ("no retention job exists") is
simply wrong — three cleanup jobs do exist.

## The one structural theme

Both audits, run independently, hit the same wall: **the site describes a
product the code has not finished building, and the gap is widest exactly where
trust is decided.**

* The homepage FAQ, the pricing page, the privacy policy and — critically —
  `terms.html:123` promise "CSV / ZIP export endpoints … available at any time".
  `grep -riE "zipfile|ZipFile" app/ scripts/` returns **nothing**. The only bulk
  export in the product is order headers as CSV. That promise is in the binding
  Terms, so it is a liability, not a copy nit.
* `/features` sells "you quote and set the deadline". `promised_delivery_at` is
  read by the CSV export, the PDF, `orders/detail.html` and **every query in
  `sla_service.py`** — and written by nothing in `app/`. The SLA dashboard
  reports 0 orders and 0 % on-time for every tenant that will ever exist.
* The homepage says drawings live in EU cloud storage and "backups run daily".
  `scripts/backup.sh` dumps Postgres only; `docs/BACKUP_RESTORE.md:13` states S3
  attachment backup is out of scope. Customers' CAD files have **no backup and
  no bucket versioning**.
* The privacy policy names Hetzner, Brevo, Stripe and Porkbun. It does not name
  the object-storage provider that holds every drawing and every DB backup.

## Top 10, ranked by what I would actually fix first

| # | id | what | status |
|---|---|---|---|
| 1 | F-01 | Attachment bucket has no backup at all; `rclone sync` also mirrors a `/backups` wipe to the remote, destroying dump history | confirmed |
| 2 | — | Customer confirms a quote → **nobody at the supplier is told**; the notification goes to the customer's own colleagues, including the actor | confirmed |
| 3 | F-04 | Every `/platform/billing` route acts on the first row of an unordered membership query — wrong tenant under multi-membership | confirmed (agent) |
| 4 | F-30 | SLA feature is structurally dead — `promised_delivery_at` has no writer | confirmed |
| 5 | — | Terms + 3 marketing surfaces promise a CSV/ZIP export that does not exist | confirmed |
| 6 | F-06 | A stranger can lock a named staff member out of their tenant by self-signing-up with that person's email | confirmed |
| 7 | F-11 | Art. 17 erasure writes the subject's name + email into `audit_events`, which `portal_app` can never scrub | confirmed |
| 8 | F-13 / F-14 | Fabricated `quoted_total` of 0.00 on pipeline jumps; `add_item` never refreshes the cached total, so the PDF subtotal contradicts its own line items | confirmed |
| 9 | F-19 | Every GitHub Action is on a mutable tag — including `appleboy/ssh-action@v1.2.0`, which holds the production VPS SSH key | confirmed |
| 10 | F-07 / F-09 | Plan change opens a *second* Stripe subscription; no status-transition guard, so a stale `updated` can resurrect a cancelled sub | confirmed |

Cheapest high-value fixes: **F-14** (one `_recompute_quoted_total` call),
**F-29** (one changed default), **F-19** (pin action SHAs), **F-01** (`rclone
copy` + bucket versioning).

## Product verdict

The panel's call, which I find well-evidenced: Assoluto is **an upmarket product
wearing a downmarket price tag**. Consignment stock with signed movements,
per-customer catalogs, SLA reporting and a tenant-wide audit log describe a
30–150 person subcontract manufacturer; the pricing page addresses a three-user
workshop at 490 Kč, and the free unlimited Community tier sits at the top of
that page absorbing every technically capable prospect.

Two structural product gaps, both verified in code:

* **The two-sided loop is one layer short on both sides.** Staff cannot edit an
  order at all (no edit route exists — `customers.py` and `products.py` both
  have one), cannot set a due date, cannot duplicate an order. Clients get
  content-free "status changed" emails from a single global `From` with no
  `Reply-To`, so a reply reaches the platform operator, not the supplier.
* **Nothing measures whether a client ever logs in.** `CustomerContact` has
  `invited_at` and `accepted_at` but no `last_login_at`, while staff `User` has
  one. The portal's entire ROI is clients self-serving, and that number is
  invisible to both the supplier and the founder.

Also worth knowing before charging anyone: clicking "Start 30-day trial" on the
**Pro** card starts a **Starter** trial (`plan_code="starter"` hardcoded at
`platform/service.py:583`), capped at 3 users / 20 contacts, disclosed only in
the Terms. And the pricing page states no VAT position while
`billing/service.py:374` enables Stripe `automatic_tax` and the imprint says
"Neplátce DPH".

## Corrections made to the panel's own claims

Hand-verification did not only confirm. Two panel claims were wrong:

* The invitation email **does** identify the sender — "{tenant} invites you on
  behalf of {customer}". It is thin (no benefit statement), not anonymous.
* The "14-day trial" comment drift is real (`platform/service.py:576` vs
  `TRIAL_DAYS = 30`), but it is a stale comment, not a behavioural bug.

## What to do with this

Nothing here is auto-applied. The two documents are evidence, not a work order.
The one recommendation I would make unprompted: **spend a week making every
public claim true before building anything new** — either ship the export and
the due-date field, or change the four pages and the Terms that promise them.
Claim accuracy is the cheapest fix on this list and the only one that is
currently a contractual exposure.

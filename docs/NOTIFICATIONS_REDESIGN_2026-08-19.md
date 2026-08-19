# Notification redesign — 2026-08-19

Status: **plan + implementation** (branch `feat/notifications-redesign`)

## Why

The notification layer worked, but it routed mail to exactly one kind of
person: the tenant administrator. Ten problems, found by reading
`app/services/notification_service.py` against the rest of the app:

1. **`TENANT_STAFF` (Operator) never received anything.** `_staff_recipients`
   hard-filtered `role == TENANT_ADMIN`, while `tenant_staff` is the default
   role on `User` and the pre-selected option in the invite form. The person
   hired to run production was the one person never told an order arrived.
2. **`notification_prefs` was a dead column.** Present on `User` and
   `CustomerContact` since migration `0002`, written only by the GDPR eraser,
   read nowhere. No opt-out existed anywhere in the product.
3. **No order ownership.** Every staff-side event was a broadcast; there was
   no way to say "this one is yours".
4. **Every contact of a customer got every mail.** A customer with eight
   contacts received eight copies of each status change.
5. **`build_order_submitted` did not exclude the actor**, so a staff member who
   submitted an order was emailed about their own click. Every sibling builder
   excluded the actor; this one was missed.
6. **A customer was never told an order had been created for them.** Staff
   creating an order produced no mail until the first status transition.
7. **Attachment uploads sent nothing.** A customer uploading a revised drawing
   was invisible until somebody happened to log in.
8. **Invited-but-not-accepted users were mailed.** Staff rows keep
   `is_active=True` with `password_hash IS NULL`; contacts keep
   `accepted_at IS NULL`. Both sat in recipient lists and got links they could
   not open.
9. **No tenant fallback address.** With zero active admins,
   `build_order_submitted` returned `None` and an order submission went
   silently unnotified.
10. **No coalescing.** The bulk-transition endpoint scheduled one email task
    per order — moving 30 orders sent 30 mails to each recipient.

## Design

### One hard filter, two soft ones

The central idea. Every recipient decision splits into three independent
questions, and they are **not** allowed to behave the same way:

| Filter | Stored as | Semantics | On empty result |
|---|---|---|---|
| **Consent** — "do I want mail about comments at all?" | `notification_prefs["events"][<event>]` | Absolute. A recipient who opted out is never re-added. | Send nothing. |
| **Relevance** — "which orders count as mine?" | `notification_prefs["scope"]` | Best effort. A narrowing heuristic, not a promise. | Yield: use everyone who consented. |
| **Reachability** — "can they actually open the link?" | `password_hash` / `accepted_at` | Best effort. Prefer people who have accepted their invite. | Yield: use the tier above. |

This is what makes the system safe to extend. Scope can be tightened
aggressively (only the assignee, only involved contacts) and pending
invitations can be deprioritised, without ever producing the failure
mode "the customer heard nothing about their own order". Silence is the
worse failure: a link to a login page at least says something happened.

Consent never yields to anything.

Resolution is therefore always, in `_select`:

```
pool       = active rows on the relevant side − actor
consented  = [c for c in pool      if prefs.wants(event)]        # hard
relevant   = [c for c in consented if scope_matches(c, order)]   # soft
tier       = relevant or consented
reachable  = [c for c in tier      if c.accepted]                # soft
recipients = reachable or tier
```

The actor is removed **first**, not last. Removing them at the end lets
them win a tier and take it with them: an accepted contact commenting on
an order whose only other contact is still pending would satisfy the
reachability filter alone, and stripping them afterwards leaves nobody.

Reachability started out as an eligibility rule — a hard filter that
dropped invited-but-unaccepted rows outright. That reintroduced the bug
it was meant to prevent, one layer down: a customer whose only contact
had not accepted yet heard nothing at all. Demoting it to the same
yielding shape as relevance fixed it, and made the two soft filters read
as one idea instead of two special cases.

### Event catalogue

`NotificationEvent` in `app/services/notification_prefs.py`:

| Event | Fires when | Audience |
|---|---|---|
| `order_submitted` | order enters SUBMITTED | staff |
| `order_created` | staff creates an order for a customer | contacts |
| `order_status_changed` | any other transition | the side that did **not** act |
| `order_comment` | non-internal comment | the other side |
| `order_attachment` | file uploaded | the other side |
| `order_assigned` | order assigned to a staff member | the assignee only |

Adding an event is: one enum member, one entry in the default maps, one
email template triple. No new dataclass, no new task function.

### Scope semantics per side

| Side | `scope=all` | `scope=involved` |
|---|---|---|
| Staff | every order in the tenant | orders assigned to me, **plus every unassigned order** |
| Contact | every order of my company | orders I created, commented on, uploaded a file to, or moved (accepting a quote counts) |

An **unassigned** order counts as involved for every staff member. It has
no owner, so it belongs to the whole shop until somebody picks it up —
untriaged work is exactly what must not fall through the cracks. Without
this, "only orders assigned to me" silently binned new orders for anyone
who chose it, as long as one colleague was on the wide setting.

### Defaults

Chosen so the out-of-the-box behaviour fixes the original complaint without
anybody touching a settings page:

| Role | Events | Scope |
|---|---|---|
| `tenant_admin` | all staff events on | `all` |
| `tenant_staff` | all staff events on | `all` |
| `customer_admin` | all contact events on | `all` |
| `customer_user` | all contact events on | `involved` |

Operators are on by default and see everything; a shop that grows past the
point where that is useful flips individuals to `involved` and starts
assigning. `customer_user` starts narrow because the noise complaint is
sharpest there and `customer_admin` (scope `all`) guarantees the company
still hears about everything.

### Storage format

`notification_prefs` JSONB, on both `User` and `CustomerContact`:

```json
{"events": {"order_comment": false}, "scope": "involved"}
```

Missing keys fall back to the role default, so the existing `{}` rows need
no backfill and no migration. The nested `events` object keeps the GDPR
eraser's `_gdpr_erased_at` marker from colliding with an event name — the
parser ignores unknown top-level keys by construction.

### One payload = one email

`OrderNotification` is frozen and carries exactly one `Recipient`. Builders
return `list[OrderNotification]`. This makes retry granularity, logging, and
digest merging all per-recipient, and collapses six task functions into one
`send_order_notification`.

### Digest coalescing

`merge_for_digest()` groups payloads by `(recipient email, event)`. A
recipient with more than one order for the same event gets a single
`order_digest` mail listing them; a recipient with one order gets the normal
single-order template. Pure function over the payload list — no queue, no
new periodic job, no state.

### Staff fallback

When a tenant has **no active staff rows at all**, staff-side events fall
back to `tenants.billing_email` and log `notifications.staff_fallback`.

The fallback is keyed on the rows existing, not on consent — a tenant
whose staff all deliberately muted an event gets a
`notifications.no_recipients` warning in the log rather than a mail to
billing. Pending invitations do not trigger it either: they are
reachable-but-demoted, so a team that has all been invited and none
accepted still gets the mail directly.

One consequence worth stating plainly, because it surprised a reviewer:
if every *accepted* staff member mutes an event while an unaccepted
invitation is outstanding, the mail goes to the pending address alone.
That is consent working — the pending row never opted out, and the
reachability filter has nobody left to prefer over them — but it means
"everyone muted it, so nothing is sent" is only true once there are no
outstanding invitations.

## Changes

### New

- `app/services/notification_prefs.py` — `NotificationEvent`,
  `NotificationScope`, `NotificationPrefs` (mirrors the
  `customer_permissions.OrderPermissions` pattern).
- `migrations/versions/1008_order_assignment.py` — `orders.assigned_to_user_id`.
- `app/email/templates/_layout.html` — shared chrome for new mails.
- Email triples: `order_attachment`, `order_assigned`, `order_created`,
  `order_digest`.

### Rewritten

- `app/services/notification_service.py` — event-driven audience resolution,
  single `OrderNotification` payload, `merge_for_digest`.
- `app/tasks/email_tasks.py` — `send_order_notification` replaces the three
  per-event senders.

### Extended

- `app/models/order.py` — `assigned_to_user_id`.
- `app/services/order_service.py` — `assign_order`, `assigned_to` filter in
  `build_orders_query`.
- `app/routers/orders.py` — assign endpoint, assignment filter, order-created
  notification, digest on bulk transition.
- `app/routers/attachments.py` — attachment notification.
- `app/routers/me.py`, `app/routers/tenant_admin.py` — preference forms.
- Templates: `orders/detail.html`, `orders/list.html`, `admin/profile.html`,
  `admin/user_edit.html`, `me/profile.html`.

## Picked up along the way

* **`admin/sla.html` had a dead filter.** Its timeframe `<select>` used an
  inline `onchange`, which `script-src 'self'` blocks — the control did
  nothing in production. The assignment picker needed the same
  submit-on-change behaviour, so both now use one delegated
  `[data-submit-on-change]` listener in `static/js/app.js`, paired with a
  `[data-hide-when-js]` button that stays visible when scripts do not run
  (`<noscript>` did not cover the CSP case: scripts *are* enabled, the
  handler just never fires).
* **`STATUS_LABELS` existed in three copies** — the orders router, the
  email tasks and the PDF service — and the new templates would have made
  it four. Consolidated next to the enum in `app/models/enums.py`.
* **`admin/profile.html` had four render paths**, and each validation
  error re-render passed its own hand-built context. Adding a second form
  to the page broke two of them. They now share `_profile_context()`.
* **`TenantRef`, the test tenant fixture, was missing `billing_email` and
  `settings`** — columns the app reads off `request.state.tenant`. Added,
  so a test exercising the fallback path fails for the right reason.

## Deliberately out of scope

- **Full daily digest.** Coalescing bulk actions covers the volume problem
  without a notification-event table and a periodic job. Revisit if a tenant
  asks for a morning summary.
- **Trial-nurture routing.** Stays admin-only; it is a billing message, not an
  operational one, and operators cannot act on it.
- **Migrating the seven existing email templates onto `_layout.html`.**
  Mechanical, unrelated to routing, and would triple the diff.
- **In-app notification centre.** Email only, as today.
- **`assigned_to_user_id` in the tenant/GDPR exports.** The export already
  omits `created_by_*`; adding ownership to both belongs with that gap,
  not here.
- **Consolidating the `mock_s3` fixture.** It now exists in four test
  files; they disagree on bucket names, so merging them into
  `conftest.py` is its own change.

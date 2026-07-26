# E2E audit — 2026-07-26

**Commit audited**: `b94348a` (tip of `origin/main`; production runs `d65161b`, one docs-only commit behind).
**Method**: 8 parallel static-analysis lenses -> semantic dedupe -> adversarial refutation.
Product coherence was audited separately by a 5-persona panel + judge (see [product-panel.md](product-panel.md)).

## Honest note on verification coverage

The verification layer was cut short: 52 of 66 verifier agents died on an account
session limit mid-run. **No findings were lost** — all 8 finder agents and the dedupe
agent completed, and verifiers only judge existing findings. I replaced the dead
verifiers by hand-checking the findings myself against the source. Every finding below
carries its real status:

| status | meaning |
|---|---|
| `confirmed` | independently re-read the code and it holds |
| `partial` / `confirmed-mitigated` | true but narrower than reported — read the note |
| `overstated` / `not-substantiated` | reported more than the code supports |
| `unverified` | verifier died and I did not re-check; finder evidence only |

**Verification tally**: confirmed 30, confirmed-minor 1, confirmed-mitigated 1, confirmed-moot 1, likely 1, not-substantiated 1, overstated 1, partial 3, unverified 4 (of 43 findings).

**Severity mix** (as reported by the finders): P1 14, P2 25, P3 4.

## Index

| id | sev | status | category | title |
|---|---|---|---|---|
| [F-01](#f-01) | P1 | `confirmed` | ops | Off-site backup uses `rclone sync`, so a wipe of /backups propagates to the remote — and |
| [F-02](#f-02) | P1 | `confirmed` | injection | Customer-contact-supplied order line text is passed unescaped into ReportLab `Paragraph` |
| [F-03](#f-03) | P1 | `partial` | dependencies | python-multipart pinned at 0.0.26 — vulnerable to CVE-2026-42561, reachable pre-auth thr |
| [F-04](#f-04) | P1 | `confirmed` | authz | Every /platform/billing route acts on an arbitrary tenant — the first row of an unordere |
| [F-05](#f-05) | P1 | `confirmed` | authn | Platform password-reset links are replayable for their whole 30-minute TTL, and the rese |
| [F-06](#f-06) | P1 | `confirmed` | authn | Any unauthenticated stranger can permanently lock a named tenant staff member out by sel |
| [F-07](#f-07) | P1 | `confirmed` | billing-integrity | Plan change starts a second Stripe subscription instead of modifying the existing one —  |
| [F-08](#f-08) | P1 | `likely` | billing-integrity | A tenant hard-cut after cancellation is never reactivated when the customer subscribes a |
| [F-09](#f-09) | P1 | `confirmed` | billing-integrity | No status-transition guard in handle_subscription_upserted — a retried or out-of-order s |
| [F-10](#f-10) | P1 | `confirmed` | availability | Attachment upload does a blocking boto3 PUT on the single-worker event loop, and the thu |
| [F-11](#f-11) | P1 | `confirmed` | privacy | Art. 17 erasure leaves the subject's name and email in audit_events — and the erasure fl |
| [F-12](#f-12) | P1 | `confirmed` | privacy | Platform Identity has no erasure or export path — erase_identity / export_for_identity / |
| [F-13](#f-13) | P1 | `confirmed` | billing-integrity | Staff jump past QUOTED stamps a fabricated quoted_total of 0.00 on unpriced orders, and  |
| [F-14](#f-14) | P1 | `confirmed` | billing-integrity | add_item never refreshes the cached quoted_total, so the PDF's Subtotal and the detail c |
| [F-15](#f-15) | P2 | `confirmed-mitigated` | secrets | The app never refuses to boot with the public default APP_SECRET_KEY in APP_ENV=producti |
| [F-16](#f-16) | P2 | `not-substantiated` | privacy | Single-use recovery tokens (password reset, staff invite, contact invite) are written ve |
| [F-17](#f-17) | P2 | `partial` | dos | Thumbnail background task feeds attacker-controlled images to Pillow with the default 89 |
| [F-18](#f-18) | P2 | `confirmed` | tenant-isolation | Order-item creation accepts any product_id in the tenant, ignoring the product's custome |
| [F-19](#f-19) | P2 | `confirmed` | ops | Third-party GitHub Actions are pinned to mutable tags, and the one holding the productio |
| [F-20](#f-20) | P2 | `confirmed` | session | The platform session cookie has no version field, so a platform password reset (or "log  |
| [F-21](#f-21) | P2 | `confirmed` | authn | Platform password reset accepts a password of any length — the 8-character minimum enfor |
| [F-22](#f-22) | P2 | `confirmed` | ops | The platform password-reset endpoint sends SMTP synchronously on the event loop, so one  |
| [F-23](#f-23) | P2 | `confirmed` | privacy | Tenant login is an account-existence oracle: disabled accounts get a distinct 403 before |
| [F-24](#f-24) | P2 | `partial` | billing-integrity | Invoice PDF back-computes 21 % Czech VAT out of every CZK invoice, including reverse-cha |
| [F-25](#f-25) | P2 | `confirmed` | privacy | Documented production backup procedure ships unencrypted full-database dumps to an undis |
| [F-26](#f-26) | P2 | `overstated` | privacy | No retention or purge job exists for any personal data — privacy.html promises deletion  |
| [F-27](#f-27) | P2 | `unverified` | privacy | Art. 15/20 exports are materially incomplete and asymmetric — each principal type omits  |
| [F-28](#f-28) | P2 | `unverified` | privacy | Customer contacts can self-erase data the tenant is controller of, with no notification  |
| [F-29](#f-29) | P2 | `confirmed` | authz | Customer edit form renders can_set_prices as OFF while enforcement treats a missing key  |
| [F-30](#f-30) | P2 | `confirmed` | data-integrity | SLA dashboard is structurally dead: promised_delivery_at has no writer anywhere in the c |
| [F-31](#f-31) | P2 | `confirmed` | data-integrity | Backward and cancelling transitions never clear milestone stamps, and jumps fabricate th |
| [F-32](#f-32) | P2 | `confirmed-moot` | data-integrity | SLA dashboard counts cancelled orders as overdue forever and inflates heatmap totals — s |
| [F-33](#f-33) | P2 | `confirmed` | data-integrity | Concurrent transitions on one order are not serialised: a double-click writes two histor |
| [F-34](#f-34) | P2 | `confirmed` | audit-integrity | Every scheduled job writes zero audit_events — including auto-close of orders, a hard DE |
| [F-35](#f-35) | P2 | `confirmed` | audit-integrity | Bulk order status changes are recorded in the audit log as actor 'system' — the staff me |
| [F-36](#f-36) | P2 | `confirmed` | ops | There is no error tracking or alerting of any kind: unhandled 500s, permanently failed e |
| [F-37](#f-37) | P2 | `confirmed-minor` | ops | LOG_LEVEL is hard-coded in the base compose environment and not re-declared in the prod  |
| [F-38](#f-38) | P2 | `unverified` | billing-integrity | The manual subscription editor — the endpoint that grants free plan time and marks tenan |
| [F-39](#f-39) | P2 | `confirmed` | i18n | Every error string in the platform signup, login, verification and billing flows is hard |
| [F-40](#f-40) | P3 | `confirmed` | session | Logout is cookie-deletion only — a session token that has already been copied stays vali |
| [F-41](#f-41) | P3 | `confirmed` | dos | Upload body is fully read into memory before the size limit is checked, so a rejected 50 |
| [F-42](#f-42) | P3 | `confirmed` | privacy | No Cache-Control header on any response, so authenticated HTML is browser-cacheable and  |
| [F-43](#f-43) | P3 | `unverified` | privacy | Cookies policy does not match what the app sets — a listed cookie does not exist, and th |

---

## F-01 — Off-site backup uses `rclone sync`, so a wipe of /backups propagates to the remote — and S3 attachments are never backed up at all

- **Severity**: P1  ·  **Verification**: `confirmed` (via agent (2 lenses) + re-read)  ·  **Category**: ops
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `scripts/backup.sh:74 (and :70-71); docs/BACKUP_RESTORE.md:11-14`

> **Verification note.** History loss, not total loss: sync runs only after a successful dump, so the remote keeps that day's file. Attachment half is fully confirmed.

**Evidence**

```
scripts/backup.sh:69-75:
    if [ -n "${RCLONE_REMOTE:-}" ]; then
        rclone sync "${BACKUP_DIR}" "${RCLONE_REMOTE}/pg" --progress --retries=3
    fi
immediately preceded by local rotation `find "${BACKUP_DIR}" -name 'portal-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete`. docs/BACKUP_RESTORE.md:11-14: "S3 attachment backup is **not** in scope for this script". Docs also contradict the script: BACKUP_RESTORE.md:9 says "last 30 days locally" while scripts/backup.sh:29 sets KEEP_DAYS default 14.
```

**Failure scenario**

(a) An attacker with the `deploy` account, a `rm -rf /backups` fat-finger, or a full disk empties /backups. At 03:00 cron runs backup.sh; `rclone sync` is a destructive mirror and deletes destination objects absent at source, so the wipe propagates to the Backblaze/R2 remote. Every historical off-site dump is gone in one scheduled run; there is no documented object-lock/versioning. (b) Order attachments and client assets live only as S3 objects — nothing in scripts/backup.sh or any workflow copies or versions that bucket, so a restored Postgres has storage_key rows pointing at objects that no longer exist.

**Impact**

Total, unrecoverable loss of customer data: the entire database history for (a), every file any customer ever uploaded for (b). Both are GDPR Art. 32(1)(c) availability failures and business-ending.

**Recommendation**

Change scripts/backup.sh:74 to `rclone copy ... --retries=3` and move retention to a remote-side lifecycle rule (or `rclone sync --backup-dir`). Enable object lock / versioning on the remote bucket. Add a copy (or versioning + lifecycle) for the S3 attachment bucket. Reconcile docs/BACKUP_RESTORE.md:9 with KEEP_DAYS=14.

**Adversarial verifier correction**

Three details are wrong or overstated, though the core finding stands.

(a) "a full disk empties /backups" is not a valid trigger. A full disk does not delete files, and if the dump fails or truncates, `set -euo pipefail` + the <1024-byte sanity check at backup.sh:60-63 abort the script *before* rotation and sync. The realistic triggers are: `rm -rf /backups`, a compromised/fat-fingered `deploy` account, or `/backups` being a separate volume that fails to mount (mkdir -p silently recreates an empty dir, dump succeeds, sync then mirrors the emptiness).

(b) Impact for scenario (a) is not "total, unrecoverable loss of customer data". Because the sync only runs after a successful fresh dump, the remote ends up holding that day's dump. What is lost is the entire *history* (all prior daily dumps, i.e. any point-in-time recovery and any ability to roll back logical corruption or a malicious data deletion discovered later). Total loss requires the DB and /backups to be lost in the same window.

(c) The location line for the docs contradiction should also include `docs/OPERATOR_PLAYBOOK.md:233`, which documents the behaviour as `rclone copy` — the operator has been told the wrong verb, whi

**Adversarial verifier correction**

Directionally correct; four corrections and one addition.

1. Doc line number: the "last 30 days locally" claim is `docs/BACKUP_RESTORE.md:10`, not :9. The contradiction with `KEEP_DAYS=14` (backup.sh:29) is real, and `docs/OPERATOR_PLAYBOOK.md:175-177` independently says "14-day rotation", so :10 is the wrong doc, not the wrong code.

2. The finding missed the sharper doc/code mismatch: `docs/OPERATOR_PLAYBOOK.md:232-234` tells the operator the script "`rclone copy`-ies to the remote". The operator has been told copy semantics and is getting sync semantics — that is the reason nobody has caught this, and it should be fixed in the same commit.

3. The evidence mischaracterises `BACKUP_RESTORE.md:13-15`. That passage claims S3 backup is out of scope "see the operator decision in PRELAUNCH_REVIEW_2026-04-25.md". There is no such decision. `PRELAUNCH_REVIEW_2026-04-25.md:215-217` says the opposite — "Add a `rclone sync` of the S3 bucket to off-site storage. Without this, an attachment-bucket loss is unrecoverable" — and `:150-153` classes it as an unfixed **P0**. So BACKUP_RESTORE.md is not merely silent on S3; it launders an unremediated P0 into a fake accepted decision. That is wort

---

## F-02 — Customer-contact-supplied order line text is passed unescaped into ReportLab `Paragraph` — server-side SSRF, local file read, and a stored 500 on the order PDF

- **Severity**: P1  ·  **Verification**: `confirmed` (via agent (2 lenses))  ·  **Category**: injection
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/pdf_service.py:308 (also :204, :266, :307, :309); app/services/invoice_pdf_service.py:276,279`

**Evidence**

```
pdf_service.py builds the items table with no escaping:
        data.append([Paragraph(sku, normal), Paragraph(name, normal),  # <- line 308, raw user text
`OrderItem.description` is a 2000-char free-text column written straight from the form (app/routers/orders.py:773 `description: str = Form("")`; `order_service.add_item` only `.strip()`s), and `OrderPermissions.can_add_items` defaults to True (app/services/customer_permissions.py:19). Verified by running the real service with pinned reportlab 4.4.10:
    SSRF case: PDF rendered OK, 45017 bytes
    SERVER HITS: [('/internal-probe.png', 'ReportLabAgent')]
    DoS case: EXPORT CRASHES -> ValueError: paragraph text '<para>M8 <b>bolt, zinc</para>' caused exception Parse error
    file-img: OK (1759 bytes)
and `reportlab.rl_config.trustedSchemes == ['file','rml','data','https','http','ftp']`, `trustedHosts is None`.
```

**Failure scenario**

A CustomerContact POSTs `/app/orders/{own_order_id}/items` with `description=Bracket <img src="http://169.254.169.254/latest/meta-data/" width="1" height="1"/>`. Any later `GET /app/orders/{id}.pdf` (app/routers/orders.py:483, guarded only by require_login) makes reportlab perform a server-side HTTP GET from inside the app container with UA `ReportLabAgent`; nothing is logged or surfaced. Swapping to `file:///…` embeds a local file in the returned PDF. Alternatively storing `description=M8 <b>bolt` permanently 500s that order's PDF export. The same sink is reachable via `tenant.settings['billing_address']` (app/platform/routers/billing.py:428) rendered at invoice_pdf_service.py:279.

**Impact**

Blind SSRF from the production container reaching cloud metadata, the docker-compose internal network and any RFC1918 host, available to the lowest-privilege authenticated principal; with `file://` a limited arbitrary-file read whose contents are returned inside the PDF; plus a two-character payload that permanently bricks the customer-facing PDF export. tests/test_orders_pdf.py only exercises benign strings.

**Recommendation**

Apply `xml.sax.saxutils.escape` to every variable interpolated into a `Paragraph` in both PDF services (escape the variable, not the surrounding `<b>`/`<i>` literals), and as defence in depth set `reportlab.rl_config.trustedSchemes = []` at import time in pdf_service.py.

**Adversarial verifier correction**

Core claim stands; three details are overstated.

(a) `file://` is NOT a usable arbitrary-file read. I tested `file:///etc/hosts` and a plain text file: both raise `PIL.UnidentifiedImageError`, which surfaces as the same 500, not as embedded content. It only works when the target is already a decodable image on the container filesystem — negligible value. Non-image `file://` targets collapse into the DoS case, so this should be described as a second DoS trigger, not as file disclosure.

(b) The SSRF is blind in the strict sense: nothing is returned to the attacker unless the target responds with a PIL-decodable image. Practical yield is internal-network probing via success-vs-500 and response timing, not data exfiltration. Whether `169.254.169.254` yields anything depends on the VPS provider, which I cannot observe (CLAUDE.md §11/§16 describe a docker-compose + Caddy VPS, not EC2).

(c) `pdf_service.py:307` is not an independent sink — `sku` is derived from the same `description` string by the `" — "` split heuristic. And lines 204/266 (`tenant.name`, `customer.name`, `customer.billing_address`) are staff/admin-controlled, not contact-controlled, so they carry materially lower risk

**Adversarial verifier correction**

Two details in the finding are overstated and should be corrected before it reaches the founder:

1. The `file://` primitive is NOT a general arbitrary-file read. reportlab routes `<img src>` through `ImageReader`, so only files that decode as an image come back in the PDF. I verified `file:///etc/passwd` and `file:///etc/hosts` both raise `OSError ... caused exception fileName='file:///etc/hosts' identity=[ImageReader...]` → 500, not a leak. Accurate statement: "reads any image-parseable file on the container filesystem; non-image paths only produce a 500 existence/permission oracle."

2. The SSRF is blind in the same way. The request is definitively made (confirmed listener hit with UA `ReportLabAgent`), but the response body is only exfiltrated into the PDF when it decodes as an image; anything else (e.g. the 169.254.169.254 metadata text response) raises `OSError` → 500. So the metadata-credential-theft framing in "impact" is wrong. What the attacker actually gets is (a) an arbitrary outbound GET from inside the app container to any RFC1918 / link-local / docker-network host, and (b) a reliable 200-vs-500 oracle for internal port/host scanning, since an unreachable port also ra

---

## F-03 — python-multipart pinned at 0.0.26 — vulnerable to CVE-2026-42561, reachable pre-auth through verify_csrf's request.form()

- **Severity**: P1  ·  **Verification**: `partial` (via self: version only)  ·  **Category**: dependencies
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `uv.lock:1499-1500 (python-multipart 0.0.26); reached via app/security/csrf.py:146-149`

> **Verification note.** uv.lock pins python-multipart 0.0.26 — confirmed. The specific CVE id is post-cutoff and I could not confirm it; treat the version bump as prudent hygiene, not a proven exploit.

**Evidence**

```
uv.lock:1498-1500 `name = "python-multipart" / version = "0.0.26"`; pyproject.toml:25 pins only a floor `"python-multipart>=0.0.12"`. app/security/csrf.py:146-149, a router-level dependency on EVERY mutating route including the public router:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
CVE-2026-42561 / GHSA-pp6c-gr5w-3c5g: unbounded number/size of part headers in MultipartParser causing excessive CPU. Affected < 0.0.27; patched 0.0.27. (CVE-2026-40347 IS fixed in 0.0.26 — this one is not.)
```

**Failure scenario**

Attacker GETs the site and is handed a `csrftoken` cookie unconditionally by CsrfCookieMiddleware, then POSTs `/auth/login` with `Content-Type: multipart/form-data` and a part whose header block is tens of megabytes of repeated header lines (Caddy's 60MB body cap permits it). `verify_csrf` runs as a FastAPI dependency — before the slowapi `@limit` on the endpoint body — so the login rate limiter never fires. The parse burns CPU on the single event loop (one-worker Dockerfile CMD), stalling the whole portal, all tenants and /healthz.

**Impact**

Unauthenticated remote denial of service against the entire production deployment, needing only a freely-issued CSRF cookie and no account.

**Recommendation**

Bump the floor in pyproject.toml to `python-multipart>=0.0.27`, run `uv lock --upgrade-package python-multipart`, redeploy, verify with `grep -A1 'name = "python-multipart"' uv.lock`. Consider rejecting oversized bodies before `request.form()`.

---

## F-04 — Every /platform/billing route acts on an arbitrary tenant — the first row of an unordered membership query, including platform support-access grants

- **Severity**: P1  ·  **Verification**: `confirmed` (via agent (2 lenses))  ·  **Category**: authz
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/routers/billing.py:76-88 (duplicated loop at :552-567); app/platform/service.py:205-214`

**Evidence**

```
`_resolve_current_tenant` never consults `membership.access_type`:
    memberships = await list_memberships_for_identity(db, identity_id=identity.id)
    for membership in memberships:
        if membership.user_id is None: continue
        tenant, target = await resolve_membership_targets(db, membership=membership)
        if tenant is None or not isinstance(target, User): continue
        if target.role != UserRole.TENANT_ADMIN: continue
        return tenant, target
and the source query has no ORDER BY:
    select(TenantMembership).where(TenantMembership.identity_id == identity_id, TenantMembership.is_active.is_(True))
Meanwhile `grant_platform_admin_support_access` (app/platform/service.py:415-423) creates exactly the row this loop accepts: `User(tenant_id=tenant_id, email=email_lower, password_hash=None, role=UserRole.TENANT_ADMIN)`. `TenantMembership.access_type` exists (app/platform/models.py:125) but is never checked here.
```

**Failure scenario**

A platform admin uses the documented support workflow (POST /platform/admin/tenants/{id}/support-access) against paying tenant ACME, then opens /platform/billing. The resolver returns whichever tenant_admin membership Postgres emits first, which may be ACME: the page renders ACME's plan, usage and invoice history, /platform/billing/invoices/{id}.pdf downloads ACME's invoices, POST /platform/billing/cancel-subscription cancels ACME's subscription (after CANCEL_GRACE_DAYS `enforce_canceled_subscriptions` sets `tenants.is_active=false` and bumps every session_version), and POST /platform/billing/details overwrites ACME's IČO / billing address. Because there is no ORDER BY, the heap order is not stable — the GET can render tenant A while the following POST mutates tenant B. The same applies to any non-operator identity holding two tenant_admin memberships: they can only ever pay for one tenant and the other silently expires.

**Impact**

Cross-tenant billing read and money/availability actions with no tenant picker and no confirmation: reading another company's invoices, cancelling their paid subscription (leading to automated deactivation), and rewriting their legal invoicing identity. Nothing in the tenant's audit log records a billing read or a cancel initiated here. Violates the CLAUDE.md §6 rule that support access is an explicit, scoped grant.

**Recommendation**

Skip memberships with `access_type == MEMBERSHIP_ACCESS_SUPPORT` in `_resolve_current_tenant` and in the duplicated loop in `post_verify_checkout`; add a deterministic `ORDER BY created_at` to `list_memberships_for_identity` (app/platform/service.py:205); and make the tenant explicit — take a tenant_id parameter on every /platform/billing route (or redirect to a tenant picker when more than one candidate remains) so the GET and the POST cannot disagree.

**Adversarial verifier correction**

Directionally correct, with three factual corrections.

(a) The impact line "Nothing in the tenant's audit log records a billing read or **cancel** initiated here" is wrong on cancel. `cancel_subscription` writes a `billing.subscription_canceled` audit row with `actor_label=identity.email` and an explicit `tenant_id` (billing/service.py:184-215), and `billing_details_save` writes `tenant.settings_updated` into the target tenant (billing.py:495-506). Only the **read** paths (dashboard, invoice PDF) and the plan-change/checkout path are unaudited. What *is* genuinely missing versus the switch flow is the `platform.support_access_session_started` marker, so a tenant admin reading their log sees a cancel attributed to a support User with no record of how that session came to exist.

(b) This is not an unprivileged cross-tenant escalation. Every tenant the loop can return is one where the identity holds an **active tenant_admin membership** — either self-granted support access (which CLAUDE.md §6 defines as a legitimate explicit grant) or a genuine second admin membership. There is no path here for an outsider, or for a tenant_staff/customer-contact identity, to reach another tenant's b

---

## F-05 — Platform password-reset links are replayable for their whole 30-minute TTL, and the reset invalidates no existing platform session — the single-use guarantee of CLAUDE.md §14 exists only in the tenant flow, and the whole flow is untested

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: authn
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/service.py:112-141; consumed at app/platform/routers/platform_auth.py:219-283`

> **Verification note.** Identity has no session_version column (same root as F-20), so nothing invalidates a consumed platform reset token.

**Evidence**

```
The token payload carries nothing but the identity id:
    def create_platform_password_reset_token(secret_key, identity_id): return create_token(secret_key, TokenPurpose.PLATFORM_PASSWORD_RESET, {"identity_id": str(identity_id)})
and consuming it mutates nothing a second verification could notice:
    async def reset_platform_password(db, identity_id, new_password): ... identity.password_hash = hash_password(new_password); await db.flush()
Contrast app/services/auth_service.py:614-621: `if row.session_version != token_session_version: raise InvalidInvitation("token already used or superseded")` then `row.session_version += 1`. The Identity model (app/platform/models.py:32-70) has no session_version column. Coverage: `grep -rl 'platform/password-reset' tests/` → empty; `grep -rl 'reset_platform_password' tests/` → empty; tests/test_token_replay.py covers only tenant flows.
```

**Failure scenario**

(a) Replay: a tenant owner resets their platform password; the link lands in a shared/forwarded mailbox, a support-ticket screenshot, or an email-scanning proxy. Within the remaining 30-minute window whoever else holds the URL POSTs the same token to /platform/password-reset/confirm — `decode_platform_password_reset_token` only checks the itsdangerous signature and age — and overwrites the victim's brand-new password, N times if desired. (b) Persistence: the victim's reset does not log the attacker out; read_platform_session validates only signature and 14-day age and get_current_identity re-reads only `is_active`, so a stolen `sme_portal_platform` cookie keeps working.

**Impact**

Full takeover of a platform Identity — every tenant that identity can switch into, plus the Stripe billing surface (cancel subscription, billing portal, change billing details) — with no credentials, no CSRF and no victim interaction beyond the reset they themselves initiated. Directly contradicts the security invariant the repo documents for itself, and nothing in the 518-test suite touches the route.

**Recommendation**

Bind the token to state the reset destroys. Migration-free: include a digest of the current password hash in the payload (`{"identity_id": ..., "h": sha256(identity.password_hash)[:16]}`) and have `reset_platform_password` recompute and refuse on mismatch. Better: add a `session_version` column to `platform_identities`, embed it in the token, bump it on reset, and compare it in `app/platform/deps.py:get_current_identity` (see F-20). Port the four tests in tests/test_token_replay.py to the platform identity.

---

## F-06 — Any unauthenticated stranger can permanently lock a named tenant staff member out by self-signing-up with that person's email address

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: authn
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/auth_service.py:136 (gate) and :66-105 (global lookup)`

> **Verification note.** auth_service.py:136 raises UnverifiedIdentity *after* verify_password succeeds; the lookup is global and unscoped.

**Evidence**

```
auth_service.py:136-137, inside `authenticate()` AFTER the password has already verified correctly:
        if await _has_unverified_identity(email):
            raise UnverifiedIdentity()
and the lookup (auth_service.py:88-96) is global — no tenant scoping, no membership join, run on the RLS-bypassing owner DSN:
        "SELECT email_verified_at IS NULL FROM platform_identities WHERE email = :e LIMIT 1"
The attacker side needs no auth: `signup_tenant` (app/platform/service.py:537-539) only refuses when an Identity already exists, and `create_tenant_with_owner(..., pre_verified_identity=False)` writes the new Identity with `email_verified_at = NULL`. /platform/signup is live and returns 200.
```

**Failure scenario**

Attacker learns a public staff email at a customer (jan.novak@firma.cz). That user was created via /app/admin/users/invite, which by design never mints a platform Identity. Attacker POSTs /platform/signup with owner_email=jan.novak@firma.cz and a free slug; signup succeeds and inserts an unverified Identity. Jan then signs in at firma.assoluto.eu with his correct password: verify_password succeeds, `_has_unverified_identity` returns True, `UnverifiedIdentity` → public.py:280 redirects him to /platform/check-email. No amount of correct-password entry helps. Recovery requires Jan to verify an account he never created (handing the attacker a verified Identity with the attacker's password) or manual psql surgery. Per-IP limit 10/15 min, SIGNUP_THROTTLE 3/day per email → ~960 lockouts/day from one host.

**Impact**

Remote, unauthenticated, targeted denial of service against any tenant staff account with a guessable or public email — most of them for a B2B supplier portal. Also litters `tenants`/`platform_identities` with junk rows only manual DB work clears.

**Recommendation**

In `_has_unverified_identity`, stop matching on email alone: take the authenticated `user.id` and only consider an Identity actually linked to that User via `platform_memberships` (`JOIN platform_memberships m ON m.identity_id = i.id WHERE m.user_id = :uid`). An Identity a stranger created is not linked to Jan's User row, so the gate stops firing for invited staff while still covering the self-signup owner it was written for.

---

## F-07 — Plan change starts a second Stripe subscription instead of modifying the existing one — the tenant is billed for both, forever

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: billing-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/routers/billing.py:254-268; app/templates/platform/billing/dashboard.html:171`

> **Verification note.** Plan change reaches Stripe only via create_checkout_session (billing.py:259,592). The Subscription.modify at service.py:235 is the cancel path only.

**Evidence**

```
`start_checkout` does no existing-subscription check before minting a new Checkout Session:
    current_sub = await get_subscription_for_tenant(db, tenant.id)
    ...
    checkout_url = create_checkout_session(settings, tenant=tenant, plan=plan, ...)
and `create_checkout_session` always builds `"mode": "subscription"` with `line_items: [{"price": plan.stripe_price_id, "quantity": 1}]` (app/platform/billing/service.py:395-401). The only `stripe.Subscription.modify(...)` in the codebase is inside cancel_subscription (service.py:229). The dashboard renders the checkout form for every non-current plan unconditionally (`Upgrade to `/`Downgrade to `, dashboard.html:158-171).
```

**Failure scenario**

Tenant on Starter with live sub_A and `tenants.stripe_customer_id = cus_X` clicks "Upgrade to Pro". create_checkout_session passes `customer=cus_X`, mode=subscription; Stripe creates a SECOND subscription sub_B rather than replacing sub_A. `customer.subscription.created` for sub_B overwrites the single local row (UNIQUE(tenant_id)) at webhooks.py:236, so sub_A becomes invisible to the app. Every month the customer is charged 490 + 1490 CZK and two invoices appear with no explanation. A later "Cancel subscription" only modifies sub_B; sub_A bills indefinitely with no in-app surface to stop it.

**Impact**

Real customers double- or triple-charged monthly, chargeback/refund exposure and a CZ/EU consumer-protection problem; the orphaned subscription survives cancellation. Latent only because Stripe price IDs are still NULL in prod — it fires on the first real plan change after billing goes live.

**Recommendation**

In `start_checkout` / `post_verify_checkout`, when `settings.stripe_enabled` and `current_sub.stripe_subscription_id` is set, do not create a Checkout Session: redirect to `create_billing_portal_session(...)` or call `stripe.Subscription.modify(sub_id, items=[...], proration_behavior="create_prorations")`. Reserve Checkout for tenants with no live Stripe subscription.

---

## F-08 — A tenant hard-cut after cancellation is never reactivated when the customer subscribes again — money taken, service still 404

- **Severity**: P1  ·  **Verification**: `likely` (via self: partial)  ·  **Category**: billing-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/tasks/periodic.py:318-322; app/platform/billing/webhooks.py:158-224; app/platform/routers/billing.py:61-88`

> **Verification note.** enforce_canceled_subscriptions sets tenants.is_active=false; no reactivation path found in webhooks/service. Not exhaustively traced.

**Evidence**

```
The grace job disables the tenant:
    await conn.execute(text("UPDATE tenants SET is_active = false WHERE id = :id"), {"id": tenant_id})
and app/deps.py:122 turns that into a hard 404: `if tenant is None or not tenant.is_active: raise HTTPException(status_code=404 ...)`. No webhook handler ever writes `tenant.is_active` — grep of app/platform/billing/ for `is_active` returns only `Plan.is_active`. The only re-enable path is the operator-only `reactivate_tenant` (app/platform/service.py:341-351). Nothing blocks re-purchase: `resolve_membership_targets` loads the Tenant with no `is_active` filter, and /platform/billing/checkout/{plan} lives on the apex host so `get_current_tenant`'s 404 never applies.
```

**Failure scenario**

Customer cancels; three days after current_period_end `enforce_canceled_subscriptions` sets `tenants.is_active=false`. A week later they change their mind, log in at assoluto.eu/platform, open /platform/billing and click "Try Pro". Checkout succeeds, the card is charged, and the local subscription goes active with plan=pro. They go to {slug}.assoluto.eu and get a bare 404. Nothing told them the tenant was disabled and nothing re-enables it; the only recovery is emailing support.

**Impact**

A win-back customer pays and receives nothing until a human intervenes — the highest-friction moment in the funnel, plus refund/chargeback exposure and a Czech consumer-law problem (paid service not delivered). Also affects any tenant that lapses briefly and re-subscribes.

**Recommendation**

In `handle_checkout_completed` / `handle_subscription_upserted`, when the resulting status is `active` or `trialing`, set `tenant.is_active = True` on the resolved tenant and audit the reactivation. Additionally block or warn in `start_checkout` / `post_verify_checkout` when the resolved tenant has `is_active=False`.

---

## F-09 — No status-transition guard in handle_subscription_upserted — a retried or out-of-order subscription.updated resurrects a canceled subscription

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: billing-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/billing/webhooks.py:239-241`

> **Verification note.** Dedup is per event.id only; handle_subscription_upserted does `if new_status: subscription.status = new_status` with no ordering/transition guard.

**Evidence**

```
    new_status = data.get("status")
    if new_status:
        subscription.status = new_status
No comparison against the current status and no event-timestamp check — the handler never reads `event["created"]`, and `platform_stripe_events` stores only id/type/received_at (migrations/versions/1004_stripe_events.py:35-39). Contrast handle_checkout_completed, which DOES carry an ordering guard: `already_synced = subscription.stripe_subscription_id is not None` (webhooks.py:212-219). handle_invoice_payment_failed (webhooks.py:355) has the same unguarded write.
```

**Failure scenario**

`customer.subscription.updated` (status=active) is delivered while the DB is briefly unreachable → the `async with db.begin()` block raises → 500 → Stripe retries with backoff for up to ~3 days. Meanwhile the card fails, dunning exhausts, and `customer.subscription.deleted` is processed (status=canceled). The retry of the stale `updated` finally lands; its event id was never committed to `platform_stripe_events` (the failed transaction rolled the dedup row back), so it passes dedup and flips status back to `active`. `enforce_canceled_subscriptions` only selects `WHERE s.status = 'canceled'`, so the tenant is never cut off. Stripe does not guarantee event ordering, so plain out-of-order delivery of a near-simultaneous updated/deleted pair produces the same effect.

**Impact**

A cancelled, non-paying tenant keeps full production access indefinitely and appears in no delinquency report — pure revenue leakage with no alarm. The inverse (a stale past_due after a successful payment) shows a paying customer a false past-due state.

**Recommendation**

Refuse to move out of a terminal state: skip the status write when `subscription.status == 'canceled'` and the incoming status is not 'canceled'. Better: store `event['created']` in `platform_stripe_events`, carry `last_event_at` on `platform_subscriptions`, and ignore any subscription event older than the last applied one.

---

## F-10 — Attachment upload does a blocking boto3 PUT on the single-worker event loop, and the thumbnail task blocks it again with a synchronous S3 GET, Pillow decode and PUT

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: availability
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/routers/attachments.py:128; app/tasks/thumbnail_tasks.py:68,95,104,128; Dockerfile:110`

> **Verification note.** No run_in_threadpool/to_thread anywhere in attachments, s3, sender.

**Evidence**

```
app/routers/attachments.py:126-128:
    data = await file.read()
    ...
    s3_storage.upload_bytes(attachment.storage_key, data, content_type=attachment.content_type)
app/tasks/thumbnail_tasks.py:68 — `async def generate_thumbnail(...)` whose body calls the synchronous `s3_storage.download_bytes(...)` (:95), `_render_thumbnail(data, ...)` (:104, `Image.open(...).thumbnail(...)` / `rgb_img.save(...)`), then `s3_storage.upload_bytes(...)` (:128) — none wrapped in `anyio.to_thread.run_sync`. There is no `to_thread`/`run_in_executor` anywhere in app/ (verified by grep). Dockerfile:110 CMD has no `--workers`, so one process, one event loop.
```

**Failure scenario**

A CustomerContact uploads a 30 MB PDF (MAX_UPLOAD_SIZE_MB=50, Caddy allows 60 MB). `upload_attachment` is `async def`, so the fully synchronous botocore PUT to remote S3 runs directly on the event loop with nothing yielding — for its whole duration the single uvicorn worker services no other coroutine: every other tenant's page load, /healthz probe and Stripe webhook hangs. Starlette then awaits `generate_thumbnail` on the same loop, blocking again for the S3 GET, the Pillow decode/resize and the thumbnail PUT. Three or four concurrent uploads make the deploy workflow's own /healthz poll time out; any authenticated contact can trigger it in a loop deliberately.

**Impact**

Whole-portal unavailability across all tenants triggered by one user's ordinary upload; also makes the marketing SLA/uptime claims unachievable and can false-fail the deploy health gate.

**Recommendation**

Wrap the blocking calls in `anyio.to_thread.run_sync` — `s3_storage.upload_bytes` in attachments.py, and `download_bytes` / `_render_thumbnail` / `upload_bytes` in thumbnail_tasks.py. Cheapest correct alternative for the task: make `generate_thumbnail` a plain `def` so Starlette's BackgroundTasks runs it in the threadpool. Longer term add `--workers 2+` to the Dockerfile CMD.

---

## F-11 — Art. 17 erasure leaves the subject's name and email in audit_events — and the erasure flow writes a fresh row containing them

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: privacy
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/routers/me.py:245-256 and app/routers/tenant_admin.py:678-689 (writers); app/services/audit_service.py:195; app/routers/tenant_admin.py:790-838 (reader)`

> **Verification note.** Erasure writes full name+email into entity_label; 0009 grants portal_app SELECT+INSERT only, so it can never be scrubbed.

**Evidence**

```
app/routers/me.py:245 — `original_label = f"{contact.full_name} <{contact.email}>"` then `await audit_record(db, action="contact.gdpr_erased", ..., entity_label=original_label, ...)`. Test-locked: tests/test_gdpr.py:185 `assert events[0].entity_label == "4MEX Owner <owner@4mex.cz>"` and :350 `== "Jan Novák <jan@acme.cz>"`. Prior rows keep PII too: audit_service.py:195 `actor_label=actor.label[:255]`, and auth_service.py:318-323 writes `entity_label=user.email, after={"email": user.email, ...}` into the JSONB diff. The gdpr_service docstring nonetheless claims "the data subject's identifying data is gone". portal_app has no UPDATE/DELETE grant on audit_events (migrations/versions/0009_audit_events.py:115), so the app literally cannot scrub them.
```

**Failure scenario**

Jan Novák exercises Art. 17 at /app/admin/profile. erase_user() nulls his row; the same request INSERTs `audit_events(action='user.gdpr_erased', entity_label='Jan Novák <jan.novak@4mex.cz>')`. Any plain STAFF user (audit_index depends on require_tenant_staff, not tenant-admin) opens /app/admin/audit?q=Novák — list_events ILIKEs both entity_label and actor_label — and gets his full name and work email, every earlier row where he was actor, and the `user.invited` row whose diff JSON contains his email. admin/audit.html:87,94,121 renders all three verbatim.

**Impact**

The erasure is not an erasure: a data subject told their data was deleted remains fully identifiable, by name and email, to every staff user of the tenant, indefinitely — an Art. 17(1) failure with a paper trail (the test file) showing it was deliberate. Undercuts the product's own GDPR selling point.

**Recommendation**

(1) Stop composing the label from PII in me.py:245 and tenant_admin.py:678 — use e.g. `f"contact {contact.id}"` and update the two assertions in tests/test_gdpr.py. (2) Add an owner-role scrub (erase path or periodic job via _owner_engine, since portal_app cannot UPDATE audit_events) rewriting actor_label/entity_label to gdpr_service.ANONYMIZED_LABEL and stripping email keys from `diff` for rows referencing the erased id. If the identifiers are retained under Art. 17(3)(e), state that in privacy.html §5 and correct the gdpr_service docstring.

---

## F-12 — Platform Identity has no erasure or export path — erase_identity / export_for_identity / find_target_rows_for_email are dead code, and the account survives tenant-side "account deleted"

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: privacy
- **Auto-fixable**: False  ·  **Known from a prior run**: True (F-BE-003)
- **Location**: `app/services/gdpr_service.py:150 (export_for_identity), :246 (erase_identity), :266 (find_target_rows_for_email); app/platform/routers/ (no profile route); app/templates/www/privacy.html:150-152`

> **Verification note.** erase_identity/export_for_identity exist in gdpr_service but are referenced by nothing outside a docstring — no router surface.

**Evidence**

```
`grep -rn 'erase_identity|export_for_identity|find_target_rows_for_email' app scripts tests` returns zero hits outside gdpr_service.py — all three are defined and never called. The platform router surface is login / logout / password-reset / select-tenant / switch / complete-switch / signup / verify-* / check-email / admin/* / billing/* — there is no /platform/profile, no export, no delete, and no operator-side delete either (platform_admin.py has tenant CRUD, subscription editing and support-access only; scripts/ has create_tenant, make_platform_admin, seed_dev, backup.sh). `git show --stat 8b3ad5c` — the commit recorded against F-BE-003 as 'fixed' — touched only app/routers/me.py, app/templates/me/profile.html and tests/test_gdpr.py, nothing under app/platform/. erase_identity also never nulls `terms_accepted_ip`, despite migrations/versions/0013_consent_record.py claiming it does. privacy.html §7 tells users "The data-export and account-delete actions are available self-service".
```

**Failure scenario**

A tenant owner signs up at assoluto.eu; signup_tenant creates both a tenant User and an Identity holding email, full_name, an Argon2 hash, terms_accepted_at/version and terms_accepted_ip (the signup source IP), for which Assoluto — not the tenant — is controller. They later POST /app/admin/profile/delete and are redirected to /auth/login?notice=account_deleted, but only the User row is anonymised. platform_identities still holds their name, email and signup IP; authenticate_identity checks only `identity.is_active` and the password hash, so they can still log in at /platform/login and reach /platform/select-tenant. Conversely they can never sign up again with that address (DuplicateIdentityEmail). An emailed Art. 17 request has no route at all — only hand-written SQL, which produces no audit row and must mask the email to satisfy uq_platform_identities_email (exactly what the unused erase_identity already does correctly).

**Impact**

Art. 15/17/20 obligations toward the platform's own direct data subjects — the paying tenant owner, the one with an IP-address consent record — are unimplemented, while docs/audit-runs/2026-07-03-1507/findings.md records F-BE-003 as 'fixed (8b3ad5c)', so the founder believes it is closed. A user told their account is deleted still has a live, loginable account at the vendor.

**Recommendation**

Add /platform/profile with GET /platform/profile/export and POST /platform/profile/delete under require_identity, wiring the already-written export_for_identity / erase_identity and mirroring app/routers/me.py:184-260 (password re-confirmation, audit row, forced logout via clear_platform_session). In erase_identity also null `terms_accepted_ip`, `terms_accepted_version`, `email_verified_at`, `last_login_at`, and add terms_accepted_* to export_for_identity. Add tests mirroring tests/test_gdpr.py:279-360, correct the F-BE-003 status in the prior findings doc, and until the route exists change privacy.html §7 to say identity-level deletion is by email request.

---

## F-13 — Staff jump past QUOTED stamps a fabricated quoted_total of 0.00 on unpriced orders, and that 0.00 is what the customer-facing PDF prints

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: billing-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/order_service.py:646-647 (with app/services/pdf_service.py:340)`

> **Verification note.** order_service.py:646-647 recomputes into coalesce(...,0) when quoted_total IS NULL.

**Evidence**

```
order_service.py:646 `if dst >= _PIPELINE_RANK[OrderStatus.QUOTED] and order.quoted_total is None:` → `await _recompute_quoted_total(db, order)`, and `_recompute_quoted_total` (line 600) does `select(func.coalesce(func.sum(OrderItem.line_total), 0))` … `order.quoted_total = Decimal(total)`. With no priced items that is Decimal('0'), not NULL. pdf_service.py:340: `total_value = Decimal(order.quoted_total) if order.quoted_total is not None else subtotal`.
```

**Failure scenario**

Staff take an order over the phone — the workflow d65161b was built for — where the contact drafted line items with no unit prices (common: can_set_prices is off by default on the customer-create form, routers/customers.py:98). Staff click the CONFIRMED node on the stepper; `_backfill_milestones` sees dst(CONFIRMED)=3 >= rank(QUOTED)=2 with quoted_total NULL, recomputes over NULL line_totals, coalesces to 0. The detail card reads "Quoted (confirmed): 0 Kč" instead of "—", the CSV "Quoted total" column reads 0, and /app/orders/{id}.pdf — downloadable by the customer contact — prints "Subtotal 0 Kč". Items are no longer editable at CONFIRMED, so the only repair is walking the order back to QUOTED, firing another status email.

**Impact**

A confirmed commercial document stating a price of zero is sent to an EU B2B customer: disputes, unbillable orders, and a revenue figure of 0 in every export for orders created through the headline new workflow.

**Recommendation**

In `_backfill_milestones`, only adopt the recomputed value when it is meaningful — compute the sum first and leave `quoted_total` NULL when no line item has a `unit_price`, rather than coalescing to 0.

---

## F-14 — add_item never refreshes the cached quoted_total, so the PDF's Subtotal and the detail card silently disagree with the line items

- **Severity**: P1  ·  **Verification**: `confirmed` (via self)  ·  **Category**: billing-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/order_service.py:388-457 (add_item)`

> **Verification note.** _recompute_quoted_total is called at :480 (remove), :553 (update), :647, :697 — never in add_item.

**Evidence**

```
`remove_item` (line 480) and `update_item` (line 553) both call `await _recompute_quoted_total(db, order)`; `add_item` does not — it goes `db.add(item)` → `await db.flush()` → `audit_service.record(...)` → `return item` with no recompute between lines 426 and 457. pdf_service.py:340 then prefers the stale cache: `total_value = Decimal(order.quoted_total) if order.quoted_total is not None else subtotal`.
```

**Failure scenario**

An order sits in QUOTED with `quoted_total = 1000` (stamped by the QUOTED transition at order_service.py:697). Staff add a missing 500 CZK line via POST /app/orders/{id}/items — legal, since QUOTED is in STAFF_ITEM_EDIT_STATES. quoted_total stays 1000: the detail card and the CSV show 1000, while the PDF renders an item table summing to 1500 directly above a Subtotal row reading 1000. Nothing later repairs it — the QUOTED side-effect only fires when landing exactly on QUOTED and `_backfill_milestones` only fills when the value is NULL — so the wrong number follows the order to closure.

**Impact**

Under- or over-billing on real invoiced orders, plus a customer-visible PDF that contradicts itself — the exact document a B2B buyer forwards to their accounts department.

**Recommendation**

Call `await _recompute_quoted_total(db, order)` at the end of `add_item`, right after `await db.flush()`, mirroring `remove_item`/`update_item`. One line.

---

## F-15 — The app never refuses to boot with the public default APP_SECRET_KEY in APP_ENV=production

- **Severity**: P2  ·  **Verification**: `confirmed-mitigated` (via self)  ·  **Category**: secrets
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/config.py:36; app/main.py:274-402 (create_app has no secret-key assertion)`

> **Verification note.** Default is 'dev-insecure-secret-change-me' with no runtime guard, BUT docker-compose.prod.yml:60 asserts ${APP_SECRET_KEY:?}. Real risk is limited to non-compose deploys (self-hosters running uvicorn directly).

**Evidence**

```
app/config.py:36 `app_secret_key: str = Field(default="dev-insecure-secret-change-me", alias="APP_SECRET_KEY")`. Grepping app/main.py for guards returns only two `settings.is_production` branches — line 281 (hide /docs) and line 369 (Stripe demo-mode warning); neither checks the key. That key signs `read_session` (app/deps.py:263), platform sessions (app/platform/deps.py:62), invite tokens (public.py:395,574,636), password-reset tokens (public.py:742,802,856) and platform reset tokens (platform_auth.py:163,201). railway.toml:21 `startCommand = "uvicorn app.main:app --host 0.0.0.0 --port 8000"` with no assertion.
```

**Failure scenario**

docker-compose.prod.yml:53 asserts `APP_SECRET_KEY: ${APP_SECRET_KEY:?...}`, but that single compose assertion is the only guard and it does not cover (a) the Railway deploy path documented in railway.toml + docs/DEPLOY_RAILWAY.md, where a missing value silently falls through to the default, nor (b) an operator who copies the literal value from .env.example into /etc/assoluto/env. Either way the app starts in APP_ENV=production signing session cookies with a string published in this AGPL repo; an attacker mints itsdangerous-signed session cookies for any tenant_id and user_id, plus forged reset and staff-invitation tokens.

**Impact**

Complete authentication bypass and cross-tenant data access on any deployment that misses the one compose assertion. The default is public in a public repo, so exploitation requires zero discovery work.

**Recommendation**

In `create_app`, next to the existing `settings.is_production` blocks, add a hard `raise RuntimeError` when `settings.is_production` and (`settings.app_secret_key == "dev-insecure-secret-change-me"` or `len(...) < 32`). Fail closed. Add a test asserting `Settings(APP_ENV="production")` with the default key raises.

---

## F-16 — Single-use recovery tokens (password reset, staff invite, contact invite) are written verbatim into the uvicorn access log

- **Severity**: P2  ·  **Verification**: `not-substantiated` (via self)  ·  **Category**: privacy
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `Dockerfile:110 (no --no-access-log) + app/routers/public.py:751 (and :385, :560; platform_auth.py:192)`

> **Verification note.** I found no log call emitting a token/URL. invite_url/reset_url in email_tasks.py are template context, not logging. Treat as unproven unless the reporter can cite a concrete log line.

**Evidence**

```
public.py:751 builds the emailed link as a GET query parameter:
    f"{tenant_base_url(settings, tenant)}/auth/password-reset/confirm?token={reset_token}"
uvicorn's default LOGGING_CONFIG registers `"uvicorn.access"` with its own stdout StreamHandler and `propagate: False`, so `configure_logging`'s `logging.basicConfig(..., force=True)` (app/logging.py:20-27), which only clears root handlers, does not disable it. The logged request_line comes from uvicorn's `get_path_with_query_string(scope)`, which appends `scope["query_string"]` verbatim. Dockerfile:110 passes no `--no-access-log`, and railway.toml:21 likewise.
```

**Failure scenario**

A tenant admin invites a staff member; the invitee's browser hits `GET /invite/staff?token=<signed-token>` and uvicorn writes the full token to stdout, captured into the container's json-file log and (once LOG_JSON=true logs are shipped) into any aggregator. Anyone with read access to those logs — a contractor, a SaaS log vendor, a compromised aggregator account, or a log volume that lands in the pg dump directory — can replay an unconsumed staff-invitation token within its TTL to create a fully-privileged staff account, or replay a password-reset token to take over an account. The team already treats this data as toxic (`_safe_error_summary` at app/tasks/email_tasks.py:60-96 was hardened specifically to keep reset-token URLs out of structured logs).

**Impact**

Account takeover / privilege escalation for anyone with log-read access, without needing DB or mailbox access; also an unnecessary GDPR-relevant secondary store of authentication material.

**Recommendation**

Add `--no-access-log` to the Dockerfile CMD and railway.toml startCommand, and emit the access line from `LogContextMiddleware` with the query string dropped or redacted; or install a `logging.Filter` on the `uvicorn.access` logger that rewrites `token=…`. Best long-term fix is to move recovery tokens out of the query string (POST from a small interstitial form).

---

## F-17 — Thumbnail background task feeds attacker-controlled images to Pillow with the default 89M-pixel limit, so a 310 KB PNG allocates hundreds of MB in the web process

- **Severity**: P2  ·  **Verification**: `partial` (via self)  ·  **Category**: dos
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/tasks/thumbnail_tasks.py:35`

> **Verification note.** No MAX_IMAGE_PIXELS set before Image.open. Pillow's built-in default still raises DecompressionBombError above 2x the limit, so protection is partial, not absent.

**Evidence**

```
    if content_type.startswith("image/"):
        try:
            with Image.open(BytesIO(data)) as img:
                img.thumbnail(THUMBNAIL_MAX_SIZE)
No `Image.MAX_IMAGE_PIXELS` override and no `warnings.simplefilter("error", Image.DecompressionBombWarning)` anywhere in the tree; Pillow only *warns* between MAX_IMAGE_PIXELS and 2×, and the DecompressionBombError above 2× is swallowed by the surrounding `except Exception`. Measured with pinned Pillow 12.2.0:
    bomb png bytes: 310119 pixels: 100000000
    thumbnail OK bytes: 3127
    peak RSS MB: 414.671875
Neither docker-compose.yml nor docker-compose.prod.yml sets `mem_limit` / `deploy.resources`.
```

**Failure scenario**

Any customer contact with can_upload_files (default True) generates a solid-colour PNG at 13360×13360 = 178.4M pixels — just under Pillow's 2× hard error threshold — which compresses to a few hundred KB and passes both Caddy's 60MB cap and MAX_UPLOAD_SIZE_MB=50. POST /app/orders/{id}/attachments with Content-Type image/png (on the allow-list at attachment_service.py:29). `generate_thumbnail` runs in the web process; PNG has no draft() downscale path so the full RGB surface is materialised (~750 MB at that size, extrapolating the measured 415 MB at 100M pixels), while also blocking the event loop. A handful of concurrent uploads OOM-kills the container, taking down every tenant on the box.

**Impact**

A single low-privilege client contact of one tenant can exhaust memory in the shared web process and cause a full multi-tenant outage, for the cost of one small file upload, with no rate limit on the attachment endpoint and no distinguishing trace beyond a Python warning.

**Recommendation**

At module scope in app/tasks/thumbnail_tasks.py set `Image.MAX_IMAGE_PIXELS = 40_000_000` and `warnings.simplefilter("error", Image.DecompressionBombWarning)` so oversized images fall into the existing `except Exception` branch and are logged as `thumbnail.image_failed`; additionally check `img.size` right after the lazy `Image.open` and bail before `.thumbnail()`. Add `mem_limit` to the `web` service in docker-compose.prod.yml.

---

## F-18 — Order-item creation accepts any product_id in the tenant, ignoring the product's customer scope, can_use_catalog, and the can_set_prices guard

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: tenant-isolation
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/routers/orders.py:828-839`

> **Verification note.** select(Product).where(Product.id==product_uuid) has no customer-scope predicate, then backfills product.default_price.

**Evidence**

```
The permission gate only inspects the submitted price field:
    if not perms.can_add_items: raise HTTPException(403, ...)
    if unit_price.strip() and not perms.can_set_prices: raise HTTPException(403, ...)
and the product lookup that follows has no scope check at all:
    product = (await db.execute(select(Product).where(Product.id == product_uuid))).scalar_one_or_none()
    ...
    if price is None and product.default_price is not None: price = product.default_price
Compare app/services/product_service.py:47-51, the only place the customer scope is applied and only on the read path: `stmt = stmt.where(or_(Product.customer_id.is_(None), Product.customer_id == customer_id))`. `perms.can_use_catalog` appears nowhere in this handler — orders.py:592 uses it only to decide whether to render the picker.
```

**Failure scenario**

As a CustomerContact of customer A with can_use_catalog=False and can_set_prices=False, on my own DRAFT order, POST /app/orders/{my_order}/items with description="", unit_price="" and product_id=<uuid>. (a) With a shared product's UUID — contacts are handed these in plain HTML by the command palette, search_service.py builds href=/app/products/{p.id} for every result — the handler backfills description, unit and unit_price from the catalog, so the line lands with a price I may not set, using a catalog I may not use. (b) With the UUID of a product scoped to customer B, the same code copies B's SKU, product name, unit and default_price into my order and renders it on the order detail page. The only filter on the SELECT is RLS, which is tenant-wide, not customer-wide.

**Impact**

Two defects on one line: a same-tenant cross-customer disclosure of another client's private SKU, product name and negotiated unit price — exactly what the per-customer catalog exists to separate — and a straight bypass of both can_use_catalog and can_set_prices, the two flags a supplier sets to stop a client pricing their own order.

**Recommendation**

Constrain the lookup to the order's own customer: `select(Product).where(Product.id == product_uuid, or_(Product.customer_id.is_(None), Product.customer_id == order.customer_id), Product.is_active.is_(True))`, reject non-staff principals outright when `not perms.can_use_catalog`, and move the `can_set_prices` check below the product back-fill so it tests the resolved `price`, not the raw form string.

---

## F-19 — Third-party GitHub Actions are pinned to mutable tags, and the one holding the production VPS private key is a third-party action

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: ops
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `.github/workflows/deploy-production.yml:70 (also ci.yml:45,86; release.yml:45; deploy-production.yml:57)`

> **Verification note.** Every action is on a mutable tag, including appleboy/ssh-action@v1.2.0 which holds the production VPS SSH key.

**Evidence**

```
.github/workflows/deploy-production.yml:70-77:
      - name: Roll web service on VPS
        uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.DEPLOY_HOST }}
          username: ${{ secrets.DEPLOY_USER }}
          key: ${{ secrets.DEPLOY_SSH_KEY }}
ci.yml:45 `uses: astral-sh/setup-uv@v4`; deploy-production.yml:57 / release.yml:45 / ci.yml:86 `uses: docker/build-push-action@v6`. Every one is a floating tag, not a 40-char commit SHA.
```

**Failure scenario**

`v1.2.0` is a mutable git tag; the maintainer — or anyone who compromises that account, as in the tj-actions/changed-files incident — can force-push it to new code. The next push to `production` runs that code in a job handed DEPLOY_SSH_KEY, DEPLOY_HOST and DEPLOY_USER as inputs. One line of exfiltration ships the private key for the `deploy` account, which is in the `docker` group and owns /etc/assoluto/env (PORTAL_OWNER_PASSWORD, S3_SECRET_KEY, SMTP_PASSWORD and, once configured, STRIPE_SECRET_KEY).

**Impact**

Total production compromise — VPS, database, S3 bucket, mail sender and Stripe — via a dependency the repo does not control and the founder has no visibility into.

**Recommendation**

Pin every third-party action to a full commit SHA with the tag as a trailing comment, e.g. `uses: appleboy/ssh-action@2ead5e36573f08b82fbfce1504f1a4b05a647c6f # v1.2.0`. Do this at minimum for appleboy/ssh-action and astral-sh/setup-uv. Enable Dependabot for `github-actions`.

---

## F-20 — The platform session cookie has no version field, so a platform password reset (or "log out") revokes nothing — a captured cookie stays valid for 14 days

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: session
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/platform/session.py:26-38 (payload); app/platform/deps.py:56-73 (validation)`

> **Verification note.** No session_version in app/platform/session.py or the Identity model.

**Evidence**

```
The entire payload is two fields:
    def to_dict(self) -> dict[str, Any]: return {"iid": self.identity_id, "admin": self.is_platform_admin}
and `get_current_identity` validates only existence + is_active:
    identity = (await db.execute(select(Identity).where(Identity.id == identity_uuid))).scalar_one_or_none()
    if identity is None or not identity.is_active: return None
There is no `session_version` column on Identity and nothing compares one. `platform_logout` (platform_auth.py:112-118) only calls `clear_platform_session`; `reset_platform_password` changes only `password_hash`. Compare the tenant side: app/deps.py:288 and :305 both do `if …session_version != session_data.session_version: return None`, with bumps in `change_user_password` / `reset_password_with_token`. PLATFORM_MAX_AGE_SECONDS = 60*60*24*14.
```

**Failure scenario**

A tenant owner signs into /platform/login on a borrowed laptop and forgets to sign out; the cookie is scoped to `.assoluto.eu` and lives 14 days. Realising later, she resets her platform password from her own machine. Nothing happens to the other browser: its `sme_portal_platform` cookie still verifies, get_current_identity still loads the active Identity, and whoever sits down retains /platform/select-tenant, /platform/switch/{slug} into any tenant she belongs to, and /platform/billing/cancel-subscription for the rest of the 14 days. Her own explicit logout only deletes that browser's copy; an exfiltrated cookie value is unaffected.

**Impact**

Password reset — the action a user takes precisely because they suspect compromise — provides no containment at the platform layer. Blast radius is every tenant the identity can switch into plus the Stripe billing controls.

**Recommendation**

Add a `session_version integer not null default 0` column to `platform_identities` (new 100x migration), carry it in `PlatformSession.to_dict()` as `"v"`, compare it in `get_current_identity` before returning, and bump it in `reset_platform_password` and `platform_logout` — mirroring the tenant mechanism that already works.

---

## F-21 — Platform password reset accepts a password of any length — the 8-character minimum enforced everywhere else is missing on this one path

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: authn
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/service.py:134-141; router at app/platform/routers/platform_auth.py:223-268`

> **Verification note.** len(password)<8 appears 6x in auth_service.py (tenant flows) and 0x in platform_auth.py.

**Evidence**

```
`reset_platform_password` hashes whatever it is given: `identity.password_hash = hash_password(new_password)`. The only validation in `platform_password_reset_confirm_submit` is `if password != password_confirm`. Every sibling path checks: `parse_signup_form` (app/platform/validation.py:156) `if len(password) < 8: raise ...`; `reset_password_with_token` (auth_service.py:597); `accept_invitation`, `accept_staff_invite`, `change_user_password`, `change_contact_password`. Only `hash_password` objects, and only to the empty string.
```

**Failure scenario**

A tenant owner uses the platform forgot-password flow and types `a` into both password fields. The POST succeeds and their platform Identity — the principal holding billing and every TenantMembership — now has a one-character password. /platform/login is limited to 20 attempts per 15 minutes per IP with no per-account lockout, so a single host exhausts the single-character space in under an hour.

**Impact**

A user can silently downgrade the strength of the highest-privilege credential in the product, on the one flow where they are least likely to think about it, below the 8-character floor the signup form promises.

**Recommendation**

Add `if len(new_password) < 8: raise InvalidCredentials("password must be at least 8 characters")` at the top of `reset_platform_password` (service layer, so posting directly still hits it) and render that message in the confirm template instead of the generic catch-all.

---

## F-22 — The platform password-reset endpoint sends SMTP synchronously on the event loop, so one unauthenticated request stalls the whole worker for up to 10 seconds

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: ops
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/routers/platform_auth.py:171-178`

> **Verification note.** app/email/sender.py:152 uses blocking smtplib.SMTP inside the async path.

**Evidence**

```
`platform_password_reset_submit` calls the blocking sender directly inside the async handler:
        send_password_reset(sender, to=identity.email, tenant_name="Assoluto", ...)
`send_password_reset` is a plain `def` (app/tasks/email_tasks.py:263) ending in a synchronous `smtplib.SMTP(host, port, timeout=10)` (app/email/sender.py:152). Every other mail-sending route goes through `background_tasks.add_task(...)`, including the tenant twin of this endpoint (app/routers/public.py:785-793). This handler does not even declare a `BackgroundTasks` parameter.
```

**Failure scenario**

An attacker POSTs /platform/password-reset with the email of any real platform identity. The handler blocks the single asyncio event loop inside smtplib until Brevo answers or the 10-second timeout fires; during that time no other request in that uvicorn worker — tenant logins, order pages, Stripe webhooks — is served. The per-IP limiter allows 5 such requests per 15 minutes, so a few dozen cheap VPN exits keep the loop saturated. The same stall hits legitimately on every real platform password reset.

**Impact**

Availability: an unauthenticated endpoint converts a slow third-party SMTP hop into whole-application latency and gives a cheap loop-starvation primitive against a live production service.

**Recommendation**

Give `platform_password_reset_submit` a `background_tasks: BackgroundTasks` parameter and replace the direct call with `background_tasks.add_task(send_password_reset, sender, to=..., ...)`, matching `password_reset_request_submit` in app/routers/public.py. No commit-ordering concern applies — the task reads nothing from the DB.

---

## F-23 — Tenant login is an account-existence oracle: disabled accounts get a distinct 403 before any password check, and non-existent accounts skip Argon2 entirely

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: privacy
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/auth_service.py:121-122 and 158-160; responses at app/routers/public.py:246 and 265`

> **Verification note.** auth_service.py:121-123 raises AccountDisabled() before verify_password — a distinct outcome without knowing the password.

**Evidence**

```
In `authenticate()` the active-flag check precedes password verification, so no credential is required to trigger it:
        if not user.is_active: raise AccountDisabled()
        if not verify_password(password, user.password_hash): raise InvalidCredentials()
The router maps the two to visibly different responses — AccountDisabled → 403 with "Your account is disabled." (public.py:224-246), InvalidCredentials → 401 with "Invalid email or password." (public.py:247-265). Separately, when neither a User nor a CustomerContact matches, `authenticate` returns at auth_service.py:158-160 having called `verify_password` zero times, whereas any existing account costs one full Argon2id verification (t=3, m=64 MiB).
```

**Failure scenario**

(a) POST /auth/login with email=former.employee@firma.cz and password=x: a 403 body containing "Your account is disabled" proves that address is a real, deactivated account on this tenant; unknown addresses return 401. (b) For active accounts, time responses to a wrong password — hits take ~50-100 ms longer than misses because only hits reach Argon2. The limiter allows 20 attempts per 15 minutes per source IP (public.py:211) and there is no per-account throttle at all — EmailThrottle is applied to reset/invite/signup but never to login — so a handful of source IPs confirm a curated list of names in an afternoon.

**Impact**

Tells an outsider exactly which employees of a supplier's client have portal accounts and which have been offboarded — a targeted-phishing shopping list and a personal-data disclosure on an EU-hosted B2B system. The absence of any per-account login throttle also removes any ceiling on guesses against one known-good address.

**Recommendation**

Move the `is_active` check to AFTER `verify_password` for both the User and CustomerContact branch, and on the no-account-found path perform a dummy `verify_password(password, _DUMMY_HASH)` against a module-level constant so the timing profile matches. Return the same 401 body for AccountDisabled and log the distinction only to the security stream. Add a per-email login throttle alongside the per-IP limiter.

---

## F-24 — Invoice PDF back-computes 21 % Czech VAT out of every CZK invoice, including reverse-charge and foreign-VAT (OSS) invoices

- **Severity**: P2  ·  **Verification**: `partial` (via self)  ·  **Category**: billing-integrity
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/services/invoice_pdf_service.py:304-313; app/platform/billing/service.py:492-514 (record_paid_invoice)`

> **Verification note.** CZ_VAT_STANDARD=Decimal('0.21') is hardcoded, but a non-VAT-payer path exists and is documented. Reverse-charge/EU-VAT remains unhandled.

**Evidence**

```
    if supplier_is_vat and currency == "CZK":
        gross = Decimal(amount_total_cents) / Decimal(100)
        base_amount = (gross / (Decimal(1) + CZ_VAT_STANDARD)).quantize(Decimal("0.01"))
        vat_amount = (gross - base_amount).quantize(Decimal("0.01"))
The only condition is supplier-is-VAT-registered AND currency CZK — nothing about where the customer is. The tax Stripe actually computed is never persisted: `record_paid_invoice` stores only `amount_cents=int(data.get("amount_paid", 0))` and `currency`, discarding `invoice.total_tax_amounts` / `tax`. Yet checkout deliberately enables cross-border tax: `"automatic_tax": {"enabled": True}, "tax_id_collection": {"enabled": True}` with the comment "reverse-charge (0 %) for EU B2B with a valid DIČ" (app/platform/billing/service.py:410-419).
```

**Failure scenario**

A Slovak or German B2B customer enters a valid EU VAT ID at Stripe Checkout; Stripe Tax applies reverse charge, so the invoice total is 490,00 CZK with 0,00 tax. The in-app invoice PDF nevertheless prints základ 404,96 CZK, DPH 21 % = 85,04 CZK, celkem 490,00 CZK — VAT that was never charged and never remitted, with no "daň odvede zákazník / reverse charge" note (mandatory under §29 ZDPH). Mirror case: a German consumer charged 19 % German VAT via OSS still gets a PDF asserting 21 % CZ VAT on a different base.

**Impact**

A legally invalid tax document handed to EU customers: they may deduct input VAT that does not exist and the operator's VAT return will not reconcile with issued dokladů. Affects exactly the DE/EN audience the marketing site targets.

**Recommendation**

Persist Stripe's authoritative tax breakdown (`invoice.total_excluding_tax`, `invoice.tax` / `total_tax_amounts[].amount` and `.rate.percentage`, plus customer tax-exempt status) as new columns on `platform_invoices` in `record_paid_invoice`, and have `render_invoice_pdf` print those verbatim, with an explicit reverse-charge line when recorded tax is zero and the customer is a non-CZ EU business. Do not derive tax from the gross at all.

---

## F-25 — Documented production backup procedure ships unencrypted full-database dumps to an undisclosed subprocessor

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: privacy
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `docs/OPERATOR_PLAYBOOK.md:172-250 (§3); scripts/backup.sh:41-59; app/templates/www/privacy.html:71-101, :169`

> **Verification note.** BACKUP_GPG_RECIPIENT is optional; empty = unencrypted dump shipped off-site.

**Evidence**

```
OPERATOR_PLAYBOOK.md §3.4 instructs "Add to /etc/assoluto/env: RCLONE_REMOTE=b2:assoluto-backups", and §3.5's expected output is `[backup] Transferred: portal-20260423-215142.sql.gz` — a plain gzip, i.e. BACKUP_GPG_RECIPIENT unset. §3 never mentions BACKUP_GPG_RECIPIENT, while scripts/backup.sh:53 prints "(UNENCRYPTED — set BACKUP_GPG_RECIPIENT)" on that path. privacy.html's subprocessor table lists exactly four entities — Hetzner, Brevo, Stripe, Porkbun — and no backup/object-storage provider; §9 asserts "encrypted backups".
```

**Failure scenario**

The founder follows the repo's own playbook. Nightly cron gzips a complete pg_dump of every tenant's users, customer_contacts (name, email, phone), orders, comments and audit_events without GPG and rclone-syncs it to Backblaze B2 (a US company; Wasabi/Amsterdam is the other suggested option). A German B2B buyer performing routine subprocessor due diligence reads privacy.html §4, sees no backup provider, and is told in §10 that new subprocessors are announced 30 days in advance. Neither happened.

**Impact**

Art. 28(2)/(4) undisclosed subprocessor plus, if B2's US entity contracts, an undocumented third-country transfer with no SCC reference; and a direct contradiction of the "encrypted backups" claim in §9. Medium confidence because /etc/assoluto/env is not readable here — certain if the operator followed the playbook, moot if RCLONE_REMOTE is unset.

**Recommendation**

Confirm what is set on the VPS. If RCLONE_REMOTE is configured, add the provider as a row in the privacy.html §4 table with region and data categories, and set BACKUP_GPG_RECIPIENT so §9 is true. Amend OPERATOR_PLAYBOOK.md §3.4 to set BACKUP_GPG_RECIPIENT in the same step as RCLONE_REMOTE so the two cannot be configured apart.

---

## F-26 — No retention or purge job exists for any personal data — privacy.html promises deletion that no code performs

- **Severity**: P2  ·  **Verification**: `overstated` (via self)  ·  **Category**: privacy
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/tasks/periodic.py (whole file); app/templates/www/privacy.html:117,119; app/services/gdpr_service.py:200-210`

> **Verification note.** Cleanup jobs DO exist (cleanup_stale_invited_contacts, cleanup_old_stripe_events, expire_demo_trials). What is missing is retention for audit_events, closed orders and orphaned S3 objects. Rewrite the finding narrowly.

**Evidence**

```
periodic.py registers exactly six jobs: auto_close_delivered_orders, cleanup_stale_invited_contacts, cleanup_old_stripe_events, expire_demo_trials, enforce_canceled_subscriptions, send_trial_nurture_emails. The only one deleting personal data is cleanup_stale_invited_contacts (unaccepted invites, 14 d); enforce_canceled_subscriptions ends at `UPDATE tenants SET is_active = false` plus session_version bumps — nothing is deleted. privacy.html:117 promises "data remains recoverable on written request for 30 days after deactivation. After that, it is permanently deleted." and :119 promises 90-day retention for auth/security logs, yet auth.login audit rows are append-only and never pruned. erase_user's docstring claims it will "stamp ``deleted_at`` (audit marker; used by the cleanup job)" but there is no `deleted_at` column on User, erase_user never sets one, and no such job exists (grep for deleted_at/purge/retention over app/ and scripts/ returns only the Stripe-event pruner).
```

**Failure scenario**

A tenant cancels in month 3; the tenant is deactivated 3 days after period end. Day 33 arrives and privacy.html says the data is now permanently deleted. In reality every users, customer_contacts, orders, order_comments, audit_events row and S3 object is still there for the life of the database — there is no route (platform_admin.py has deactivate/reactivate but no delete), no script and no job that removes it. A subject-access request from an ex-customer's employee would have to be answered with "we still hold everything".

**Impact**

Art. 5(1)(e) storage-limitation breach plus a published-policy/reality mismatch that is trivially demonstrable by a complainant or auditor. Also unbounded cost and blast-radius growth: every dead tenant's PII stays in the primary DB and in every nightly dump.

**Recommendation**

Either implement the promises or restate them. Minimum viable: add a `purge_deactivated_tenants` job (owner engine, advisory lock, same shape as enforce_canceled_subscriptions) hard-deleting tenant rows and their S3 prefix N days after is_active flipped false, and a `purge_old_auth_events` job deleting audit_events where action IN ('auth.login','auth.logout') AND occurred_at < now() - 90 days. Fix the erase_user docstring's references to a nonexistent deleted_at column and cleanup job.

---

## F-27 — Art. 15/20 exports are materially incomplete and asymmetric — each principal type omits what the other includes

- **Severity**: P2  ·  **Verification**: `unverified` (via -)  ·  **Category**: privacy
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/gdpr_service.py:49-99 (export_for_user), :102-147 (export_for_contact)`

> **Verification note.** Not re-checked after the verifier agents died; export asymmetry claim stands on the finder's evidence alone.

**Evidence**

```
export_for_user returns `profile`, `orders_created`, `audit_events_authored` — it never queries OrderComment, so comments the staff member wrote are absent. export_for_contact returns `profile`, `customer`, `comments_authored` — it never queries Order or AuditEvent, so orders the contact submitted (`Order.created_by_contact_id`, a real column at app/models/order.py:78) and every audit event where they are the actor are absent. The docstring rationalises this as "their customer's orders belong to the customer, not the contact", while the same module treats `Order.created_by_user_id` as exportable personal data for staff. Both exports also omit OrderStatusHistory rows keyed to the subject (changed_by_user_id / changed_by_contact_id) and attachments they uploaded (uploaded_by_*_id).
```

**Failure scenario**

A purchasing manager (CustomerContact) submits 40 orders over a year, uploads drawings, and moves orders through statuses, then exercises Art. 15 at /app/me/profile/export. The JSON lists her profile fields and comments — nothing else. The controller in fact holds 40 rows linking her identity to specific orders, N attachment rows attributing uploads to her, and audit_events rows recording her logins and status changes, which are even visible to the supplier in the audit viewer. Her export gives her no way to know they exist or to challenge them.

**Impact**

Art. 15(1) requires disclosure of all personal data concerning the subject and Art. 20 requires portability. An incomplete export is the most commonly complained-about GDPR failure and is trivially provable by the subject once they see the audit viewer or an order page attributing an action to them.

**Recommendation**

In export_for_contact add `orders_created` (`Order.created_by_contact_id == contact.id`) and `audit_events_authored` (`AuditEvent.actor_id == contact.id`), mirroring export_for_user. In export_for_user add `comments_authored` (`OrderComment.author_user_id == user.id`). Add `status_changes` and `attachments_uploaded` to both. ~30 lines, no product decision.

---

## F-28 — Customer contacts can self-erase data the tenant is controller of, with no notification to the controller

- **Severity**: P2  ·  **Verification**: `unverified` (via -)  ·  **Category**: privacy
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/routers/me.py:216-261 (profile_delete); app/templates/www/privacy.html:25-27`

> **Verification note.** Not re-checked; self-erase-vs-controller claim stands on the finder's evidence alone.

**Evidence**

```
privacy.html §1: "For personal data that you (the Customer) upload about your own end clients into the portal … the Provider acts as a data processor under Article 28 GDPR; the Customer remains the controller of that data." Yet me.py:246-247 runs `await erase_contact(db, contact=contact)` — nulling email, full_name and phone on a row the tenant created via invite_customer_contact — on the data subject's direct instruction. The route's own comment says "There is no last-admin lockout here — contacts don't administer the tenant, so any contact may erase themselves at any time." No email task is scheduled to the tenant; the only trace is the `contact.gdpr_erased` audit row.
```

**Failure scenario**

A purchasing manager at client ACME leaves for a competitor and on her last day POSTs /app/me/profile/delete with her password. Her CustomerContact row loses name, email and phone. Supplier 4mex — the controller for that record — is never told; the next time they look for ACME's contact they find `<erased>` / erased-contact-<uuid>@erased.invalid. In-flight order notifications to that customer silently stop (_contact_recipients filters is_active, so build_order_status_changed returns None and transitions proceed with no email and no warning).

**Impact**

Legally, the Provider as processor executes a data-subject request against the controller's data without instruction, which Art. 28(3)(e) says it should instead assist with. Operationally, a supplier irreversibly loses a client contact record with zero notification — a support ticket that cannot be explained or undone.

**Recommendation**

Keep the self-service route but make the controller a participant: schedule an email to the tenant's active TENANT_ADMIN users on successful erasure (the notification_service/_staff_recipients + email_tasks pattern already exists), and reword privacy.html §1 or a DPA clause accordingly. For strict Art. 28 conformance, change the route to raise a request the tenant admin approves.

---

## F-29 — Customer edit form renders can_set_prices as OFF while enforcement treats a missing key as ON — staff see a lock that does not exist

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: authz
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/routers/customers.py:192`

> **Verification note.** customers.py:192 renders perms.get('can_set_prices', False); customer_permissions.py:31 enforces raw.get('can_set_prices', True). Opposite defaults on a '{}' row.

**Evidence**

```
customers.py:190-193 → `"can_add_items": "on" if perms.get("can_add_items", True) else ""`, `"can_use_catalog": ... True ...`, **`"can_set_prices": "on" if perms.get("can_set_prices", False) else ""`**, `"can_upload_files": ... True ...`. Enforcement uses the opposite default: app/services/customer_permissions.py:30 `can_set_prices=bool(raw.get("can_set_prices", True))`, and `from_dict` returns the all-True dataclass for an empty dict (`if not raw: return cls()`).
```

**Failure scenario**

Migration 0008 added `order_permissions JSONB NOT NULL DEFAULT '{}'::jsonb`, so every pre-existing customer — plus any created via `create_customer` without explicit perms (customer_service.py:124) — carries `{}`. A tenant admin opens /app/customers/{id}/edit, sees the "may set prices" checkbox UNCHECKED and concludes those contacts cannot price line items. In reality `OrderPermissions.from_dict({})` yields can_set_prices=True, so the guard at routers/orders.py:799 lets the contact POST a unit_price of their choosing. The mismatch also mutates state the other way: saving the edit form untouched writes `can_set_prices: False`, revoking a permission the admin never intended to change.

**Impact**

Staff hold a false belief about a live authorization state on the one permission that touches money, and an unrelated edit (renaming a customer) silently revokes contacts' pricing ability.

**Recommendation**

Make the defaults agree — either change customers.py:192 to `perms.get("can_set_prices", True)` or flip the dataclass default to False. Add an assertion test that the form prefill and `OrderPermissions.from_dict({})` agree for all four flags.

---

## F-30 — SLA dashboard is structurally dead: promised_delivery_at has no writer anywhere in the codebase

- **Severity**: P2  ·  **Verification**: `confirmed` (via self (independent))  ·  **Category**: data-integrity
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/services/sla_service.py:55-57; app/models/order.py:83 (column) vs app/routers/orders.py:391-445 (only writer of delivery dates)`

> **Verification note.** promised_delivery_at is read by the CSV, the PDF, detail.html and all of sla_service, and assigned nowhere in app/ — only in a test fixture. The SLA dashboard is dead on arrival for every tenant.

**Evidence**

```
Every SLA bucket is gated on the column:
    in_window = and_(Order.promised_delivery_at.is_not(None), Order.promised_delivery_at >= date_from, Order.promised_delivery_at <= date_to)
A repo-wide grep for `promised` outside .venv returns only the creating migration, the model declaration, three read sites (orders.py:342 CSV, templates/orders/detail.html:110, pdf_service.py:227), sla_service itself, and tests/test_orders_pdf.py:70 which sets it to None. There is no assignment on any route or service: `create_order` takes `requested_delivery_at` only (routers/orders.py:395), there is no order-edit route at all, and neither `transition_order` nor `_backfill_milestones` nor `update_item` touches it. The dashboard is nonetheless a first-class nav item — app/templates/app_base.html:50 `<a href="/app/admin/sla" ...>{{ _("SLA") }}</a>`.
```

**Failure scenario**

A tenant admin clicks SLA in the top nav on any live tenant. `on_time_rate` runs with `promised_delivery_at IS NOT NULL` as its first predicate, which matches zero rows for every tenant; summary returns {total: 0, on_time: 0, late: 0, pending: 0, rate: 0.0}, heatmap_data returns no cells, and the page renders 0.0% on-time plus "No orders with a promised delivery date yet." — permanently. The CSV export's "Promised delivery at" column is always blank and the order PDF prints an em-dash for the promised date on every job.

**Impact**

A whole reporting feature promoted in the primary staff navigation can never produce a number, and a tenant admin who reads '0.0%' as their actual on-time rate is being shown a false business metric. The empty state reads like "you have no data yet" rather than "this field is unreachable". It also means the delivered_at machinery has no consumer, so wrong stamps go unnoticed (see F-31).

**Recommendation**

Add the missing writer — a promised-delivery input on the staff side of orders/detail.html and/or the QUOTED/CONFIRMED transition path in `transition_order` — or, if the promised date is not a product commitment yet, remove the SLA nav entry (app_base.html:50) and the /app/admin/sla route until it is. Do not leave a nav-level feature rendering a fabricated 0.0%.

---

## F-31 — Backward and cancelling transitions never clear milestone stamps, and jumps fabricate them — delivered_at is invisible in the UI so it cannot be corrected

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: data-integrity
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/services/order_service.py:604-612 (_backfill_milestones) and :698-701 (forward stamp) vs app/services/sla_service.py:54-89`

> **Verification note.** No milestone field is ever set back to None anywhere in order_service.

**Evidence**

```
`_backfill_milestones`:
    if dst >= _PIPELINE_RANK[OrderStatus.SUBMITTED] and order.submitted_at is None: order.submitted_at = now
    if dst >= _PIPELINE_RANK[OrderStatus.QUOTED] and order.quoted_total is None: await _recompute_quoted_total(db, order)
    if dst >= _PIPELINE_RANK[OrderStatus.DELIVERED] and order.delivered_at is None: order.delivered_at = now.date()
and in `transition_order` the forward stamp is deliberately one-way:
    if to_status == OrderStatus.DELIVERED and order.delivered_at is None:
        # Stamp only once … we keep the original delivery date so SLA numbers remain stable.
        order.delivered_at = date.today()
No branch anywhere clears delivered_at, submitted_at or closed_at on a backward or CANCELLED move, and `grep -rn delivered_at app/templates app/services/pdf_service.py` returns nothing — the column is invisible in the UI and PDF. sla_service buckets purely on the stamp with no status predicate: `on_time_expr = case((and_(Order.delivered_at.is_not(None), Order.delivered_at <= Order.promised_delivery_at), 1), else_=0)`.
```

**Failure scenario**

STAFF_ALLOWED_TRANSITIONS is fully connected, so staff mis-click DELIVERED on an order actually IN_PRODUCTION; delivered_at = today. They immediately click back to IN_PRODUCTION — allowed, but the stamp is not cleared and no field anywhere shows it is set. The same happens implicitly on a legitimate jump: DRAFT → CLOSED (a cited use case) runs `_backfill_milestones` with dst = rank(CLOSED), stamping submitted_at, delivered_at = today and quoted_total = 0.00 on an order never submitted, never delivered and never quoted. DELIVERED → CANCELLED likewise keeps the delivery stamp. The order permanently claims a delivery date it never had, and the fill-blanks-only rule means no later transition can correct it.

**Impact**

Latent today only because promised_delivery_at is never written (F-30), so sla_service matches no rows. The moment the SLA feature is made to work, every mis-clicked or jumped order enters the on_time/late buckets with a fabricated delivery date and is excluded from the overdue bucket, over-reporting on-time delivery — and the operator has no UI to find or fix them. Fixing SLA therefore silently activates a corrupted dataset.

**Recommendation**

In `transition_order`, when `pipeline_rank(to_status) < pipeline_rank(previous)` (or the target is CANCELLED), clear the milestone stamps the new status invalidates — at minimum `delivered_at` and `closed_at` — and record the cleared values in the existing audit `after` payload alongside the `skipped` key. Alternatively/additionally add a status-aware predicate to sla_service so only orders currently at/after DELIVERED count. Surface `delivered_at` on the staff view of orders/detail.html so a wrong value is at least visible.

---

## F-32 — SLA dashboard counts cancelled orders as overdue forever and inflates heatmap totals — sla_service never filters on order status

- **Severity**: P2  ·  **Verification**: `confirmed-moot` (via self)  ·  **Category**: data-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/sla_service.py:54-95 and :161-177`

> **Verification note.** sla_service never filters on status, so CANCELLED counts. Currently masked by F-30 — it will surface the moment F-30 is fixed.

**Evidence**

```
sla_service.py:80-89 — `pending_expr = case((and_(Order.delivered_at.is_(None), Order.promised_delivery_at < today), 1), else_=0)`. The only WHERE clause is `in_window` at :54-58, filtering solely on promised_delivery_at. `grep -n 'status' app/services/sla_service.py` → no hit; the word OrderStatus does not appear in the module. Line 168 — `func.count(Order.id).label("total")` for the heatmap, again with no status filter. tests/test_sla_service.py builds only DRAFT / DELIVERED / IN_PRODUCTION orders; `grep -n 'CANCELLED' tests/test_sla_service.py` → no hit.
```

**Failure scenario**

A customer cancels order 2026-0042 on 10 July with promised_delivery_at = 2026-07-20. Staff move it to CANCELLED, which sets cancelled_at and correctly leaves delivered_at NULL (_backfill_milestones returns early for CANCELLED). From 21 July onward every load of /app/admin/sla evaluates `delivered_at IS NULL AND promised_delivery_at < today` as true and reports the cancelled order in the pending/overdue tile permanently, with no way to clear it. The same order increments `total` in its heatmap cell while contributing to neither on_time nor late, so cells read e.g. 'total 7, on-time 4, late 1'.

**Impact**

The on-time SLA report is the tenant-facing proof behind the product's '≥90 % on time' claim. A shop with normal cancellation volume sees a permanently growing phantom overdue backlog and heatmap arithmetic that does not add up, and cannot distinguish real late work from cancelled work.

**Recommendation**

Add `Order.status != OrderStatus.CANCELLED` to the `in_window` predicate in on_time_rate and to the heatmap WHERE clause (:171-174), document the choice in the module docstring's Semantics section, and add a test seeding a cancelled order with a past promised date asserting pending == 0.

---

## F-33 — Concurrent transitions on one order are not serialised: a double-click writes two history rows, two audit events and sends two emails

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: data-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/order_service.py:230-237 (get_order_for_principal) and :670-671 (transition_order guard)`

> **Verification note.** The only with_for_update in order_service is on Tenant for number allocation; transitions take no row lock.

**Evidence**

```
`get_order_for_principal`: `order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()` — no `.with_for_update()`. The only concurrency guard in `transition_order` is `if order.status == to_status: raise ForbiddenTransition("already in that status")`, evaluated against the in-session snapshot. The Order model (app/models/order.py:42-99) declares no `__mapper_args__ = {'version_id_col': ...}`, and app/db/session.py sets no isolation level (Postgres default READ COMMITTED).
```

**Failure scenario**

A staff member double-clicks the "Submit" node on the stepper (templates/orders/detail.html:30 and :330 are plain `<form method="post">` with no submit-disabling). Two POSTs to /app/orders/{id}/transitions/submitted are in flight: A SELECTs status=DRAFT, B SELECTs status=DRAFT before A commits (the row is not locked, so B is not blocked at read time). Both pass the guard, both INSERT an OrderStatusHistory row, both INSERT an `order.status_changed` audit event, both re-stamp submitted_at, both commit and both schedule `send_order_submitted`. B's UPDATE merely blocks on A's row lock and re-applies the same value.

**Impact**

Duplicate customer-facing notification emails and a status history/audit trail showing the same transition twice, undermining the audit log as evidence; the same pattern applies to any target, including a duplicated DELIVERED stamp path, and the bulk endpoint shares the code path.

**Recommendation**

Re-read the order under a lock inside the mutation path — add `.with_for_update()` to the SELECT in `get_order_for_principal` (or take the lock in `transition_order` before the status check) — so the second request sees the committed new status and correctly 409s.

---

## F-34 — Every scheduled job writes zero audit_events — including auto-close of orders, a hard DELETE of customer contact rows across all tenants, and tenant deactivation

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: audit-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/tasks/periodic.py:104-118 (auto_close_delivered_orders), :130-168 (cleanup_stale_invited_contacts), :266+ (enforce_canceled_subscriptions)`

> **Verification note.** 'actor' does not appear anywhere in app/tasks/periodic.py.

**Evidence**

```
`grep -n 'audit_service' app/tasks/periodic.py` returns nothing — the module never imports it. auto_close writes the status and a history row only:
    for order in rows:
        order.status = OrderStatus.CLOSED
        order.closed_at = current
        session.add(OrderStatusHistory(tenant_id=order.tenant_id, order_id=order.id, from_status=OrderStatus.DELIVERED, to_status=OrderStatus.CLOSED, note="auto-closed after 14 days"))
cleanup_stale_invited_contacts issues a raw `delete(CustomerContact).where(...)` on the owner engine (`_owner_engine()`, RLS bypassed) with INVITE_EXPIRY_DAYS = 14, recording only `log.info("periodic.cleanup_invites.done", removed=removed)`; enforce_canceled_subscriptions flips `tenants.is_active = false` and bumps session_version for every user and contact with no audit row. Every interactive path writes one (order_service.py:722 `audit_service.record(action="order.status_changed", ...)`), and /app/admin/audit reads `audit_events` exclusively via `list_events` — never order_status_history.
```

**Failure scenario**

A tenant admin invites a contact who never clicks the link; on day 15 the job hard-deletes the row across all tenants as the `portal` owner. On day 16 the admin asks 'where did Jan Novák go?' — /app/customers/{id} no longer lists him and /app/admin/audit contains nothing, because audit_events only ever gets rows from audit_service.record. Same for auto-close (the only trace is a status-history row with changed_by_user_id and changed_by_contact_id both NULL) and for tenant deactivation, where every session is force-expired with no record the tenant can see.

**Impact**

Row destruction, order state changes and tenant deactivation happen with no tenant-visible trail, undercutting the audit log as the single answer to 'who changed this' and GDPR Art. 30 record-keeping; 'a contact disappeared' is unanswerable without psql. tests/test_notifications_flow.py:356 covers the job's row selection but cannot catch a missing audit row.

**Recommendation**

Import app.services.audit_service in app/tasks/periodic.py and record one event per affected tenant with SYSTEM_ACTOR — `order.status_changed`/`order.auto_closed` (before={'status':'delivered'}, after={'status':'closed','reason':'auto_close_14d'}), `contact.invite_expired` (deleted ids/emails in `before`), `tenant.deactivated_for_nonpayment`. The jobs run as owner and audit_events carries FORCE ROW LEVEL SECURITY, so pass `tenant_id=` explicitly (as app/platform/routers/platform_admin.py:656 does) or set `app.tenant_id` per row. Add audit-row assertions to the existing periodic-job tests.

---

## F-35 — Bulk order status changes are recorded in the audit log as actor 'system' — the staff member who made them is not attributable

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: audit-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/services/order_service.py:775`

> **Verification note.** order_service records `actor=audit_actor or SYSTEM_ACTOR`; when the bulk caller omits audit_actor the row is attributed to 'system'.

**Evidence**

```
app/services/order_service.py:775 — `await transition_order(db, order=order, to_status=to_status, actor=actor)` — the `audit_actor` keyword is omitted. Inside transition_order the audit write at :728 is `actor=audit_actor or SYSTEM_ACTOR`, and app/services/audit_service.py:54 defines `SYSTEM_ACTOR = ActorInfo(type="system", id=None, label="system")`. The single-order path does it correctly — app/routers/orders.py:1226-1232 passes `audit_actor=actor_from_principal(principal)`. tests/test_orders_bulk_transition.py has three tests and none contains the string 'audit'.
```

**Failure scenario**

A staff member selects 40 orders on /app/orders, picks 'Cancelled' and submits to POST /app/orders/bulk/transition. bulk_transition loops through transition_order without audit_actor, so 40 rows land in audit_events with actor_type='system', actor_id=NULL, actor_label='system'. The tenant admin later opens /app/admin/audit to find out who mass-cancelled a month of work and sees 40 rows authored by 'system' — indistinguishable from the nightly auto-close job. The per-order order_status_history rows do carry changed_by_user_id, so the information is recoverable only via psql.

**Impact**

The audit log — the artefact the product sells as accountability and the one an EU customer would rely on in a dispute or an Art. 15 request — silently loses operator attribution for the highest-blast-radius action in the app (mass status change including mass cancellation). One missing keyword argument, invisible to the test suite.

**Recommendation**

Thread an `audit_actor: ActorInfo | None` parameter through `bulk_transition` and pass it to transition_order at line 775; have `orders_bulk_transition` supply `actor_from_principal(principal)`. Add an `actor_type == 'user'` assertion to tests/test_orders_bulk_transition.py.

---

## F-36 — There is no error tracking or alerting of any kind: unhandled 500s, permanently failed emails and S3 failures are invisible until a customer complains

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: ops
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/main.py:514-521; app/logging.py:13-47; .github/workflows/uptime.yml:30-40`

> **Verification note.** No sentry/rollbar/bugsnag/opentelemetry anywhere in app/ or pyproject.toml.

**Evidence**

```
`grep -rni sentry app/` → zero hits, while docs/LAUNCH_CHECKLIST.md:147 carries the unticked box '- [ ] Set up Sentry for error tracking (SENTRY_DSN env var)' and docs/DEPLOY_SAAS.md:225 documents a SENTRY_DSN that app/config.py never reads. app/main.py:515 — the catch-all is `get_logger("app.errors").error("unhandled", path=..., error=...)` and nothing consumes that stream: app/logging.py:45 uses `structlog.PrintLoggerFactory()` straight to stdout, and neither compose file declares a `logging:` driver or ships logs anywhere. The only alerting is uptime.yml, which every 15 minutes curls four URLs (/healthz, /readyz, / and 4mex.assoluto.eu/auth/login) and relies on GitHub's default failed-workflow email; /healthz returns a hard-coded `{"status": "ok"}` and /readyz pings the DB only.
```

**Failure scenario**

SMTP is the named blind spot and the codebase records the precedent (app/main.py:257-259: 'we caught ourselves once with prod pointing at Brevo … on port 1025 … 31 mails in 24h timed out before the next operator noticed'). `_safe_send` in app/tasks/email_tasks.py retries three times, then `log.error("email.failed", ...)` and returns — it is a BackgroundTask, so nothing propagates, no row is written, no counter increments. A Brevo credential rotation on a Saturday drops every invitation, password-reset and order-status notification while /healthz, /readyz, / and /auth/login all return 200 and the uptime workflow stays green; the founder finds out Monday. The same holds for a POST-only 500 and for S3 upload failure.

**Impact**

Mean-time-to-detection for anything short of total process death is 'whenever a customer complains'. Silent loss of password-reset and invitation email locks users out with no signal at all.

**Recommendation**

(1) Add a `sentry_dsn` field to app/config.py and initialise sentry-sdk with the FastAPI integration in create_app — it captures the app/main.py:514 handler path plus uvicorn tracebacks, and the env var is already documented. (2) Have `_safe_send` capture to Sentry on final failure, and extend /readyz with an S3 head_bucket check so the existing uptime workflow covers storage. Add `logging: driver: json-file` with size/rotation options to docker-compose.prod.yml.

---

## F-37 — LOG_LEVEL is hard-coded in the base compose environment and not re-declared in the prod overlay — the operator's value is silently shadowed (third instance of the CLAUDE.md §15 trap)

- **Severity**: P2  ·  **Verification**: `confirmed-minor` (via self)  ·  **Category**: ops
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `docker-compose.yml:91 vs docker-compose.prod.yml:104-112`

> **Verification note.** Base compose sets LOG_LEVEL: INFO; the prod overlay only names it in a comment, so the base value wins per CLAUDE.md §15.

**Evidence**

```
docker-compose.yml:91-92 (base, web `environment:`):
      LOG_LEVEL: INFO
      LOG_JSON: "false"
docker-compose.prod.yml re-declares LOG_JSON (line 50) but never LOG_LEVEL, while the overlay's own comment at :104-112 asserts the opposite: "Everything else flows through ``env_file`` above: … LOG_LEVEL, MAX_UPLOAD_SIZE_MB, DEFAULT_TENANT_SLUG — all automatic." This is exactly the merge semantics CLAUDE.md §15 documents and that already bit the project twice (SMTP_PORT, comment at prod.yml:78-86 "Caught in prod 2026-04"; S3_PUBLIC_ENDPOINT_URL, commit 8e16fcb).
```

**Failure scenario**

During an incident the founder sets `LOG_LEVEL=DEBUG` in /etc/assoluto/env and runs `docker compose up -d web`, then waits for detail that never arrives — the base compose's `LOG_LEVEL: INFO` in `environment:` wins over env_file, so configure_logging still builds a bound logger at INFO and every `log.debug(...)` is dropped. Incident time is burned debugging the logging config, and the overlay's comment actively misleads. Setting `LOG_LEVEL=WARNING` to quiet a noisy log bill likewise has no effect.

**Impact**

The operator loses the ability to change log verbosity without a repo change and redeploy — exactly when they need it most. The file's documentation states the opposite of the behaviour, so the failure is silent and self-concealing.

**Recommendation**

Add `LOG_LEVEL: ${LOG_LEVEL:-INFO}` to the `web` `environment:` block in docker-compose.prod.yml alongside the SMTP_PORT / S3_PUBLIC_ENDPOINT_URL passthroughs and remove LOG_LEVEL from the "flows through env_file" comment. Better: add a CI check diffing the key set of base `web.environment` against the overlay's and failing on any key present in the base but absent from the overlay.

---

## F-38 — The manual subscription editor — the endpoint that grants free plan time and marks tenants paid — has zero tests and 500s on a malformed quick_action

- **Severity**: P2  ·  **Verification**: `unverified` (via -)  ·  **Category**: billing-integrity
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/platform/routers/platform_admin.py:407 (GET) and :456-560 (POST subscription_edit); crash at :535-537`

> **Verification note.** Not re-checked; manual subscription-editor audit claim stands on the finder's evidence alone.

**Evidence**

```
`grep -rn 'subscription_edit|start_trial|/subscription' tests/*.py` → zero hits for this route; the three files matching 'subscription' exercise only Stripe-driven paths. The handler performs unguarded revenue-affecting mutations: `sub.status = "active"` with `sub.trial_ends_at = None` (:551-556), `pin_internal` setting both dates to `datetime(2099, 1, 1, tzinfo=UTC)` (:545-549), and unbounded trial extension. The crash is at :535-537 — `if quick_action.startswith("extend_trial:"): days = int(quick_action.split(":", 1)[1])` with no try/except; the template only ever emits the three safe values (subscription_edit.html:61-63).
```

**Failure scenario**

(a) Regression exposure: nothing pins the Stripe-managed refusal at :514-521, the start_trial branch at :495-502, or the audit row the handler should write under the target tenant. A refactor dropping the `if sub.stripe_subscription_id:` guard would let an operator hand-edit a Stripe-managed subscription whose state the next webhook silently overwrites — a tenant believing it is on Pro while Stripe bills Starter, with no test failing. (b) Crash: a platform admin POSTs `quick_action=extend_trial:x`; `int("x")` raises ValueError, falls through to app/main.py:514 and returns a 500 — which, per F-36, alerts nobody.

**Impact**

This is the surface where money is given away or granted (trial extension, pin-to-2099, mark-active-without-Stripe). It is the least-tested revenue-affecting endpoint in the codebase and its most dangerous invariant is enforced only by an untested early return.

**Recommendation**

Add tests/test_platform_admin_subscription.py covering start_trial on a tenant with no subscription, refusal when stripe_subscription_id is set, each quick_action's resulting status/dates, and the audit row written under the target tenant. Wrap the `int()` at :536 in try/except ValueError and redirect with an error flash, matching the pattern at :498-500.

---

## F-39 — Every error string in the platform signup, login, verification and billing flows is hard-coded Czech — the i18n guard only scans templates

- **Severity**: P2  ·  **Verification**: `confirmed` (via self)  ·  **Category**: i18n
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/platform/routers/signup.py:162,195,207,317,328,342,356,374; app/platform/routers/platform_auth.py:61,83,85,208,239; app/platform/routers/billing.py:451,456,461,470,515; app/platform/deps.py:99`

> **Verification note.** `_t(request` count: signup.py 0, platform_auth.py 0, versus orders.py 27.

**Evidence**

```
A scan of app/**/*.py for literals containing Czech diacritics outside any `_t(` or `_(` wrapper finds 86 lines, all under app/platform/. Verbatim: signup.py:195 `"Tato subdoména je již obsazená."`; signup.py:207 `"Účet s tímto e-mailem již existuje. Použijte přihlášení."`; platform_auth.py:85 `"Neplatný e-mail nebo heslo."`; platform_auth.py:239 `"Hesla se neshodují."`; billing.py:456 `"IČO musí být přesně 8 číslic."`; deps.py:99 `"E-mail není ověřen…"`. The guard that should catch this — tests/test_template_i18n_coverage.py:215 test_template_has_no_untranslated_czech — is parametrised over `_collect_templates()` only and never opens a .py file. Live: `curl -H 'Accept-Language: en' https://assoluto.eu/platform/signup` → `<title>Create portal · Assoluto</title>`; `curl -H 'Accept-Language: de' https://assoluto.eu/pricing` → `<title>Preise · Assoluto</title>`.
```

**Failure scenario**

A German prospect lands on the DE pricing page, clicks through to /platform/signup which renders in English, and types a taken subdomain. signup.py:195 renders the form back with a Czech sentence inside an English page and no indication of what went wrong. Same for a wrong password at /platform/login, an expired verification link, and every billing-details validation error. The 518-test suite stays green because no test asserts on the language of a Python-side error and the only Czech-detection test walks templates.

**Impact**

This is the top-of-funnel conversion path for a product marketing to EU customers in three languages, and the failure is concentrated exactly on the error paths where a confused prospect most needs to understand what happened. It also negates the effort already spent translating the platform templates.

**Recommendation**

Wrap the user-facing subset in `_t(request, "English msgid")` (the request is already in scope in every listed handler) and add CS/DE msgstrs per the CLAUDE.md §7 extract/update/compile workflow, prioritising signup.py, platform_auth.py, billing.py and deps.py:99 (platform_admin.py strings face only the Czech operator). Extend tests/test_template_i18n_coverage.py with a parametrised scan over app/**/*.py flagging Czech diacritics in literals not passed to `_t(`.

---

## F-40 — Logout is cookie-deletion only — a session token that has already been copied stays valid for the rest of its 14-day life

- **Severity**: P3  ·  **Verification**: `confirmed` (via self)  ·  **Category**: session
- **Auto-fixable**: False  ·  **Known from a prior run**: False
- **Location**: `app/routers/public.py:318-322`

> **Verification note.** public.py:319-322 logout only calls clear_session; no session_version bump.

**Evidence**

```
    @router.post("/auth/logout")
    async def logout(request: Request) -> Response:
        response = RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
        clear_session(response)
        return response
No DB session at all and no `session_version` bump, although the mechanism exists and is used everywhere else revocation is needed (`change_user_password`, `reset_password_with_token`, `erase_user`, `deactivate_tenant` at app/platform/service.py:327-337). `read_session` accepts a 14-day-old signature (DEFAULT_MAX_AGE_SECONDS = 60*60*24*14) and `get_current_principal` only compares the version to the row, which logout never changed.
```

**Failure scenario**

A staff user signs in from a kiosk or a machine with a monitoring/backup agent that captures cookie jars, then clicks "Odhlásit". The Set-Cookie deletion applies to that browser only; the signed token value is still valid. Anyone replaying it from a different machine, hours or days later, is transparently authenticated as that user until the 14-day max_age lapses or something unrelated bumps session_version.

**Impact**

Logout gives a weaker guarantee than users reasonably assume. Blast radius is bounded (an attacker must already have the cookie value, which is HttpOnly + Secure + SameSite=lax + host-only), but it is the one revocation lever a worried user actually reaches for and it does nothing server-side.

**Recommendation**

Have `logout` take `principal: Principal = Depends(require_login)` and `db: AsyncSession = Depends(get_db)`, bump `principal.raw.session_version`, commit, then clear the cookie — the same three lines `change_user_password` already runs. Note this signs the user out of all devices; the alternative is a server-side revoked-token set.

---

## F-41 — Upload body is fully read into memory before the size limit is checked, so a rejected 50 MB upload still costs 50 MB of RSS

- **Severity**: P3  ·  **Verification**: `confirmed` (via self)  ·  **Category**: dos
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/routers/attachments.py:97`

> **Verification note.** No streaming/threadpool on the upload path.

**Evidence**

```
    # Read body into memory (MVP). …
    data = await file.read()
    if not data: raise HTTPException(status_code=400, detail="Empty file")
The size check happens ~20 lines later inside the service, after the bytes are resident: `max_size_bytes=settings.max_upload_size_bytes` (attachments.py:117) → `if size_bytes > max_size_bytes: raise AttachmentTooLarge(...)` (attachment_service.py:93). Starlette's UploadFile spools past 1 MB to a temp file, so `.read()` pulls the whole object back into RAM. MAX_UPLOAD_SIZE_MB defaults to 50 (app/config.py:159) and Caddy allows 60 MB (docker/Caddyfile).
```

**Failure scenario**

An authenticated contact POSTs a 55 MB file to /app/orders/{id}/attachments. Caddy passes it; `await file.read()` materialises all 55 MB in the web process; only afterwards does create_attachment_row raise AttachmentTooLarge → 413. The client burned one request, the server 55 MB. Issued in parallel with no rate limit on the endpoint, this multiplies — the same memory pressure that makes the Pillow bomb (F-17) lethal, reached without needing a valid image.

**Impact**

Amplifies any memory-pressure attack on the shared web container and wastes bandwidth/RAM on uploads guaranteed to be rejected. Bounded by Caddy's 60 MB cap and request concurrency, so hygiene rather than an outage by itself — but it removes the cheapest defence against F-17.

**Recommendation**

Reject before reading: compare `int(request.headers.get("content-length", 0))` against `settings.max_upload_size_bytes` and raise 413 immediately, then read in chunks with a running byte counter that aborts once the cap is exceeded. Tighten the Caddy `max_size` to match MAX_UPLOAD_SIZE_MB so the two limits cannot drift.

---

## F-42 — No Cache-Control header on any response, so authenticated HTML is browser-cacheable and survives logout

- **Severity**: P3  ·  **Verification**: `confirmed` (via self)  ·  **Category**: privacy
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/security/headers.py:71-73 (SecurityHeadersMiddleware._extra_headers)`

> **Verification note.** No Cache-Control is set in headers.py or main.py.

**Evidence**

```
The middleware adds exactly one header:
        self._extra_headers: list[tuple[bytes, bytes]] = [(b"content-security-policy", _build_csp(subdomain_apex))]
`grep -rn "Cache-Control\|cache_control\|no-store" app/ --include="*.py"` returns nothing. docker/Caddyfile's `header { … }` block sets HSTS, X-Frame-Options, nosniff, Referrer-Policy and Permissions-Policy but no Cache-Control. Confirmed live: `curl -sI https://4mex.assoluto.eu/auth/login` returns no `cache-control`, `expires`, `etag` or `last-modified`.
```

**Failure scenario**

A tenant staff user views /app/orders/{id} on a shared workstation and logs out. With no Cache-Control, ETag or Last-Modified the browser stored the response in the disk cache and, on a back-button / bfcache navigation, re-renders the cached HTML containing the customer's order lines, prices and contact details without revalidating. The next person at that machine presses Back and reads another company's commercial data. Medium confidence: only anonymous pages plus the middleware source could be verified, but nothing in the /app/* code path adds cache headers either.

**Impact**

Post-logout disclosure of B2B order/pricing data on shared machines; also means any future CDN or corporate proxy may heuristically cache authenticated HTML.

**Recommendation**

In `SecurityHeadersMiddleware.__call__`'s send_wrapper, add `cache-control: no-store, private` (and `pragma: no-cache`) to responses whose path is not under `/static`, which StaticFiles already stamps with its own validators.

---

## F-43 — Cookies policy does not match what the app sets — a listed cookie does not exist, and the locale cookie is not host-only

- **Severity**: P3  ·  **Verification**: `unverified` (via -)  ·  **Category**: privacy
- **Auto-fixable**: True  ·  **Known from a prior run**: False
- **Location**: `app/templates/www/cookies.html:55-64; app/routers/public.py:368-378; app/static/js/app.js:142-186`

> **Verification note.** Not re-checked; cookie-policy mismatch stands on the finder's evidence alone.

**Evidence**

```
cookies.html:61 lists `sme_theme` — "Light / dark / system preference … 1 year. Host-only". No such cookie is ever set: `grep set_cookie app/` yields only session.py:140, platform/session.py:70, public.py:370 and the CSRF middleware, and app/static/js/app.js:158-159 stores the theme in `localStorage.setItem("theme", mode)`, which the policy never mentions. cookies.html:55-58 describes `sme_locale` as "Host-only" but public.py:377 passes `domain=cookie_domain or None`. Verified live: `curl -s -o /dev/null -D - "https://assoluto.eu/set-lang?lang=en"` returns `set-cookie: sme_locale=en; Domain=.assoluto.eu; Max-Age=31536000; Path=/; SameSite=lax; Secure`.
```

**Failure scenario**

A German prospect's DPO reviews the cookies policy before signing, opens devtools, and finds (a) a cookie the policy says exists but does not, (b) the locale cookie scoped to the whole apex rather than host-only as claimed, so it is transmitted to every tenant subdomain, and (c) browser storage (localStorage `theme`) used but undisclosed, even though ePrivacy Art. 5(3) covers any storage on terminal equipment.

**Impact**

No user harm and no consent violation — the stored items really are strictly necessary. The damage is credibility: an inaccurate cookies table on a product sold on its GDPR posture, spotted by exactly the kind of buyer this product targets.

**Recommendation**

In app/templates/www/cookies.html delete the `sme_theme` row, add a line under §2 noting the theme preference is kept in browser localStorage (key `theme`), and change the `sme_locale` scope cell from "Host-only" to "assoluto.eu and subdomains". Alternatively drop the `domain=` argument at public.py:377 if the locale should not follow the user across tenants.

---

## Area verdicts (what each lens checked and how it rated the area)

### tenant-isolation

Core tenant isolation is genuinely solid and I could not break it. All twelve tenant-scoped tables carry ENABLE ROW LEVEL SECURITY plus a tenant_isolation policy with both USING and WITH CHECK (verified live against the local portal DB via pg_policies/pg_class, not just from migration source), and every route a CustomerContact can reach adds the second, application-level customer_id check — I traced orders, items, comments, attachments (download/thumbnail/delete), assets, CSV export, PDF export, the HTMX item-patch fragment, the command-palette search fragment, and the audit/activity feeds, and all scope correctly. Where I did find problems is one layer up: the platform billing surface silently reuses a support-access grant as "the tenant I manage", which is a money-mutating cross-tenant action (F1, P1), plus defects in the newest orders commit (F2–F4). What I could NOT check: I ran no Postgres-backed tests (shared DB, per instructions), so findings are code- and psql-derived rather than executed end-to-end; I had no tenant or platform-admin credentials, so nothing under /app/* or /platform/* was exercised authenticated against production; and I could not confirm whether the production portal role is a container superuser (locally it holds BYPASSRLS, which makes the FORCE ROW LEVEL SECURITY on audit_events decorative for owner sessions).

Checked and found sound:

- RLS coverage: queried pg_policies + pg_class on the live local DB — all 12 tenant-scoped tables (users, customers, customer_contacts, orders, order_items, order_status_history, order_comments, order_attachments, products, assets, asset_movements, audit_events) have relrowsecurity=t and a tenant_isolation policy with both USING and WITH CHECK on tenant_id = current_setting('app.tenant_id',true)::uuid. No tenant-scoped model is missing a policy.
- Owner-role periodic jobs (app/tasks/periodic.py): auto_close, invite cleanup, stripe-event prune, expire_demo_trials, enforce_canceled_subscriptions and the trial-nurture loop all act per-row or per-tenant; the nurture loop opens a per-tenant session and emails only that tenant's TENANT_ADMIN users. No cross-tenant join feeding a single email.
- IDOR sweep on every {id} route: get_order_for_principal raises OrderAccessDenied when order.customer_id != actor.customer_id, and orders detail/pdf/items/patch/delete/transition/comments plus attachments download/thumbnail/delete all route through it; assets_detail has an explicit asset.customer_id != principal.customer_id check; customers routes are all require_tenant_staff and _get_contact joins contact_id AND customer_id.
- search_service.global_search: contacts get Order.customer_id == their own, no customer-name matching, empty customers section, and search_products(customer_id=<own>) so only shared + own-company products appear.
- audit_service._apply_principal_scope: contacts are restricted to entity_type='order' events whose entity_id is in their own customer's orders; a contact with customer_id NULL gets where(false()).
- customer_permissions server-side enforcement exists on the mutating paths for can_add_items, can_upload_files, and the submitted unit_price field (orders.py:797-801, attachments.py:79-92) — not UI-only.
- tenant_admin.py: every one of the 17 routes declares require_tenant_staff, and the privileged ones (users CRUD, tenant-settings) additionally call _require_tenant_admin(principal).
- Recovery-token single-use: password reset embeds session_version and rejects on mismatch; invite accept checks accepted_at/password_hash; both invite and reset compare the token's tenant_id against the resolved tenant and 400 on mismatch.
- Session/tenant binding: the only read_session caller is deps.get_current_principal, which immediately compares session_data.tenant_id to tenant.id; all three public pages use read_session_for_tenant + cookie_mismatches_tenant per CLAUDE.md §13.
- Platform switch flow: /platform/switch and /platform/complete-switch re-fetch the membership server-side by id, re-check tenant match, reject deactivated targets, verify the handoff token's identity claim against the cookie holder, and cap token age at 60s.
- Support-access revoke is effective: revoke_platform_admin_support_access deletes the TenantMembership and sets user.is_active=False, which get_current_principal rejects on the next request, so an in-flight tenant cookie dies immediately.
- Platform admin /dashboard exposes only aggregates (tenant counts, sub counts, MRR sums, recent tenant names) — no per-tenant business content.

### authn-session

The tenant-side auth core is in good shape: Argon2id with rehash-on-login, a signed session cookie whose `max_age` IS enforced at load time, per-request re-validation of `is_active` + `session_version`, correct `read_session_for_tenant` usage on all three public pages, purpose-salted tokens (so a reset token cannot be replayed at an invite endpoint), single-use guards on tenant password-reset + both invitation flows, and `verify_csrf` wired as a router-level dependency on every one of the 13 routers that accept mutating requests (constant-time compare, HTMX header wired in app.js, Stripe webhook exempt but signature-verified). The weak half is the platform (`app/platform/`) layer, which is newer and demonstrably held to a lower standard than the tenant layer it mirrors: its password-reset token has none of the single-use binding CLAUDE.md §14 claims for "password reset", it enforces no minimum password length, its session cookie has no version field so nothing can revoke it, and it sends SMTP synchronously on the event loop. Separately, the cross-layer `_has_unverified_identity` login gate lets any unauthenticated stranger lock a named tenant staff member out of their portal by signing up with that person's email. I could not exercise anything dynamically — the live-site rule is GET/HEAD only and the postgres test suite was off-limits — so every finding below is derived from reading the code paths end to end; the timing-oracle claim in F5 in particular is proven structurally (Argon2 is skipped entirely on the no-account branch) rather than measured.

Checked and found sound:

- app/security/session.py — `read_session` passes `max_age=DEFAULT_MAX_AGE_SECONDS` (14 d) to `loads()`, so the cookie is NOT eternal; `write_session` sets httponly + samesite=lax + `secure=settings.is_production` + path=/ and no Domain (host-only), which is what makes cross-subdomain tenant leakage impossible in a correct browser
- app/deps.py:263-320 — `get_current_principal` re-reads the row every request and rejects on `not is_active` or `session_version != cookie.v`; both User and CustomerContact branches, so admin-disable and password-change revocation actually work
- app/routers/public.py:158/187/701 — landing, /auth/login and /auth/password-reset all use `read_session_for_tenant` + `cookie_mismatches_tenant`/`clear_session`, matching CLAUDE.md §13; no caller of bare `read_session` outside deps.py
- app/security/tokens.py:38-51 — every flow has its own itsdangerous salt (INVITE / STAFF_INVITE / PASSWORD_RESET / EMAIL_VERIFY / PLATFORM_PASSWORD_RESET / PLATFORM_TENANT_HANDOFF), so cross-purpose replay is cryptographically blocked, and `verify_token` always passes an explicit `max_age_seconds`
- Tenant single-use guards verified in code, not trusted: `reset_password_with_token` rejects `row.session_version != token_session_version` then bumps (auth_service.py:604-621); `accept_invitation` rejects `accepted_at is not None`; `accept_staff_invite` rejects `password_hash is not None`. Tenant tokens also bind tenant_id and principal type, and every router re-checks `tenant_id != tenant.id`
- CSRF: enumerated all 13 `APIRouter(` sites — every router accepting POST carries `dependencies=[Depends(verify_csrf)]` (billing splits into `router`/`csrf_router` purely to exempt the Stripe webhook, which is validated by `verify_webhook` + an `ON CONFLICT DO NOTHING` dedup table). `tokens_match` uses `hmac.compare_digest`; app.js attaches `X-CSRF-Token` on `htmx:configRequest`
- app/security/passwords.py — argon2-cffi defaults (t=3, m=64 MiB, p=4) are the current OWASP profile; `needs_rehash` is actually called on both successful login paths in `authenticate` and no legacy hash scheme is accepted anywhere
- app/security/rate_limit.py:_client_ip matches CLAUDE.md §16 (only honours XFF when the direct peer is in `TRUSTED_PROXIES`, and walks past trusted hops), and `docker-compose.prod.yml:104` asserts `${TRUSTED_PROXIES:?...}` so prod cannot boot with the one-global-bucket default
- Per-recipient throttles (`EmailThrottle`) are checked BEFORE token minting on tenant reset, platform reset, signup and both verify-resend paths, so IP rotation cannot mailbomb one address
- `/platform/complete-switch` re-verifies the platform cookie identity against the token's `iid`, re-fetches the membership server-side, rejects inactive tenant/target, uses a 60 s TTL, and writes a `platform.support_access_session_started` audit row into the target tenant's log
- `_safe_next_path` (public.py:63-93) blocks `//`, backslashes, loop-decoded `..`, and any parsed scheme/netloc; it is reused by `/set-lang`, `/platform/switch` and `/platform/complete-switch`
- Live headers on https://assoluto.eu: `strict-transport-security`, `referrer-policy: same-origin` (so reset tokens in the URL do not leak via Referer), `x-content-type-options: nosniff`, `frame-ancestors 'none'`, and `csrftoken=...; SameSite=lax; Secure`

### injection-input

The classic injection surfaces are in genuinely good shape: Jinja autoescape covers every template (all are `.html`, there is not a single `|safe`, `{% autoescape false %}` or `render_template_string` in the tree), every `text()` call binds parameters including `set_config('app.tenant_id', :tid, true)`, there is no dynamic ORDER BY anywhere, `_safe_next_path` survived live probing with `//evil.com`, `/\evil.com`, `https://evil.com` and the `%09`/raw-tab bypass, the live CSP is `script-src 'self'` with no `unsafe-inline`, and S3 keys are built entirely server-side with a forced `Content-Disposition: attachment` on download. The real hole is in a place autoescape does not reach: ReportLab's `Paragraph` parses its own mini-markup, and `app/services/pdf_service.py` passes raw customer-contact-supplied order-line text straight into it — I proved end-to-end through the real `render_order_pdf()` that this yields an outbound server-side HTTP fetch to an attacker-chosen URL, a `file://` local read, and a permanent 500 on the order PDF export. A second proven issue is a Pillow decompression bomb in the thumbnail background task (310 KB upload → 415 MB peak RSS, measured). I could not exercise authenticated `/app/*` flows against production (no credentials, and the hard rule forbids POSTs), so both findings were proven by executing the shipped code paths locally against the exact pinned library versions rather than against the live host; I also did not review authz/tenant-isolation logic, which is another agent's lens.

Checked and found sound:

- Jinja autoescape coverage: every template file in app/templates and app/platform is .html; grep found zero `|safe`, zero `{% autoescape %}` blocks, zero `render_template_string`, and exactly one `Markup()` (app/templating.py:304, csrf_input, whose value is a server-minted `secrets.token_urlsafe` token)
- Live CSP verified with `curl -sI https://assoluto.eu/` and `/platform/login`: `script-src 'self'` (no unsafe-inline), `object-src 'none'`, `frame-ancestors 'none'`, `base-uri 'self'`, form-action correctly extended with `https://*.assoluto.eu` + Stripe hosts per CLAUDE.md §10
- Open redirect: `_safe_next_path` (app/routers/public.py:62) probed live via /set-lang — `//evil.com`, `/\evil.com`, `https://evil.com` all collapse to `/`; `/%09/evil.com` stays a literal path (browsers do not percent-decode before URL parsing) and a raw-tab variant is caught because Python's urlsplit strips \t/\r/\n and then sees netloc='evil.com'
- SQL injection: every `text()` call site (deps.py:139, deps.py:241, tenant_admin.py:104, main.py, tasks/periodic.py) uses bind parameters; `set_config('app.tenant_id', :tid, true)` is parameterised as documented; no f-string or %-interpolation into SQL anywhere
- Dynamic ORDER BY / column names: none — all `order_by` calls use ORM column attributes; sorting is never taken from a query param
- CSRF coverage: every router that accepts mutations carries `dependencies=[Depends(verify_csrf)]`. Only two routers lack it — app/routers/health.py (GET-only) and the Stripe webhook APIRouter in app/platform/routers/billing.py:38 (signature-verified, correctly exempt)
- S3 object-key injection: `build_storage_key` (attachment_service.py:57) composes `tenant.storage_prefix` + order UUID + attachment UUID + `filename.rsplit('/',1)[-1].replace('\\','_')` — the client cannot choose or escape the prefix; presigned GETs are 300 s and force `Content-Disposition: attachment` so an uploaded .html cannot render inline from the storage origin
- Email header injection (app/email/sender.py:135, app/routers/www.py contact form): confirmed stdlib `EmailMessage.__setitem__` raises `ValueError: Header values may not contain linefeed or carriage return characters`, so a CRLF in a contact-form name cannot inject a Bcc
- Reflected XSS on public pages: `og:url` echoes the full request URL, but a raw `"` comes back as `&#34;` — verified live with `curl -sg 'https://assoluto.eu/contact?x="><script>alert(1)</script>'`
- Search / command palette (app/routers/search.py, app/services/search_service.py, _palette_results.html): LIKE metacharacters escaped by `_ilike_pattern`, query echoed only through autoescaped Jinja, result `href`s are server-built route paths so palette.js's `window.location.href = ...` cannot be fed a javascript: URI
- Client JS (app/static/js/{app,palette,theme-init}.js): the only `innerHTML` writes are `= ""` and a DOM-round-trip of the button's own markup; no `eval`, `new Function`, `document.write`, `location.hash`/`search` parsing, or server-JSON parsing
- JSON-in-<script>: the only `<script>` blocks besides same-origin src tags are `application/ld+json` on marketing pages, populated exclusively from gettext msgids (developer-controlled), not request data

### billing-integrity

The Stripe *ingress* is genuinely well built: the signature is verified against the raw body before any parsing, the dedup INSERT and the handler run inside one explicit `async with db.begin()`, the customer→tenant resolution has a real anti-spoof cross-check, and prices/plans are never client-controlled (only `plan_code` from the path, resolved against `platform_plans`). Money is integer cents plus `Decimal` in the PDF — no float arithmetic anywhere. Where it breaks down is *lifecycle*: there is no status-transition guard, so a retried or out-of-order `customer.subscription.updated` can resurrect a canceled subscription; a plan change is implemented as a brand-new Checkout session on a customer who already has an active subscription (double billing, with the local single-row model hiding the orphan); a tenant hard-cut by `enforce_canceled_subscriptions` is never reactivated even after they pay again; and every billing route picks "the first tenant_admin membership" from an unordered query, which now includes platform-support memberships. I could not verify runtime config (whether prod has `STRIPE_SECRET_KEY` set — if it does not, `start_checkout` runs the demo branch and grants any tenant admin a free plan upgrade with a "successfully updated" flash; combined with known F-BE-002 that is worth an operator check), and I did not run the postgres test suite per instructions, so all conclusions are from code reading plus the non-postgres suite baseline.

Checked and found sound:

- Webhook authenticity: app/platform/routers/billing.py:724-730 reads the raw body via `await request.body()` and passes it unparsed to `verify_webhook`, which calls `stripe.Webhook.construct_event(payload=..., sig_header=..., secret=..., tolerance=300)`; signature failure raises BillingError → HTTP 400 (not 200). Demo mode returns 503 before anything is parsed. Correctly CSRF-exempt (mounted on the non-csrf `router`), so the signature is the only gate — as intended.
- Replay/idempotency: the dedup `INSERT INTO platform_stripe_events ... ON CONFLICT (id) DO NOTHING RETURNING id` and the handler run inside the SAME explicit `async with db.begin()` (billing.py:753-770), so a concurrent duplicate delivery loses the row lock and short-circuits to 200 — no check-then-act race. WebhookNotYetReady deliberately rolls the dedup row back and returns 503 so Stripe retries.
- Tenant-spoof guard in `_resolve_tenant_id` (webhooks.py:70-140): resolution is customer→Tenant.stripe_customer_id first (server-written only), then server-minted client_reference_id, then customer-writeable metadata, with a cross-check that refuses to resolve on mismatch. `tests/test_stripe_webhooks.py:363` covers it.
- Price/plan tampering: the client can only supply `plan_code` in the URL path; the price always comes from `plan.stripe_price_id` in `platform_plans` (service.py:398). `HIDDEN_PLAN_CODES` blocks `community` with a 400, `enterprise` renders a /contact link rather than a checkout button. No request field reaches Stripe's amount, price, currency, or quantity.
- Plan-limit call sites: `ensure_within_limit` is invoked from exactly the four documented creation services (auth_service.py:274 contacts, auth_service.py:374 users, order_service.py:347 orders, attachment_service.py:117 storage) and I found no alternate creation route that bypasses them — there is no bulk import, no CSV upload, and no JSON API creating those rows.
- Storage-quota accounting is honest: `app/routers/attachments.py:116` passes `size_bytes=len(data)` from the actually-received bytes (the server proxies the upload; presigned PUT is still roadmap R0), so a client cannot under-declare a file size to evade `max_storage_mb`. Rounding is ceil-to-MB with a floor of 1 (attachment_service.py:114-117), which errs in the operator's favour.
- Money representation: integer cents throughout (`monthly_price_cents`, `amount_cents`, `int(data.get("amount_paid", 0))`); the PDF uses `Decimal` with explicit `quantize(Decimal("0.01"))` — no float arithmetic on money anywhere in billing.
- Periodic jobs `expire_demo_trials` and `enforce_canceled_subscriptions` (app/tasks/periodic.py:208-353) are each wrapped in a `pg_try_advisory_lock`, are idempotent (`AND t.is_active = true`, `status IN ('trialing','demo')`), and correctly bump `session_version` on users and contacts so live cookies die immediately.
- Stripe idempotency keys are supplied on both `checkout.Session.create` and `billing_portal.Session.create`, and the round-3 fix anchoring the key on `sub-{subscription_id}` after a consumed trial does prevent the stale-cached-session collapse (service.py:466-478).
- `_sync_stripe_prices_from_env` (app/main.py:43-110) no-ops safely on empty env under advisory lock 42_005, and `create_checkout_session` raises a loud BillingError rather than silently returning success_url when `plan.stripe_price_id` is NULL — so the F-BE-002 config gap fails loudly *provided* STRIPE_SECRET_KEY is set.

### data-integrity

The transaction plumbing in this codebase is genuinely good: audit rows and OrderStatusHistory are written in the same session as the business mutation (no stray commits), every one of the 14 `background_tasks.add_task` call sites either commits first or schedules a task that reads no DB state, the migration chain is linear with a single head (`1006`) and no model/migration column drift, order status is a varchar-backed enum (so a new Python enum member cannot explode on a missing PG type), there is no `datetime.utcnow()` anywhere, and the CSV export already has a customer-name cache instead of an N+1. The real damage in my lens is concentrated in one place: `quoted_total` is a cached denormalisation that is only maintained on *some* mutation paths, and commit d65161b's new free-jump graph added a path that stamps a fabricated `0.00` onto it — that number is what the customer-facing PDF prints. Secondary theme: milestone stamps (`delivered_at`) are write-only — nothing clears them on a backward or cancelling move, and `sla_service` keys purely on them with no status filter. What I could NOT check: I did not run the postgres-marked suite (per instruction), so the concurrency finding (F4) is proven structurally from the code rather than by executing two parallel POSTs, and I could not exercise prod with mutating requests.

Checked and found sound:

- All 14 `background_tasks.add_task` call sites (orders.py x5, customers.py x2, tenant_admin.py, attachments.py, public.py, www.py, platform/signup.py x3) — every one either has an explicit `await db.commit()` before scheduling with the payload built while the session was open, or schedules a task that takes only plain values and reads no DB state. CLAUDE.md §2 holds with zero violations.
- Server-side authorization on the new transition endpoint: `orders_transition` (routers/orders.py:1206) calls `get_order_for_principal` (contact→customer scope check) and then `transition_order`, which re-checks `CONTACT_ALLOWED_TRANSITIONS` at the service layer. A CustomerContact POSTing `/app/orders/{id}/transitions/delivered` gets 409, and one posting another customer's order id gets 404. No template-trust bypass.
- `pipeline_rank()` None-handling at every call site: `skipped_statuses` (src=-1 fallback, dst None→[]), `_backfill_milestones` (early return on None), `_status_pipeline._confirm_for`/`_node` (explicit `is not None` guards before comparison). No None arithmetic or comparison bug found.
- Atomicity of status change + OrderStatusHistory + audit_events: all three happen in one request-scoped session with only `flush()` (audit_service docstring and code confirm no internal commit), committed once by the router. `tests/test_audit_transaction_atomicity.py` covers the rollback case; the pattern holds for `transition_order` too.
- Migrations vs models: enumerated every table/column in `Base.metadata` against all 19 migration files — no missing table, no missing column, no orphan model attribute. Order status uses `Enum(..., native_enum=False, length=32)` i.e. varchar, so `enums.py` additions cannot hit a missing PG enum label. Migration chain is a single head (`1006_drop_starter_orders_cap`) with `0010` legitimately merging the 0009/1005 branches via a tuple `down_revision`.
- Timezone hygiene: zero occurrences of naive `datetime.utcnow()` in `app/` or `scripts/`; `datetime.now(UTC)` throughout; `submitted_at`/`closed_at`/`cancelled_at` are `DateTime(timezone=True)` and `delivered_at`/`promised_delivery_at` are plain `Date` compared only against other dates. Month-boundary quota (`snapshot_tenant_usage`) uses an aware `now.replace(day=1,...)` against a timestamptz column — no naive/aware TypeError path found.
- Scheduler duplication risk: `Dockerfile` CMD is a single `uvicorn` with no `--workers`, and neither compose file declares replicas, so APScheduler runs once. `auto_close_delivered_orders` additionally takes a `pg_try_advisory_lock` before mutating. Not a finding today.
- `product_service.create_product` / `update_product` NULL-uniqueness compensation (CLAUDE.md §5) — the check-then-insert is present on BOTH create and update paths, with correct `IS NULL` vs `= :cid` branching and an `id != product.id` self-exclusion on update.
- CSV export (`orders_export_csv`) — batched at 500 rows with a bulk item-count query and a per-request customer-name cache; no N+1, and the streaming generator runs before dependency teardown so the session is still open.
- `attachment_service.create_attachment_row` storage metering — `size_mb` ceils to the next full MB (`max(1, (size_bytes + 1MB - 1) // 1MB)`), so sub-megabyte files cannot slip past a storage cap for free.

### ops-supply-chain

Secret hygiene and CI/CD posture are genuinely good: no live credentials in the tree or in the last 40 commits, `.gitignore`/`.dockerignore` cover `.env`, the Dockerfile copies only named paths (never `COPY . .`), the container runs as uid 1000 under tini, Postgres/MinIO host ports are `!reset []` in the prod overlay, Caddy replaces (not appends) `X-Forwarded-For` so the trusted-proxy rate-limit logic cannot be spoofed, and the live headers on assoluto.eu carry HSTS, a strict CSP, nosniff, DENY and `Secure` cookies. The real exposure in my lens is **availability**: the app runs a single uvicorn worker with no `--workers`, and both the attachment-upload path and the thumbnail background task do blocking network + CPU work directly on the event loop, so one 30 MB upload freezes the whole portal for every tenant — and `python-multipart` is pinned one patch behind a published CPU-exhaustion CVE that any caller holding a freely-issued csrftoken cookie can reach through `verify_csrf`. Second real exposure is **backup durability**: the off-site copy is a destructive `rclone sync` mirror (a wiped `/backups` propagates to the remote on the next 03:00 run) and S3 attachments — the product's "client-owned assets" — have no backup at all, so a founder losing the VPS today recovers the database only if the last off-site mirror survived, and loses every uploaded file unconditionally. I could not verify runtime behaviour on the VPS (no shell), could not confirm the backup cron is actually installed or that a restore has ever been tested, and could not check cache headers on authenticated `/app/*` pages (no credentials) — the Cache-Control finding is inferred from the middleware source plus anonymous responses.

Checked and found sound:

- Secret scan of the working tree for sk_live_/pk_live_/whsec_/AKIA…/PEM private keys/ghp_ — only sk_test_fake and whsec_test placeholders in tests/test_billing*.py and doc placeholders in docs/*.md; no live values
- Git history probe (`git log --all -S"sk_live_"`, `-S"BEGIN RSA PRIVATE"`, `git log -p -40 -- .env.example`) — every hit is documentation or an audit report, no committed credential ever existed
- `.gitignore` covers `.env`, `.env.local`, `.env.*.local`, `.venv/`; `.dockerignore` covers `.env`, `.env.*`, `.git`, `.venv` — and the Dockerfile never does `COPY . .`, it names each path, so a local .env cannot be baked into the image
- Dockerfile runtime stage: non-root `USER app` (uid 1000), tini as PID 1, multi-stage so build-essential/curl/tailwind stay out of the runtime layer
- docker-compose.prod.yml: `ports: !reset []` on both `web` and `postgres`, `minio: !reset null`, `mailhog: !reset null` — Postgres 5432 and MinIO 9000/9001 are NOT published to the host in production
- Enumerated every key in the base compose `web.environment:` block against the prod overlay per CLAUDE.md §15 — APP_ENV, APP_DEBUG, APP_SECRET_KEY, APP_BASE_URL, DATABASE_URL/SYNC/OWNER, S3_ENDPOINT_URL, S3_PUBLIC_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, SMTP_HOST, SMTP_PORT, SMTP_FROM, LOG_JSON are all correctly re-declared (only LOG_LEVEL is not — reported below)
- `docker/Caddyfile` uses `header_up X-Forwarded-For {remote_host}` which REPLACES rather than appends, so a client-supplied XFF cannot reach `app/security/rate_limit._client_ip` — the `--forwarded-allow-ips *` in the Dockerfile CMD is therefore not exploitable in the compose+Caddy deploy
- `app/security/rate_limit.py:_client_ip` correctly refuses to honour XFF unless the direct peer is inside a TRUSTED_PROXIES CIDR, and walks left-to-right skipping trusted entries; `TRUSTED_PROXIES: ${TRUSTED_PROXIES:?}` in the prod overlay makes an empty value a hard compose failure
- Live headers on https://assoluto.eu/ and https://4mex.assoluto.eu/auth/login — HSTS max-age=31536000 includeSubDomains, strict CSP with object-src 'none' and frame-ancestors 'none', X-Content-Type-Options nosniff, X-Frame-Options DENY, Referrer-Policy same-origin, `-Server`/`-Via` stripped by Caddy, csrftoken cookie carries Secure + SameSite=lax
- `app/main.py:_register_error_handlers` — the 500 handler renders `errors/500.html` / `{"detail": "Internal server error"}` and logs only `f"{type(exc).__name__}: {exc}"`; no traceback or SQL reaches the client. `app_debug` defaults to False and the prod overlay pins `APP_DEBUG: "false"`, so Starlette's ServerErrorMiddleware traceback page is off. /docs, /redoc, /openapi.json are None in production
- `app/tasks/email_tasks.py:_safe_error_summary` still strips URLs, JWT-shape tokens, hex secrets and base64 blobs before logging SMTP errors — the prior hardening holds. All `send_*` task bodies are plain `def`, so FastAPI runs the blocking smtplib call in a threadpool, not on the event loop
- `app/security/log_context.py` binds only request_id/method/path — `scope["path"]` excludes the query string, so structlog lines do not carry `?token=` values (the leak is via uvicorn.access, reported separately)

### privacy-gdpr

The privacy surface is unusually well built for a solo-founder SaaS: no third-party fonts/analytics/CDN (verified by grep over `app/templates/` and by the live CSP `default-src 'self'` on https://assoluto.eu), `audit_events` is genuinely append-only at the DB level (`GRANT SELECT, INSERT` only to `portal_app`), platform-operator support access is opt-in and triple-audited into the *tenant's* log (grant, revoke, and session-start), internal comments are correctly excluded from customer notification emails, and every notification email is rendered and sent per-recipient rather than fanned out in one To: header. What is weak is erasure and retention: Art. 17 erasure is anonymisation-of-one-row only — the subject's name and email survive verbatim in `audit_events.entity_label`, `actor_label` and `diff`, and the erasure flow deliberately writes a *fresh* row containing `"Full Name <email>"` which any staff user can retrieve via the free-text search on `/app/admin/audit`; a test asserts this behaviour, so it is intentional rather than an oversight. Separately, the platform `Identity` (the tenant owner's own assoluto.eu account) has no erasure or export route at all — `erase_identity`/`export_for_identity` have zero callers — and there is no purge job anywhere for any personal data, so the retention promises in `privacy.html` §5 have no implementation. I could not check: the live production DB contents, whether the operator actually configured `RCLONE_REMOTE`/`BACKUP_GPG_RECIPIENT` on the VPS, the actual contents of S3, or anything requiring an authenticated session (GET/HEAD-only rule against prod, and no credentials).

Checked and found sound:

- app/services/gdpr_service.py — full read of export_for_user / export_for_contact / export_for_identity / erase_user / erase_contact / erase_identity
- Erasure column coverage: erase_user and erase_contact do null email, full_name, phone, password_hash, preferred_locale, totp_secret, notification_prefs and bump session_version — the row-level anonymisation itself is correct and the forced logout works (verified in tests/test_gdpr.py)
- app/models/order.py + app/models/attachment.py — OrderStatusHistory, OrderComment and OrderAttachment store only *_user_id / *_contact_id UUIDs, no denormalised names, so erasure does not leave PII in those tables
- migrations/versions/0009_audit_events.py:115 — audit_events is append-only for the app role (GRANT SELECT, INSERT ON audit_events TO portal_app; no UPDATE/DELETE). A tenant admin cannot tamper with or delete audit rows through the app.
- app/services/audit_service.py:_apply_principal_scope — customer contacts are hard-restricted to entity_type='order' events on their own customer's orders; contacts without a customer_id get where(false()).
- app/platform/routers/platform_admin.py:631-722 + platform_auth.py:558-592 — support-access grant, revoke AND every session entry are written into the TARGET tenant's audit log with the platform admin's email; CLAUDE.md §6 promise verified as implemented.
- app/services/notification_service.py:build_order_comment + app/routers/orders.py:1344-1365 — internal comments are skipped entirely before the notification payload is built (`if not internal_flag:`), so is_internal comment bodies cannot reach a CustomerContact by email.
- app/tasks/email_tasks.py — multi-recipient notifications render and send one message per (email, locale) tuple; no shared To:/Cc: header leaking recipient lists. _safe_error_summary strips URLs/tokens from SMTP error logs.
- Third-party origins: grep over app/templates/ found no external src=/href= except GitHub, adr.coi.cz and ec.europa.eu links; fonts are self-hosted (live CSP: font-src 'self' data:). No Google Fonts, no analytics, no CDN — Schrems II exposure from the marketing site is nil.
- Live cookie check: `curl -sI https://assoluto.eu/` sets only `csrftoken` (Secure, SameSite=lax, no Domain). No tracking cookie set before consent anywhere.
- app/storage/s3.py:generate_presigned_get — 300 s expiry and forced Content-Disposition: attachment; app/routers/attachments.py:218-220 deletes both the object and the thumbnail from S3 on attachment delete.
- scripts/backup.sh — local dump rotation is implemented at 14 days (PORTAL_KEEP_DAYS) and rclone sync mirrors deletions, so the 'backups 14 days' claim in privacy.html §5 is actually backed by code.

### test-blindspots

Tooling is genuinely clean: `ruff check .` passes, `mypy app` reports "no issues found in 89 source files", `pytest -m "not postgres" -q` is 249 passed, 518 collected. I verified the two isolation suites really do prove isolation — `test_tenant_isolation.py` and `test_audit_rls.py` both open a second engine on `DATABASE_URL` (portal_app, RLS-subject) for the assertions and only use `owner_engine`/`DATABASE_OWNER_URL` for seeding, and CI sets `DATABASE_URL=postgresql+asyncpg://portal_app:...` — so those tests are real, not owner-role theatre. I also disproved the suspected i18n regression: all three catalogs have zero `#, fuzzy` entries, zero active empty msgstrs (my first raw grep of 204/237 was a false alarm caused by multi-line msgstr continuations), and all 68 live `_t(request, "...")` msgids are active (non-obsolete) in the CS catalog. What I could NOT check: any postgres-marked test was off-limits, so I could not measure real line coverage or execute any of the untested routes I identified; I could not run authenticated flows against production (GET/HEAD only, no credentials); and I did not read the DB to confirm production data shapes. The defects below are the gaps the 518 tests do not protect — an untested platform account-recovery flow that violates the repo's own single-use-token rule, audit-trail holes in the bulk and scheduled write paths, an SLA report that counts cancelled work, half of a "fixed" GDPR finding that never shipped, and the complete absence of error tracking.

Checked and found sound:

- tests/test_tenant_isolation.py + tests/test_audit_rls.py — confirmed the asserting sessions run on app_engine (DATABASE_URL = portal_app, RLS-subject) while owner_engine is used only for seed/wipe; CI env at .github/workflows/ci.yml:34 sets the app URL to portal_app, so these prove real RLS, not owner bypass
- uv run ruff check . → 'All checks passed!'
- uv run mypy app → 'Success: no issues found in 89 source files' (check_untyped_defs + strict_optional on)
- uv run pytest -m 'not postgres' -q → 249 passed, 269 deselected; uv run pytest --collect-only -q → 518 tests collected
- i18n catalogs cs/de/en: 0 fuzzy entries in all three; 0 active empty msgstrs in cs and de; 1052 intentionally-empty msgstrs in the en identity catalog — matches the documented design
- All 68 live `_t(request, "...")` msgids in app/**/*.py resolve to ACTIVE (non-obsolete) entries in app/locale/cs/LC_MESSAGES/messages.po — the CLAUDE.md §7 obsolete-msgid failure mode is not present
- CSRF wiring: every router constructs APIRouter(..., dependencies=[Depends(verify_csrf)]) except health.py (GET-only) and app/platform/routers/billing.py, which deliberately splits `router` (4 GETs + the signature-verified Stripe webhook) from `csrf_router` (all 5 state-changing POSTs). No router forgets it.
- app/config.py: 44 settings fields; only `stripe_publishable_key` is read nowhere in app/scripts/tests/templates — and docs/ENV.md:63 already documents it as 'not currently used by the server-rendered UI'
- Advisory-lock discipline in app/tasks/periodic.py: all six scheduled jobs wrap their work in pg_try_advisory_lock/pg_advisory_unlock with distinct ids, so the CLAUDE.md multi-worker double-run hazard is genuinely handled
- app/platform/deps.py:105 require_platform_admin re-reads `is_platform_admin` from the DB rather than trusting the `admin` flag baked into the platform cookie — a revoked admin loses power immediately
- Assertion-quality sweep via AST over all 60 test files: the 'no assert' hits are almost all legitimate `pytest.raises` blocks (test_tenant_isolation, test_security, test_plan_limits, test_usage). No test asserts only on a mock it configured itself; no over-broad `pytest.raises(Exception)`.
- Live production probe (GET only): https://assoluto.eu/healthz → 200 {"status":"ok"}; /platform/signup with Accept-Language: en renders '<title>Create portal · Assoluto</title>'; /pricing with Accept-Language: de renders '<title>Preise · Assoluto</title>'

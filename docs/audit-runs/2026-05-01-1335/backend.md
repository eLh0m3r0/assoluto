# Backend audit — 2026-05-01-1335

## Summary

First automated backend audit using the `/audit` pipeline. Hygiene
baselines all clean (ruff lint + format clean, mypy clean on `app/` —
0 errors across 87 source files, full pytest suite green at
`423 passed in 58.97s`). One **production-active P0** discovered via
prod log reads; one config-shaped P1 that bricks Stripe checkout; the
remainder are P2 polish.

| Severity | Count |
|---|---|
| P0 | 1 |
| P1 | 2 |
| P2 | 4 |

---

### F-BE-001 — `/terms` returns HTTP 500 in production for English locale (gettext `%%` trap)
- **Where**: `app/templates/www/terms.html:111` (msgid in `app/locale/messages.pot:5215-ish` and msgstr empty in `app/locale/en/LC_MESSAGES/messages.po:4994-5004`)
- **Severity**: P0
- **Auto-fixable**: yes
- **Description**: The §8 SLA paragraph in the Terms page contains the literal substring `99.9% monthly` directly inside the gettext call — `{{ _("… targets 99.9% monthly uptime …") }}`. Jinja's i18n extension pipes the gettext-returned string through `rv % variables`, and Python's `%`-formatter sees `% m` (the space + `m` from "monthly") and raises `ValueError: unsupported format character 'm' (0x6d) at index 80`. The CS and DE msgstrs correctly escape this as `99,9 %%` (per CLAUDE.md §7), but the EN msgstr is empty, so gettext returns the unescaped msgid and 500s the page. This is **actively happening on production right now** — multiple `GET /terms HTTP/1.1 500 Internal Server Error` entries in the last hour of `web` container logs from the public IP `93.99.225.146`. The Privacy page renders fine; only Terms is broken, and only for the English fallback (which is the default for any visitor without a locale cookie or `Accept-Language: cs`/`de`).
- **Suggested fix**: Two equivalent options. (a) Edit the msgid in the template to `99.9 %%` (and run `pybabel extract + update + compile` so all four catalogues pick up the new msgid; existing translations in cs/de need their msgstrs re-pointed). (b) Cleaner: split the troublesome substring out of the gettext call entirely — `{{ _("… targets ") }}99.9%{{ _(" monthly uptime …") }}` — which is the pattern CLAUDE.md §7 explicitly recommends ("Prefer the split-form pattern … simpler to translate and immune to the `%%` trap"). Option (b) is preferred because it doesn't require touching every translator's catalogue.
- **Evidence**:
  - Prod logs (last 2h, sampled):
    ```
    web-1 | {"path": "/terms", "error": "ValueError: unsupported format character 'm' (0x6d) at index 80", ...}
    web-1 | INFO:     93.99.225.146:0 - "GET /terms HTTP/1.1" 500 Internal Server Error
    web-1 |   File "/app/app/templates/www/terms.html", line 111, in block 'content'
    ```
  - `app/templates/www/terms.html:111` source contains literal `99.9% monthly`.
  - `app/locale/en/LC_MESSAGES/messages.po:5004` shows the msgstr is empty (English falls back to the unescaped msgid).
  - `app/locale/cs/LC_MESSAGES/messages.po:5329` and `de/.../messages.po:5413` correctly use `99,9 %%`.

---

### F-BE-002 — Stripe checkout silently no-ops in production (price IDs missing in `/etc/assoluto/env`)
- **Where**: production env file `/etc/assoluto/env`; affects code path `app/main.py:42 _sync_stripe_prices_from_env` → `app/platform/billing/service.py create_checkout_session`
- **Severity**: P1
- **Auto-fixable**: no (operator config)
- **Description**: Per CLAUDE.md §17 the Stripe price IDs flow ENV → DB at boot. On prod, `env | grep STRIPE` lists `STRIPE_PRICE_STARTER` / `STRIPE_PRICE_PRO` as set — but their **values** are empty strings (verified by `echo $STRIPE_PRICE_STARTER` inside the web container returns empty). `_sync_stripe_prices_from_env` filters out empty values (`{code: pid for code, pid in env_map.items() if pid}`) and no-ops, so all four rows in `platform_plans` still have `stripe_price_id IS NULL`. Any user clicking "Upgrade" will hit the `if not plan.stripe_price_id: return` early-exit in the checkout service and stare at a silent failure. Given the recent prelaunch + billing-admin commits explicitly enable billing visibility, customers signing up right now will not be able to complete the upgrade flow. The web container is on tag `1b1c8f9` and was restarted ~1h ago, so this is the live state.
- **Suggested fix**: Set actual price IDs in `/etc/assoluto/env` (e.g. `STRIPE_PRICE_STARTER=price_xxx`, `STRIPE_PRICE_PRO=price_yyy`) from the production Stripe dashboard, then `docker compose ... restart web`. Boot will run `_sync_stripe_prices_from_env`, log `stripe_price.sync.updated` for each, and the DB column will populate. Until then either disable the upgrade CTAs or surface a "billing temporarily unavailable" notice. As a defensive code change: have the boot path log `stripe_price.sync.no_env` (info level) when both env vars are empty so the operator gets a notification instead of silent skip.
- **Evidence**:
  - `psql ... -c "SELECT code, stripe_price_id FROM platform_plans"` shows all four `stripe_price_id` columns NULL.
  - In-container: `STRIPE_PRICE_STARTER=MISSING` / `STRIPE_PRICE_PRO=MISSING` from `${VAR:-MISSING}` echo (confirms keys exist but values are empty).
  - Source: `app/main.py:61` filters empties; `app/main.py:62-63` early returns.

---

### F-BE-003 — Zero test coverage for GDPR endpoints
- **Where**: `app/routers/tenant_admin.py:595` (`GET /app/admin/profile/export`), `app/routers/tenant_admin.py:617` (`POST /app/admin/profile/delete`); supporting service `app/services/gdpr_service.py`
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: GDPR Art. 15 / 17 / 20 endpoints exist (export-for-user JSON download, password-confirmed soft-erase that anonymises PII while preserving order/audit history) and are linked from the staff profile page, but `grep -rln gdpr|profile/export|profile/delete|export_for_user|erase_user tests/` returns zero hits. These are precisely the routes that need regression coverage: the erase path mutates state (nulls email/full_name, bumps session_version, writes `user.gdpr_erased` audit, blocks last-admin self-erase), and a silent regression here is a regulator-visible event. Risk is amplified by the fact that the password verification + last-admin guard (lines 638-676) was added recently and only one code path — manual QA — has ever exercised it.
- **Suggested fix**: Add `tests/test_gdpr_flow.py` covering: (a) `GET /admin/profile/export` returns 200 with correct `Content-Disposition` and a JSON body containing the expected `kind: user`, profile, orders_created, audit_events keys; (b) `POST /admin/profile/delete` with wrong password redirects with `error=` flash and does NOT mutate; (c) `POST /admin/profile/delete` as the last tenant admin returns the "promote someone else first" error and does NOT mutate; (d) successful erase nulls PII, bumps session_version, writes audit row, clears session cookie. Aim for 4–5 tests. The fixtures already exist (`tenant_client`, `wipe_db`).
- **Evidence**: `grep -rln "gdpr\|GDPR\|profile/export\|profile/delete\|export_for_user\|erase_user" tests/` → empty. Only matches in `tests/` are unrelated string fragments (`/me/profile?error=` in routers, not tests).

---

### F-BE-004 — `app/services/gdpr_service.py` exports `export_for_contact` / `erase_contact` but no router exposes them
- **Where**: `app/services/gdpr_service.py:102` (`export_for_contact`), `app/services/gdpr_service.py:233` (`erase_contact`); router gap in `app/routers/me.py` (which exposes only `/me/profile` GET/POST and `/me/profile/password`)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Customer contacts are data subjects under GDPR exactly the same as staff users — and the service layer correctly implements both export and soft-erase for them — but the only routes that expose `/profile/export` and `/profile/delete` live in `app/routers/tenant_admin.py` and require `require_tenant_staff`. A customer contact that mails the operator asking for their data has no self-service path; the operator has to run an ad-hoc psql / Python script. This is a compliance gap for a regulated EU SaaS.
- **Suggested fix**: Add two endpoints to `app/routers/me.py`: `GET /app/me/profile/export` (calls `export_for_contact`, returns JSONResponse with `Content-Disposition: attachment`) and `POST /app/me/profile/delete` (password-confirmed, calls `erase_contact`, clears session cookie, redirects to public landing with a "your data has been deleted" notice). Add CTAs to `app/templates/me/profile.html`. Same pattern as staff. Then cover both with the test suite proposed in F-BE-003.
- **Evidence**: `app/services/gdpr_service.py` defines both functions; `grep -n "@router\." app/routers/me.py` returns only the three existing routes; no `me/profile/export` or `me/profile/delete` paths exist.

---

### F-BE-005 — Recent feature commits land without paired test changes (advisory)
- **Where**: commits `1b1c8f9` (subscription editor — +226 lines in `app/platform/routers/platform_admin.py`, +127-line template, no test changes) and `884508b` (verify-gate + honeypot + 5 prelaunch fixes, +88 lines in `app/platform/routers/signup.py`, +58 lines in `app/services/auth_service.py`, no test changes)
- **Severity**: P2
- **Auto-fixable**: no (process / discipline)
- **Description**: Two of the most recent business-logic commits ship with substantive new code paths and zero test additions:
  - `1b1c8f9` adds a full per-tenant subscription editor with plan-swap, trial/period-end edits, a state auto-correct (canceled → trialing on future trial-end), and quick-action buttons (`extend_trial`, `mark_internal`, `set_active`). Each writes a `billing.subscription_edited_by_platform_admin` audit row. None of these branches are exercised by `tests/test_billing.py` or any other test.
  - `884508b` adds the `_has_unverified_identity` lookup (with a new owner-DSN engine creation per call), a new `UnverifiedIdentity` exception path in `authenticate`, a public `POST /platform/check-email` endpoint with email-enumeration defence, and a CSS-hidden honeypot field on signup. Honeypot trip behaviour ("render verify-sent without creating anything") is never asserted.
  Both features are user-visible and security-adjacent. Lack of regression tests means the next refactor can silently break (e.g. a typo in the honeypot field name resurrects bot signups; a mistake in the canceled→trialing auto-correct could re-activate a deliberately-cancelled tenant).
- **Suggested fix**: Add three small tests as a follow-up:
  1. `tests/test_subscription_editor.py` — happy-path POST with new plan_id + trial date, assert DB updated, audit row written, redirect with notice.
  2. Same file — `set_active` quick action moves a `canceled` row back to `active`, drops `trial_ends_at`, sets `current_period_end` to ~now+30d.
  3. `tests/test_signup.py` — extend with a honeypot-tripped POST: assert no Identity / Tenant / User rows created, response 200 (verify-sent template), `signup.honeypot_tripped` log emitted (use `caplog`).
- **Evidence**: `git show --stat 1b1c8f9 884508b` — neither commit's stat block lists any `tests/` file.

---

### F-BE-006 — Stripe webhook handler set is narrow (no `payment_method.detached` / `customer.updated` / `customer.subscription.paused`)
- **Where**: `app/platform/billing/webhooks.py:428` (`HANDLERS` registry)
- **Severity**: P2
- **Auto-fixable**: no (depends on product policy)
- **Description**: The current registry handles checkout.session.completed, customer.subscription.{created,updated,deleted}, invoice.paid, invoice.payment_failed, customer.subscription.trial_will_end and (recently added) charge.refunded. Three events worth considering for a B2B SaaS with the current Option-A cancel flow:
  - `customer.subscription.paused` — Stripe lets a customer self-pause via the customer portal. Current code path won't see it; a paused subscription will keep its old `status='active'` in our DB and the user keeps their subscription banner / access. Should map to `past_due` or a new `paused` status.
  - `customer.updated` — billing address / VAT-ID change in Stripe should be mirrored to the invoice template, otherwise next month's invoice PDF still shows the old data.
  - `payment_method.detached` — when the only saved card is removed, surfacing this as a "no payment method on file" banner before the next renewal would prevent the silent past_due → cancel path.
  Unknown events are logged at info level (`stripe.webhook.ignored`), so this isn't a crash — just silent drift.
- **Suggested fix**: Decide product policy, then add handlers. At minimum add `customer.subscription.paused` because the existing cancel-flow trust story breaks if Stripe self-service users can pause without our DB knowing. The other two are nice-to-have.
- **Evidence**: `app/platform/billing/webhooks.py:428-437` enumerates the eight handlers above; no handler for the three events listed.

---

### F-BE-007 — Mypy passes (0 errors) — first audit baseline
- **Where**: `.venv/bin/mypy app/`
- **Severity**: P2 (informational — establishing baseline)
- **Auto-fixable**: n/a
- **Description**: Mypy reports `Success: no issues found in 87 source files`. Recording this as the baseline so the next `/audit-verify` run can detect regressions per CLAUDE.md §12 ("regression = P1"). Same baseline for ruff (`All checks passed!`) and ruff format (`147 files already formatted`). Pytest: `423 passed, 12 warnings in 58.97s` — under the 60s budget but close, and the 12 warnings are all the same `slowapi` `asyncio.iscoroutinefunction` deprecation (third-party, will resolve when slowapi releases a 3.16-compatible version). No flaky / new warnings from our own code. RLS / tenant isolation tests pass. Architecture invariants validated:
  - Periodic-job advisory lock IDs (42_001 — auto_close, 42_002 — invite_cleanup, 42_003 — stripe_event_cleanup, 42_004 — demo subscription normalise, 42_005 — stripe price sync, 42_006 — expire_trials, 42_007 — enforce_canceled) are all unique. ✓
  - All public routes that read session cookies use `read_session_for_tenant` (verified in `app/routers/public.py`); `deps.get_current_principal` does the equivalent explicit `session_data.tenant_id != str(tenant.id)` check inline. ✓
  - Every `background_tasks.add_task` call inspected (orders.py, tenant_admin.py, attachments.py, customers.py, www.py, public.py, platform/routers/signup.py) is preceded by an explicit `await db.commit()` per CLAUDE.md §2. ✓
  - Schema vs. ORM drift on the heavy tables (`orders`, `order_items`, `order_attachments`, `customer_contacts`, `platform_subscriptions`) — every column matches the ORM definitions in `app/models/*.py` and `app/platform/billing/models.py`. ✓
  - Migration chain — `alembic_version` on prod is `1006_drop_starter_orders_cap`. All 18 migrations have unique `down_revision` values; the chain merges cleanly via `0010_orders_delivered_at` (which carries a tuple `down_revision` to merge two heads).
  - `app.platform` import isolation — there are runtime-local `from app.platform.usage import ensure_within_limit` imports inside core service files (`auth_service.py`, `order_service.py`, `attachment_service.py`), but these are intentional per CLAUDE.md §17 (plan-limit enforcement called from core creation flows) and the `test_platform_routes_not_mounted_when_flag_off` regression test still passes, which is the operative assertion. The CLAUDE.md §6 wording "core never imports from app.platform" is now slightly out of date — it really means "core never mounts platform routes / depends on platform behavior at module-import time".
- **Evidence**: command outputs above; `pyproject.toml` and `.venv/bin/{ruff,mypy,pytest}` are pinned versions.
- **Suggested fix**: When the next `/audit-verify` run executes, baseline these counts and flag any of: mypy >0, ruff non-clean, ruff format drift, pytest non-green, pytest >60s, new test warning categories, or a new lock_id collision.


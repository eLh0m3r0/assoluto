# UX audit — authenticated tenant walkthrough (supplementary)

- **Run**: 2026-07-03-1507
- **Tenant**: `test-a.assoluto.eu` (live prod audit tenant, dummy data)
- **Auth**: `audit@assoluto.eu`, role TENANT_ADMIN — login POST only, GET-only thereafter
- **Method**: cookie-jar curl (Chrome MCP unavailable → no JS-console, no visual
  dark-mode render, no mobile-viewport checks; those coverage gaps noted below)

Primary flows are healthy: login → dashboard → all lists → all detail pages →
admin surfaces all return 200 and render valid Czech HTML. No tracebacks, no raw
Jinja, no `%(var)s` placeholder leaks, no double-escaped entities, no missing
`dark:` variants (static scan), and the new **"Vaše data (GDPR)"** block renders
on `/app/admin/profile`. Three low-severity defects, all on the audit-log /
copy surface.

---

### F-UX-A-001 — Audit log renders literal Python `None` for null "after" values
- **status**: fixed (same-day, commit pending)
- **Where**: https://test-a.assoluto.eu/app/admin/audit (change-summary lines, e.g. `customer.created`)
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: The one-line change summary in the audit log formats an empty
  *old* value as the em-dash placeholder `—`, but formats an empty *new* value as
  the literal string `None`. Every `customer.created` row where DIČ is unset shows
  `dic: — → None`. Inconsistent within the same line and leaks a Python internal
  into a surface the UI itself labels "Úplný a neměnný záznam" (the authoritative
  compliance record).
- **Suggested fix**: In the audit summary renderer (the helper that builds the
  `field: old → new` line — likely `app/services/audit*.py` or the
  `templates/admin/audit*.html` diff macro), coerce `None`/null to the same `—`
  placeholder used on the "before" side. One shared `_fmt(v)` returning `"—"` for
  `None`/empty covers both sides.
- **Evidence**: raw HTML `dic: —\n → None` (`<summary>` block); text
  `customer.created Klient Test Alpha customer dic: — → None`

### F-UX-A-002 — Audit-log JSON detail leaks `\uXXXX` escapes; Czech text unreadable
- **status**: fixed (same-day, commit pending)
- **Where**: https://test-a.assoluto.eu/app/admin/audit (expanded `<details>` before/after JSON)
- **Severity**: P1
- **Auto-fixable**: yes
- **Description**: The expandable before/after JSON payload is serialized with
  Python's default `ensure_ascii=True`, so the served HTML literally contains
  `Č`, `—`, `ž`, etc. A Czech operator auditing "who changed what"
  reads `"description": "DELIVERY-CZ — Doprava po ČR"` instead of
  `Doprava po ČR`. On a surface sold as the immutable audit trail, unreadable
  Czech names materially weakens its usefulness.
- **Suggested fix**: Serialize the payload with `json.dumps(payload,
  ensure_ascii=False, indent=2)` before passing to the template (or decode in the
  Jinja filter). Confirm the response is UTF-8 (it is — `<meta charset>` + Czech
  renders fine elsewhere), so non-ASCII is safe to emit directly.
- **Evidence**: raw HTML `{\n  "after": {\n    "description": "DELIVERY-CZ — Doprava po ČR", ...`

### F-UX-A-003 — English jargon leaks into Czech copy ("staff", "tenantu", "grace období")
- **status**: fixed (same-day, commit pending)
- **Where**: /app/admin/users, /app/admin/profile (GDPR block), /app dashboard (expired-subscription banner)
- **Severity**: P2
- **Auto-fixable**: yes
- **Description**: Several Czech strings splice untranslated English/tech jargon:
  `/app/admin/users` — "Spravujte **staff** uživatele — přístup do všech dat
  **tenantu**."; GDPR block — "…z tohoto **tenantu**."; dashboard banner — "Po
  skončení **grace** období pro export dat…". "staff", "tenant(u)" and "grace"
  read as developer vocabulary, not customer-facing Czech.
- **Suggested fix**: Retranslate the msgids: "staff uživatele" → "členy týmu",
  "tenantu" → "portálu"/"firmy", "grace období" → "ochranné lhůty". Update the CS
  PO entries and recompile per the CLAUDE.md `pybabel` workflow.
- **Evidence**: text `Spravujte staff uživatele — přístup do všech dat tenantu.`;
  `Po skončení grace období pro export dat skončí váš přístup.`

---

## Walkthrough log

| Page | Method | Status | Notes |
|---|---|---|---|
| /auth/login | GET | 200 | CSRF token in `csrftoken` cookie + `csrf_token` hidden field |
| /auth/login | POST | 303 → /app | login OK; `sme_portal_session` set HttpOnly/Secure/SameSite=lax (the only mutating request made) |
| /app | GET | 200 | dashboard renders; expired-subscription banner (expected for this tenant); metrics: Klienti 4, otevřené obj. 1 |
| /app/orders | GET | 200 | list renders; 2 orders |
| /app/orders/{id} | GET | 200 | order detail; empty-items state "Žádné položky." correct (item rows seen elsewhere are add-form dropdown options, not a double-render) |
| /app/customers | GET | 200 | list; 4 customers |
| /app/customers/{id} | GET | 200 | detail; permissions matrix + "Klient zatím nemá žádné kontakty." empty state good |
| /app/products | GET | 200 | list; 6 products |
| /app/products/{id} | GET | 200 | detail; clean, "Popis —" empty placeholder |
| /app/assets | GET | 200 | list renders (2 assets) |
| /app/admin/users | GET | 200 | team list; active/deactivated states, "(vy)" self-marker — **F-UX-A-003** |
| /app/admin/profile | GET | 200 | **"Vaše data (GDPR)"** block present with export action — **F-UX-A-003** (tenantu) |
| /app/admin/audit | GET | 200 | renders — **F-UX-A-001**, **F-UX-A-002** |
| /app/admin/tenant-settings | GET | 200 | email default-language form renders |
| /app/me/profile | GET | 303 → /app/admin/profile | correct — admin is a User, not a CustomerContact |

**Passed silently (positive evidence):** html `lang="cs"` on every page; no
traceback/SQLAlchemy/Jinja-exception markers; no raw `{{`/`{%`; no `%(var)s`
leaks; no double-escaped entities (`&larr;` is a proper single entity); no
`bg-white`/`text-gray-900` without a `dark:` counterpart in static scan; page
titles all well-formed (`Assoluto — <page> · Test Alpha s.r.o.`); theme toggle
("Přepnout motiv") + CS·EN·DE switcher present.

**Coverage gaps (Chrome MCP unavailable):** no JS-console/network-panel
inspection (HTTP statuses were all 200/303 via curl, but client-side errors
weren't observable); no rendered dark-mode visual check (static class scan only);
no 390px mobile-overflow check. Recommend a Chrome-enabled rerun for these three.

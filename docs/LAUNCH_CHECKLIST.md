# Assoluto launch checklist

Pre-launch tasks that **only you can do** (accounts, identity, legal).
Everything codebase-side is already renamed to Assoluto. This doc is
your one-page punch list for going live.

---

## 1. Domain registrations

**Minimum viable (must-have, total ~€6/year):**

- [ ] `assoluto.eu` — primary marketing + app root + tenant subdomains

**Defensive portfolio (recommended, ~€60/year total):**

- [ ] `assoluto.cz` — CZ market defense (~200 Kč/year via Wedos or Gransy)
- [ ] `assoluto.de` — DACH market defense (~€10/year)
- [ ] `assoluto.app` — alternative app URL (~$15/year — Google Registry)
- [ ] `assoluto.io` — tech/dev mirror, optional (~$40/year)

**Registrators:**

- **Porkbun** or **Regery** — best prices for `.eu/.app/.io`, free WHOIS privacy
- **Wedos.cz** or **Gransy.cz** — required for `.cz` (EU-resident registrant)

**Post-registration setup per domain:**

1. Enable WHOIS privacy (if registrar doesn't default)
2. Set nameservers to Cloudflare (recommended) or registrar default
3. Add `A` records for the primary: `assoluto.eu → <VPS IP>`
4. Add wildcard: `*.assoluto.eu → <VPS IP>`
5. Auxiliary TLDs (`.cz, .de, .app, .io`) → 301 redirect to `assoluto.eu`
   at the registrar's free redirect service, or via Cloudflare Page Rules

---

## 2. Trademark registration (EUTM)

- [ ] Run TM clearance via your tool or attorney in classes **9** (software)
      and **42** (SaaS services)
- [ ] File **EU trade mark** at [euipo.europa.eu](https://euipo.europa.eu/ohimportal/en/apply-for-a-trade-mark)
  - Fee: €850 for 1 class, €900 for 2 classes, €150 per additional class
  - **Recommendation: file a combined mark** (wordmark + logo) rather
    than pure wordmark — gives stronger protection and defuses any
    descriptiveness challenge on "Assoluto" as an Italian word
  - Classes: 9 (downloadable software + interfaces) + 42 (SaaS,
    platform-as-a-service, software design)
  - Term: 10 years, renewable
- [ ] Timeline: 4–6 months to registration (faster if no opposition)
- [ ] Optional: Madrid Protocol extension later for US/UK/CH if expanding

---

## 3. Social handles (squat immediately)

Cheap defensive move — even if you don't post, own the handle.

- [ ] **X (Twitter):** @assoluto_eu or @assoluto (check, claim whichever is free)
- [ ] **LinkedIn Company Page:** Assoluto (company.linkedin.com/company/assoluto)
- [ ] **GitHub org:** github.com/assoluto (move or mirror repos here)
- [ ] **Instagram:** @assoluto.eu (if primary is taken)
- [ ] **Mastodon:** @assoluto@fosstodon.org (dev-community presence)
- [ ] **YouTube:** /@assoluto
- [ ] **Product Hunt:** /products/assoluto (reserve for launch day)

Tooling: [namechk.com](https://namechk.com/) checks all at once.

---

## 4. Email infrastructure

Brand emails are already referenced in the codebase:

- `opensource@assoluto.eu` — commercial licensing inquiries
- `security@assoluto.eu` — vulnerability disclosures
- `conduct@assoluto.eu` — Code of Conduct violations
- `ops@assoluto.eu` — Let's Encrypt + monitoring alerts
- `no-reply@assoluto.eu` — transactional outbound (SMTP_FROM)
- `privacy@assoluto.eu` — GDPR / privacy requests (templated in Privacy Policy)

**Set these up:**

- [ ] Postmark / Resend / SES account for sending (transactional)
- [ ] Receiving: Google Workspace, Fastmail, or Zoho Mail routing to your inbox
- [ ] SPF, DKIM, DMARC DNS records (your email provider will guide you)
- [ ] Verify domain in Postmark/Resend → unlock `no-reply@assoluto.eu` sending

---

## 5. GitHub repo rename (optional but recommended)

The codebase still references `github.com/elh0m3r0/sme-client-portal`.
When ready:

- [ ] GitHub → repo Settings → **Rename** to `assoluto` (or `assoluto-portal`)
- [ ] GitHub auto-redirects old URLs for ~1 year
- [ ] Update `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md` clone URLs
- [ ] Update Docker image path: `ghcr.io/<new-org>/assoluto`
- [ ] Update `.github/workflows/deploy-production.yml` if image path changes

**Or** create a new GitHub **organization** called `assoluto` and
transfer the repo there — cleaner separation of personal vs brand.

---

## 6. Production deployment

Follow [`docs/DEPLOY_HETZNER.md`](DEPLOY_HETZNER.md) end-to-end. It's
already updated to use `assoluto.eu` as the canonical domain. Quick summary:

- [ ] Provision Hetzner CX22 VPS (~€5/month)
- [ ] Point DNS A records (apex + wildcard) to VPS IP
- [ ] Issue wildcard Let's Encrypt cert via Cloudflare DNS-01
- [ ] Set up `/etc/assoluto/env` with real secrets (Postgres, S3, SMTP, Stripe)
- [ ] First manual deploy from GHCR
- [ ] Enable GitHub Actions auto-deploy on push to `production` branch

---

## 7. Legal / compliance (Czech + EU)

If running paid subscriptions (`FEATURE_PLATFORM=true` + Stripe live):

- [ ] Fill `PLATFORM_OPERATOR_{NAME,ICO,ADDRESS,EMAIL}` env vars — these
      populate the Terms and Privacy pages (otherwise they 404)
- [ ] Register as **plátce DPH** (VAT payer) if annual revenue > 2M CZK
- [ ] Stripe Tax → Register for Czechia (21% DPH)
- [ ] Set up Czech-compliant invoicing (Fakturoid or iDoklad integration
      — see [`docs/DEPLOY_SAAS.md`](DEPLOY_SAAS.md) §8a)
- [ ] Privacy Policy review by Czech data-protection lawyer (GDPR + Czech
      spec like zákon č. 110/2019 Sb.)
- [ ] Terms of Service review (Czech civil code requirements)

---

## 8. Launch-day checklist

Once domains, TMs filed, infrastructure up:

- [ ] Test full flow: signup → email verify → onboarding → first order
- [ ] Test billing with Stripe test cards → then switch to live
- [ ] Set up UptimeRobot monitoring on `https://assoluto.eu/healthz`
- [ ] Set up Sentry for error tracking (`SENTRY_DSN` env var)
- [ ] First backup dry-run + restore test (don't pretend backups work until
      you've restored from one)
- [ ] Announce on LinkedIn, relevant Czech SME subreddits, maker groups
- [ ] Submit to Product Hunt, BetaList, Indie Hackers

---

## 9. What's already done in the codebase

As of this commit, the repo is ready for Assoluto:

- ✅ All user-facing branding renamed from "SME Portal" → "Assoluto"
- ✅ HTML titles, meta descriptions, email templates, www pages
- ✅ Brand emails: `security@assoluto.eu`, `opensource@assoluto.eu`,
     `conduct@assoluto.eu`
- ✅ Default `PLATFORM_OPERATOR_EMAIL = opensource@assoluto.eu`
- ✅ Default `SMTP_FROM = "Assoluto <noreply@localhost>"`
- ✅ `docs/DEPLOY_HETZNER.md` — production guide with real `assoluto.eu`
- ✅ `docs/DEPLOY_SAAS.md` — hosted SaaS guide with real `assoluto.eu`
- ✅ `docker/nginx.conf.example` — `server_name assoluto.eu *.assoluto.eu`
- ✅ Infrastructure paths: `/opt/assoluto`, `/etc/assoluto/env`,
     `assoluto-backup` cron, `assoluto-prod` VPS hostname
- ✅ Deploy SSH keypair filename: `~/.ssh/assoluto_deploy`

**What is NOT yet renamed (by design):**

- GitHub repo URL still `sme-client-portal` — rename when you're ready
- Docker image path still `ghcr.io/elh0m3r0/sme-client-portal` — follows
  GitHub rename
- Python package name still `sme-client-portal` in `pyproject.toml` —
  renaming would require updating imports across the codebase; low-value
  cosmetic change, skip for now
- Demo tenant slug `4mex` in tests/seed fixtures — this is demo data, not
  brand identity
- Generic templates (`docs/SELF_HOST.md`, `docs/DEPLOY_RAILWAY.md`,
  `.env.example` comments) keep `portal.example.com` as placeholder for
  third-party self-hosters who will substitute their own domain

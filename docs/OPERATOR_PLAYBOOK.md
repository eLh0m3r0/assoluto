# Operator playbook

Things you do **after the VPS is up** and the app is deployed — the
"day 2" ops work. Complements:

- [`LAUNCH_CHECKLIST.md`](LAUNCH_CHECKLIST.md) — pre-launch admin (domains, trademark, legal)
- [`ENV.md`](ENV.md) — full env var reference
- [`DEPLOY_HETZNER.md`](DEPLOY_HETZNER.md) — VPS provisioning + CI setup

If you're wiring up a fresh production instance, work through
sections 1 → 4 in order. Once live, sections 5+ are per-need.

---

## 1. Enable real billing (Stripe)

Without this, the billing UI runs in **demo mode**: subscriptions are
flipped locally to `trialing` but no money moves, no invoices are
issued, upgrade clicks silently no-op because `stripe_price_id` is
NULL on every plan row.

### 1.1 Stripe account

1. Create Stripe account at [stripe.com](https://stripe.com) under your
   legal entity (not a personal account). Identity verification takes
   ~1 business day.
2. Complete **Activate payments** (Dashboard → Home → Activate your
   account). Until this is done, you only have **test mode keys** —
   fine for staging, not OK for production.
3. If you're a CZ VAT payer, enable **Stripe Tax** (Dashboard → Tax).
   Set your home jurisdiction to Czech Republic, VAT rate 21 %, price
   behavior **Exclusive** ("list prices shown bez DPH"). Czech B2B
   with a valid VAT ID triggers reverse-charge automatically.

### 1.2 Create products + prices

In Dashboard → Products → **+ Add product**, create one product per
plan tier. Each needs a *recurring monthly* price, currency CZK, tax
behavior **Exclusive**.

| Product | Monthly price | Notes |
|---|---|---|
| Starter | 490 CZK | `code = starter` in our DB |
| Pro | 1 490 CZK | `code = pro` |

After saving, click the product → **Pricing** tab → copy the Price
ID (`price_...`) for each.

> **Yearly pricing**: the marketing page mentions "2 months free"
> annual rates (4 900 / 14 900 CZK), but the schema + code don't
> support yearly prices yet. If you want them now, create an
> `app/platform/billing/models.py` `yearly_price_cents` column via
> migration + a `stripe_price_*_yearly` env var + checkout flow
> picks based on a query param. Out of scope for the first launch.

### 1.3 Webhook endpoint

Dashboard → Developers → Webhooks → **+ Add endpoint**:

- URL: `https://assoluto.eu/platform/webhooks/stripe`
- Events to send (minimum):
  - `checkout.session.completed`
  - `invoice.paid`
  - `invoice.payment_failed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`

Click **Add endpoint** → scroll to **Signing secret** → **Reveal** →
copy (`whsec_...`). Each webhook has its own secret; staging and
production should have separate endpoints + separate secrets.

### 1.4 Wire keys into production

Five env vars go into `/etc/assoluto/env` on the VPS:

```bash
ssh -i ~/.ssh/hetzner_assoluto deploy@<VPS_IP>

# Edit (or use sed, see "Atomic edit pattern" at the bottom of this
# file for a safe template).
nano /etc/assoluto/env
```

Fill:

```ini
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_STARTER=price_...
STRIPE_PRICE_PRO=price_...
```

Then roll the web container so the lifespan hook can pick them up:

```bash
cd /opt/assoluto
docker compose --env-file /etc/assoluto/env \
  -f docker-compose.yml -f docker-compose.prod.yml \
  up -d --force-recreate --no-deps web
```

On boot the app logs `stripe_price.sync.updated code=starter
price_id=price_...` — that's `_sync_stripe_prices_from_env` UPSERTing
the env value into `platform_plans.stripe_price_id`. Empty env values
are skipped; existing DB rows are left alone.

### 1.5 Verify

- Navigate `https://assoluto.eu/platform/billing` as a verified
  platform admin.
- Click **Upgrade to Starter** → you should be redirected to Stripe
  Checkout (not a silent 404). Use a
  [test card number](https://docs.stripe.com/testing#cards).
- After successful payment, the webhook fires `invoice.paid` → we
  record the invoice in `platform_invoices` → the dashboard shows the
  row with two download links: **Doklad** (our CZ-compliant PDF) +
  **Stripe PDF** (Stripe-hosted receipt).

---

## 2. Email deliverability (Brevo DKIM)

Without DKIM, Gmail + Microsoft 365 route every outbound mail to
spam. This is the #1 cause of "my users never got the invite" reports.

1. **Brevo dashboard** → Senders & IP → Senders → **Authenticate
   domain** → pick `assoluto.eu`.
2. Brevo generates TXT records for you:
   - `brevo._domainkey.assoluto.eu` (DKIM)
   - `mail._domainkey.assoluto.eu` (secondary — only some sends)
3. **Porkbun DNS** → domain `assoluto.eu` → DNS Records → **+ Add**:
   - Type `TXT`, host `brevo._domainkey`, value from Brevo
   - Save. Repeat for `mail._domainkey` if provided.
4. Wait 15 min for DNS propagation. In Brevo click **Verify**.
5. Upgrade DMARC from monitoring to enforcement after ~2 weeks of
   clean `rua@dmarc.brevo.com` reports:
   ```
   _dmarc.assoluto.eu  TXT  "v=DMARC1; p=quarantine; rua=mailto:rua@dmarc.brevo.com"
   ```
   `p=quarantine` puts failing mail into spam; `p=reject` is the
   final step once you're confident.

Verify end-to-end:

```bash
# Current DNS state
host -t TXT assoluto.eu
host -t TXT brevo._domainkey.assoluto.eu
host -t TXT _dmarc.assoluto.eu

# Send a test through our SMTP path directly
ssh -i ~/.ssh/hetzner_assoluto deploy@<VPS_IP> \
  'cd /opt/assoluto && docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml exec -T web python -c "
from app.config import get_settings
from app.email.sender import SmtpSender
s = get_settings()
SmtpSender(s).send(to=\"you@gmail.com\", subject=\"[assoluto] deliverability check\",
                   html=\"<p>If you see this in inbox (not spam), DKIM is live.</p>\",
                   text=\"OK\")
"'
```

Check the Gmail raw header (⋮ → Show original). Look for:

- `dkim=pass header.i=@assoluto.eu` ✅
- `spf=pass` ✅
- `dmarc=pass` ✅

---

## 3. Off-site backup (rclone)

Daily Postgres dumps run at 03:00 via cron
(`/home/deploy/backups/portal-YYYYMMDD-HHMMSS.sql.gz`, 14-day
rotation). **These live on the same VPS.** One hardware failure = all
customer data gone. Off-site mirror is non-negotiable.

### 3.1 Pick a destination

| Provider | Monthly cost at 10 GB | EU region |
|---|---|---|
| **Backblaze B2** | ~€0.05 | Yes (EU Central) |
| **Hetzner Storage Box** | €3.50 flat (1 TB) | Yes |
| **Wasabi** | ~€0.07 | Yes (Amsterdam) |

B2 is cheapest for small volumes; Hetzner Storage Box is already
inside your provider so no egress fees.

### 3.2 Install rclone on the VPS

```bash
ssh -i ~/.ssh/hetzner_assoluto deploy@<VPS_IP>

# rclone needs sudo for /usr/local/bin install; if you don't have
# sudo, drop the binary in /home/deploy/bin/ instead.
curl https://rclone.org/install.sh | sudo bash

rclone version
```

### 3.3 Configure the remote

```bash
rclone config
# n) New remote
# name: b2   (or hetzner, wasabi — match scripts/backup.sh env var)
# Storage type: backblaze-b2 (or s3 for Wasabi, sftp for Hetzner)
# Account ID + Application Key: from your provider dashboard
# Accept defaults
# q) Quit
```

Test the remote:

```bash
rclone lsd b2:
# Should list your buckets (may be empty)

rclone mkdir b2:assoluto-backups
```

### 3.4 Wire into the backup script

Add to `/etc/assoluto/env`:

```ini
RCLONE_REMOTE=b2:assoluto-backups
```

The cron runs `scripts/backup.sh` which already picks `RCLONE_REMOTE`
up — after the pg_dump completes locally it `rclone copy`-ies to the
remote.

### 3.5 Verify

```bash
# Force a manual run
bash /opt/assoluto/scripts/backup.sh
tail -20 /home/deploy/assoluto-backup.log

# Should see lines like:
# [backup] Syncing to b2:assoluto-backups
# [backup] Transferred: portal-20260423-215142.sql.gz

# And on the remote:
rclone ls b2:assoluto-backups
```

Run a **restore drill** every 3 months — `rclone copy` a dump back,
`gunzip`, `psql -d portal_test < dump.sql` into a throw-away DB.
Dumps you can't restore aren't backups.

---

## 4. VAT / invoice identity

CZ tax-compliant invoice PDFs (`/platform/billing/invoices/{id}.pdf`)
are generated from operator identity in env + per-tenant fields.

### Operator side (env on VPS)

| Var | Required | Notes |
|---|---|---|
| `PLATFORM_OPERATOR_NAME` | yes | Legal entity name |
| `PLATFORM_OPERATOR_ICO` | yes | IČO, 8 digits |
| `PLATFORM_OPERATOR_DIC` | if VAT-registered | Format `CZxxxxxxxx`. Empty = non-payer, simpler "Faktura" header |
| `PLATFORM_OPERATOR_ADDRESS` | yes | Free text, used on `/imprint` and invoice |
| `PLATFORM_OPERATOR_EMAIL` | yes | Contact for legal pages |

After editing, roll the web container (see §1.4). Invoice templates
re-read each request.

### Tenant side (per paying customer)

When the customer's VAT ID changes, update `tenants.settings` JSONB:

```sql
UPDATE tenants
  SET settings = jsonb_set(
      COALESCE(settings, '{}'::jsonb),
      '{billing_dic}', to_jsonb('CZ12345678'::text))
  WHERE slug = 'acmeco';
```

Fields consumed by the invoice PDF under `tenants.settings`:

- `billing_name` (optional, falls back to `tenants.name`)
- `billing_address`
- `billing_ico`
- `billing_dic`

No schema change needed — `settings` is free-form JSONB already used
by `default_locale`.

---

## 5. Managing plans after launch

Use this decision matrix before changing anything plan-related:

| What you're changing | Where to change it |
|---|---|
| Stripe price ID (Stripe rotated it) | ENV → `STRIPE_PRICE_*` → roll web container |
| Plan limit (e.g. Starter 3→5 users) | DB → `UPDATE platform_plans` |
| Plan price in CZK (e.g. 490→590) | ALL of: Stripe (new Price), ENV (new ID), DB (cents), template |
| Feature list on pricing page | `app/templates/www/pricing.html` (commit + deploy) |
| New tier added (Pro+ between Pro and Enterprise) | Migration (new row) + ENV (new price ID) + template |
| Plan removed from signup | DB → `UPDATE platform_plans SET is_active = false` |

### 5.1 Raise a limit

Starter should get 50 contacts instead of 20:

```bash
ssh -i ~/.ssh/hetzner_assoluto deploy@<VPS_IP>
cd /opt/assoluto
docker compose --env-file /etc/assoluto/env \
  -f docker-compose.yml -f docker-compose.prod.yml \
  exec -T postgres psql -U portal -d portal \
  -c "UPDATE platform_plans SET max_contacts = 50 WHERE code = 'starter';"
```

No deploy needed — `ensure_within_limit` reads the current DB row on
every creation request. Update `app/templates/www/pricing.html` in
the same pass to keep the marketing page honest.

### 5.2 Change a monthly price

Existing subscribers **stay on the old price** because Stripe holds the
Price ID on their subscription object. Only new checkouts pick the
new price.

```
1. Stripe: Products → Starter → + Add another price → 590 CZK/month
   → copy new price_id

2. VPS /etc/assoluto/env:
   STRIPE_PRICE_STARTER=price_NEW_ID   # replace the old value

3. psql on prod (via docker exec):
   UPDATE platform_plans
     SET monthly_price_cents = 59000, currency = 'CZK'
     WHERE code = 'starter';

4. Repo: app/templates/www/pricing.html: "490 CZK" → "590 CZK".
   Commit + push → deploy workflow picks it up.

5. Roll the web container so the boot-time sync picks up the new
   env value:
   docker compose ... up -d --force-recreate --no-deps web
```

Boot log should show `stripe_price.sync.updated code=starter
price_id=price_NEW_ID`. Verify from the pricing page.

### 5.3 Add a new tier

```
1. Stripe: new Product + Price. Copy price_id.

2. Repo:
   - alembic revision --autogenerate -m "add pro_plus plan"
     (or handwrite an op.execute INSERT into platform_plans)
   - app/config.py: add `stripe_price_pro_plus` Field
   - app/main.py::_sync_stripe_prices_from_env: add
     "pro_plus": settings.stripe_price_pro_plus
   - app/templates/www/pricing.html: new card
   - .env.example: document STRIPE_PRICE_PRO_PLUS

3. Deploy (migration runs automatically via docker entrypoint).

4. VPS /etc/assoluto/env:
   STRIPE_PRICE_PRO_PLUS=price_...
   → roll web container.
```

### 5.4 Discontinue a tier

Existing subscribers keep their plan (Stripe drives billing); only
signup/upgrade UI stops offering it.

```sql
UPDATE platform_plans SET is_active = false WHERE code = 'starter';
```

Remove the card from `pricing.html` + commit. No Stripe action
required — Stripe Prices don't get "discontinued", just not-offered.

---

## 6. Per-environment notes

The DB column `platform_plans.stripe_price_id` is **environment-
local**: staging and production have different Stripe accounts, so the
same plan row shouldn't share a price ID. The app enforces this
implicitly — the boot-time sync reads env and writes what's there.
Just make sure you use **live mode** price IDs on production and
**test mode** price IDs on staging.

Webhook secrets are per-endpoint: staging and production have
separate Stripe endpoints and separate secrets. Don't share.

---

## 7. Atomic edit pattern for `/etc/assoluto/env`

Always back up before editing:

```bash
ssh -i ~/.ssh/hetzner_assoluto deploy@<VPS_IP>

cp /etc/assoluto/env /etc/assoluto/env.bak-$(date +%Y%m%d-%H%M%S)

# Single-key update without re-typing the whole file:
sed -i 's|^STRIPE_SECRET_KEY=.*|STRIPE_SECRET_KEY=sk_live_new|' \
    /etc/assoluto/env

# Verify the diff
diff /etc/assoluto/env.bak-<timestamp> /etc/assoluto/env
```

Never commit `/etc/assoluto/env` to git. It's deploy-owned mode 600;
only the `deploy` user reads it.

---

## 8. What's intentionally NOT managed by ops

These are engineering tasks, not operator config:

- **Plan `max_users` enforcement** — coded in `app/platform/usage.py::ensure_within_limit`.
- **Stripe webhook event handling** — coded in `app/platform/billing/webhooks.py`. New event types require a code change.
- **Tax calculation** — delegated to Stripe Tax. We just pass the VAT ID; Stripe computes.
- **Invoice PDF layout** — `app/services/invoice_pdf_service.py`. Compliance tweaks = code change.

If you find yourself wanting to edit these via SQL or env, stop and
file a ticket instead.

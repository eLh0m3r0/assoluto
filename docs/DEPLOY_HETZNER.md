# Deploying to a Hetzner Cloud VPS

End-to-end guide to running Assoluto in production on a
single Hetzner Cloud VPS with automatic zero-touch deploys from GitHub.
Every push to the `production` branch builds a new Docker image, pushes
it to GitHub Container Registry (GHCR), SSHes into the VPS, and rolls
the `web` service. Migrations run automatically via the container
entrypoint.

> **Scope.** One VPS running Postgres + the FastAPI app + an Nginx
> reverse proxy, with external managed services for object storage and
> transactional email. This is the shape recommended for first-launch
> SaaS up to a few thousand tenants. See [`DEPLOY_SAAS.md`](DEPLOY_SAAS.md)
> for higher-scale trade-offs and the multi-tenant ops model.

---

## 0. Who does what — automation map

Every step below is tagged with an actor:

| Tag | Meaning |
|-----|---------|
| 🧑 **You** | Must be done by a human — account signups, billing, DNS records at your registrar, GitHub web-UI actions, identity checks. |
| 💻 **Claude on your laptop** | Safe to delegate to Claude Code running locally — file edits, keypair generation, docker buildx. |
| 🖥️ **Claude on the VPS** | After you SSH in and launch Claude Code there, hand it the prompt in [§A — Delegation brief](#a--delegation-brief-for-claude-on-the-vps). It executes the server-side setup end-to-end. |
| ⚙️ **GitHub Actions** | Fully automated once the `production` branch and secrets exist. |

### Responsibility at a glance

| Section | What | Actor |
|---------|------|-------|
| §2 Prerequisites | Hetzner + domain + B2/R2 + Postmark + Stripe account creation; domain purchase | 🧑 You |
| §3.1 Provision VPS | Click through Hetzner cloud console, upload your SSH pubkey, note the IP | 🧑 You |
| §3.2 Baseline hardening | apt upgrade, create `deploy` user, disable root SSH, UFW, fail2ban | 🖥️ Claude on the VPS |
| §3.3 Docker install | `get.docker.com` + group add | 🖥️ Claude on the VPS |
| §4 DNS records | Create `A portal` and `A *.portal` at your registrar / Cloudflare | 🧑 You |
| §4.1 Cloudflare API token | Create token in Cloudflare UI | 🧑 You |
| §4.1 Run certbot | Install plugin, drop the token file, request the cert | 🖥️ Claude on the VPS |
| §5.1 B2 bucket + keys | Create bucket and application key in Backblaze UI | 🧑 You |
| §5.2 Postmark server + DNS | Create server, verify domain (SPF/DKIM/DMARC records at registrar) | 🧑 You |
| §5.3 Stripe setup | Stripe dashboard + webhook endpoint | 🧑 You |
| §6 App layout on VPS | Clone repo, write `/etc/assoluto/env`, patch nginx.conf + compose | 🖥️ Claude on the VPS |
| §7 First manual deploy | Pull image, `docker compose up`, fix Postgres password, health check | 🖥️ Claude on the VPS |
| §7.5 Create first tenant | `python -m scripts.create_tenant` | 🖥️ Claude on the VPS |
| §8.1 Add GitHub secrets | Paste SSH private key, host, user into repo Settings | 🧑 You |
| §8.1 Generate deploy keypair | `ssh-keygen` + install pubkey in `authorized_keys` | 💻 Claude on your laptop + 🖥️ Claude on the VPS |
| §8.2 Create `production` branch | `git checkout -b production && git push` | 🧑 You (one-time) |
| §8.3 Every later deploy | Build image, SSH to VPS, roll web, health check | ⚙️ GitHub Actions |
| §10.1 Backup cron | Script + crontab | 🖥️ Claude on the VPS |

> **Bottom line.** You spend an hour in browsers and registrar dashboards
> collecting a list of 12 values (§2a below). You SSH into the VPS,
> launch Claude Code, paste it the delegation brief from §A, and it
> executes the rest. After that every push to `production` is hands-off.

---

## 1. What you'll build

```
             ┌────────────────────────────── Cloudflare DNS ─┐
             │                                                │
  browser ───┤  https://assoluto.eu                    │
             │  https://<tenant>.assoluto.eu           │
             └──────┬─────────────────────────────────────────┘
                    │  HTTPS (TLS terminated by nginx on VPS)
                    ▼
            ┌───────────────────────── Hetzner CX22 / CX32 ──┐
            │                                                  │
            │  nginx:443  →  web:8000  (FastAPI + uvicorn)     │
            │                  │                                │
            │                  ├─►  postgres:5432  (local vol)  │
            │                  │                                │
            │                  ├─►  Backblaze B2 / R2  (S3 API) │
            │                  │                                │
            │                  └─►  Postmark / SES  (SMTP)      │
            │                                                  │
            └──────────────────────────────────────────────────┘
                    ▲
                    │  SSH push-deploy
                    │
            GitHub Actions ──► build image ──► GHCR
              (push to branch `production`)
```

**Key design choices and why:**

- **Single VPS, not Kubernetes.** One-box ops is fine until you're
  regularly saturating a 4-vCPU box. Keep it boring.
- **External S3-compatible storage.** Attachments outlive containers;
  running MinIO on the same box defeats the point of having a managed
  datastore. Backblaze B2 (EU) or Cloudflare R2 (no egress fees) are
  both good choices.
- **External SMTP.** Self-hosting mail delivery will eat a week and
  still land you in spam folders. Use Postmark, Resend, SES, or
  Mailgun.
- **Postgres on the box (for now).** Moving to a managed Postgres
  (Neon, Hetzner Managed DB, RDS) is a one-env-var change later. Start
  simple and take pg_dump backups to S3.
- **Nginx for TLS + wildcard.** The app listens on plain HTTP inside
  the Docker network; Nginx terminates TLS and serves the wildcard
  cert for `*.assoluto.eu`.

---

## 2. Prerequisites

Before you start you need:

| Item | Where | Notes |
|------|-------|-------|
| Hetzner Cloud account | console.hetzner.cloud | EU VAT-friendly, no credit card holds |
| A domain you control | any registrar | `assoluto.eu` in this guide |
| SSH keypair on your laptop | `~/.ssh/id_ed25519` | `ssh-keygen -t ed25519 -C "deploy@assoluto"` if you don't have one |
| GitHub repo with this codebase | github.com | Public or private both fine |
| Backblaze B2 or Cloudflare R2 account | respective dashboards | For attachments |
| Postmark / SES / Mailgun account | respective dashboards | For transactional email |
| Stripe account (only if selling) | dashboard.stripe.com | Only needed when `FEATURE_PLATFORM=true` and you're taking payments |

Budget: expect ~12–25 EUR/month all-in for a small production
deployment (CX22 VPS ~5 EUR + B2 storage pennies + Postmark free tier
+ domain amortised).

---

## 2a. Preflight checklist — 🧑 only you can do this

Before you SSH anywhere, collect the values below in a secure note (a
1Password item, a local file you'll `shred` afterwards, whatever). The
delegation brief in §A expects all of them to be handed over in one
block to Claude on the VPS, so having them in one place makes the
handoff a single paste.

This whole section is human-only because each item requires an account,
a billing card, or a DNS record you alone can create.

**Domain & DNS**

- [ ] `PORTAL_DOMAIN` — e.g. `assoluto.eu`
- [ ] `ACME_EMAIL` — the address Let's Encrypt will email on cert
      expiry (e.g. `ops@assoluto.eu`)
- [ ] Two **A records** created at your registrar / DNS host:
      `assoluto.eu → <VPS IP>` and
      `*.assoluto.eu → <VPS IP>`
- [ ] `CLOUDFLARE_API_TOKEN` — only if your DNS is on Cloudflare;
      zone-scoped, Zone:DNS:Edit. (If DNS is elsewhere, use the Caddy
      path in §9 and collect the provider's API token instead.)

**Hetzner**

- [ ] Hetzner Cloud account exists and has a payment method on file
- [ ] Your laptop's SSH public key (`~/.ssh/id_ed25519.pub`) added to
      the Hetzner console under *Security → SSH Keys*
- [ ] VPS provisioned per §3.1 — note the **public IPv4** as
      `VPS_IP`

**Object storage (Backblaze B2 or Cloudflare R2)**

- [ ] `S3_ENDPOINT_URL` (from the bucket page)
- [ ] `S3_BUCKET` — e.g. `assoluto-attachments`
- [ ] `S3_REGION` — e.g. `eu-central-003`
- [ ] `S3_ACCESS_KEY` / `S3_SECRET_KEY`

**Transactional email (Postmark, SES, Resend, Mailgun, …)**

- [ ] Sending domain DKIM + SPF + DMARC records added at the
      registrar and **verified** in the provider dashboard
- [ ] `SMTP_HOST`, `SMTP_PORT` (usually `587`)
- [ ] `SMTP_USER`, `SMTP_PASSWORD`
- [ ] `SMTP_FROM` — e.g. `no-reply@assoluto.eu`

**Stripe** (skip if you're not selling subscriptions yet — leave
`FEATURE_PLATFORM=false`)

- [ ] `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`
- [ ] `STRIPE_WEBHOOK_SECRET` (from the webhook endpoint you create
      pointing at `https://assoluto.eu/platform/stripe/webhook`)
- [ ] `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` (price IDs from the
      Stripe dashboard)

**GitHub**

- [ ] A **GitHub Personal Access Token (classic)** with
      `read:packages` scope, so the VPS can pull from GHCR. Call this
      `GHCR_PULL_TOKEN`. (Skip if your GHCR image is public.)
- [ ] A **deploy keypair** for CI. Generate with
      `ssh-keygen -t ed25519 -f ~/.ssh/assoluto_deploy -N ''`.
      The public half goes into the VPS's `authorized_keys`; the
      private half goes into the `DEPLOY_SSH_KEY` repo secret.
- [ ] Repository secrets added under **Settings → Secrets and
      variables → Actions**: `DEPLOY_HOST`, `DEPLOY_USER=deploy`,
      `DEPLOY_SSH_KEY`, optional `DEPLOY_PORT`.

Once every box is ticked, you're ready to SSH into the VPS and hand
off the rest to Claude. Jump to [§A — Delegation brief](#a--delegation-brief-for-claude-on-the-vps).

Everything in §3–§7 below is **reference detail** that Claude on the
VPS will execute. Read it to understand what will happen, but you
don't have to type any of it yourself.

---

## 3. Provision the Hetzner VPS

### 3.1. 🧑 Create the server — you

In the Hetzner Cloud console:

1. **New server** → Location: Falkenstein or Nuremberg (EU, low latency
   to Prague).
2. **Image:** Ubuntu 24.04 LTS.
3. **Type:** **CX22** (2 vCPU, 4 GB RAM, 40 GB NVMe) is enough for
   first-launch single-region SaaS. Upgrade to **CX32** (4 vCPU, 8 GB)
   once you're regularly running near 70% load.
4. **Networking:** Public IPv4 on, IPv6 on.
5. **SSH key:** paste your `~/.ssh/id_ed25519.pub` contents. This
   becomes the initial root key.
6. **Name:** `assoluto-prod`.
7. Create. You'll get a public IPv4 address — write it down.

### 3.2. 🖥️ First login and baseline hardening — Claude on the VPS

```bash
# Replace 1.2.3.4 with the IP Hetzner assigned you.
ssh root@1.2.3.4

# Inside the VPS:
apt-get update && apt-get -y upgrade
apt-get -y install ufw fail2ban ca-certificates curl gnupg git unattended-upgrades

# Enable unattended security upgrades
dpkg-reconfigure --priority=low unattended-upgrades

# Create a non-root sudo user for day-to-day work and for the
# GitHub Actions SSH deploy key.
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
# Allow deploy to run docker compose without a password (needed for the CI job).
echo "deploy ALL=(ALL) NOPASSWD: /usr/bin/docker, /usr/bin/sed, /usr/bin/tee" > /etc/sudoers.d/deploy-docker
chmod 440 /etc/sudoers.d/deploy-docker

# Disable SSH password auth and root login
sed -i -E 's/^#?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i -E 's/^#?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload ssh

# Firewall — only 22, 80, 443 open
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# fail2ban with Ubuntu defaults is already good for ssh; leave it on.
systemctl enable --now fail2ban
```

Log out of root, re-test the `deploy` user:

```bash
ssh deploy@1.2.3.4   # should work
```

From now on, do everything as `deploy` with `sudo` when needed.

### 3.3. 🖥️ Install Docker — Claude on the VPS

```bash
# On the VPS, as deploy with sudo
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy
# Re-login so the group takes effect (or run `newgrp docker`).

docker --version             # 27.x expected
docker compose version       # v2.x expected
```

---

## 4. 🧑 DNS configuration — you

You need two records pointing at the VPS IP:

| Type | Name                    | Value        | TTL |
|------|-------------------------|--------------|-----|
| A    | `assoluto.eu`    | `1.2.3.4`    | 300 |
| A    | `*.assoluto.eu`  | `1.2.3.4`    | 300 |

The wildcard is **mandatory** — each tenant gets a subdomain like
`acme.assoluto.eu`.

If your DNS is on Cloudflare, you can leave the orange cloud **off**
(grey cloud / DNS-only). Terminating TLS yourself on the VPS keeps
origin rules simple. Turn the proxy on later if you want CDN + DDoS
protection.

### 4.1. Wildcard TLS — obtain a certificate

> **Split.** 🧑 You create the Cloudflare API token in the Cloudflare
> UI (only a human can consent to that). 🖥️ Claude on the VPS runs
> the certbot installation and the cert request.

Let's Encrypt supports wildcard certs only via DNS-01. The cleanest
path is **certbot with the Cloudflare DNS plugin**:

```bash
sudo apt-get -y install certbot python3-certbot-dns-cloudflare

# Create an API token scoped to the single zone:
# Cloudflare → My Profile → API Tokens → Create Token
# Template: "Edit zone DNS". Zone Resources → Include → example.com.
# Permissions: Zone:DNS:Edit. Save the token.

sudo mkdir -p /etc/letsencrypt/secrets
sudo tee /etc/letsencrypt/secrets/cloudflare.ini >/dev/null <<'EOF'
dns_cloudflare_api_token = YOUR_TOKEN_HERE
EOF
sudo chmod 600 /etc/letsencrypt/secrets/cloudflare.ini

sudo certbot certonly \
    --dns-cloudflare \
    --dns-cloudflare-credentials /etc/letsencrypt/secrets/cloudflare.ini \
    -d assoluto.eu \
    -d '*.assoluto.eu' \
    --agree-tos --email ops@assoluto.eu --non-interactive
```

Certbot stores the cert at
`/etc/letsencrypt/live/assoluto.eu/{fullchain,privkey}.pem`.

Renewal is automatic via the `certbot.timer` systemd unit (every 12 h).
We'll mount a symlinked copy into the Nginx container below.

If your DNS is **not** on Cloudflare, use Caddy as a drop-in replacement
for the Nginx container — it handles DNS-01 natively against many
providers. See [§9 Alternative: Caddy](#9-alternative-caddy-instead-of-nginx).

---

## 5. External services — 🧑 you

Every subsection here is account setup, billing, and domain
verification. None of it can be safely delegated to an agent.

### 5.1. Object storage (Backblaze B2 example)

1. Sign up at backblaze.com, enable B2 Cloud Storage.
2. **Create Bucket:** name `assoluto-attachments`, private.
3. **Application Keys → Add a New Application Key:**
   - Name: `assoluto-prod`
   - Allow access to: this bucket only
   - Type of access: Read and Write
4. Save the **keyID** and **applicationKey** — you only see them once.
5. Note the **S3 endpoint** from the bucket page (e.g. `https://s3.eu-central-003.backblazeb2.com`).

Env vars you'll set later:

```
S3_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_ACCESS_KEY=<keyID>
S3_SECRET_KEY=<applicationKey>
S3_BUCKET=assoluto-attachments
S3_REGION=eu-central-003
```

Cloudflare R2 works identically — replace the endpoint with the R2 one
and use R2 access keys.

### 5.2. Transactional email (Postmark example)

1. Create a Postmark server, then a "transactional" message stream.
2. Verify your sending domain (add SPF, DKIM, DMARC records).
3. Generate a **Server API token**.

Env vars:

```
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=<Server API token>
SMTP_PASSWORD=<Server API token>     # Postmark uses the same value for both
SMTP_FROM=no-reply@assoluto.eu
```

Send a test email once the app is running via the signup flow or a
`scripts/send_test_email.py` script.

### 5.3. Stripe (only if `FEATURE_PLATFORM=true` and taking payments)

1. **Test mode first.** Never configure live keys until end-to-end works
   on test keys.
2. Create prices for your plans in the Stripe dashboard; copy the
   `price_...` IDs.
3. Create a webhook endpoint pointing at
   `https://assoluto.eu/platform/stripe/webhook` — select events
   per [`docs/DEPLOY_SAAS.md`](DEPLOY_SAAS.md#stripe-webhook-events).
4. Copy the webhook signing secret.

Env vars:

```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_STARTER=price_...
STRIPE_PRICE_PRO=price_...
```

---

## 6. 🖥️ Application layout on the VPS — Claude on the VPS

We'll keep the compose files and nginx config under `/opt/assoluto`
(readable) and secrets under `/etc/assoluto/env` (root-readable
only).

```bash
# As deploy on the VPS
sudo mkdir -p /opt/assoluto /etc/assoluto
sudo chown deploy:deploy /opt/assoluto

# Clone the repo — only for its compose + nginx config. The actual
# app code is pulled as a Docker image from GHCR.
git clone https://github.com/<your-org>/sme-client-portal.git /opt/assoluto
cd /opt/assoluto
git checkout production   # stay on the deployed branch
```

### 6.1. Nginx config

Copy the template and replace the hostnames:

```bash
sudo mkdir -p /opt/assoluto/docker
cp /opt/assoluto/docker/nginx.conf.example /opt/assoluto/docker/nginx.conf
sed -i 's/portal\.example\.com/portal.YOUR-DOMAIN.com/g' /opt/assoluto/docker/nginx.conf

# Create the common proxy include that the vhost file references.
sudo tee /opt/assoluto/docker/common-proxy.inc >/dev/null <<'EOF'
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_read_timeout 60s;
proxy_connect_timeout 5s;
EOF
```

Now patch `docker-compose.prod.yml` **on the VPS** to also mount the
certbot cert directory and the `common-proxy.inc` file into the Nginx
container. Edit `/opt/assoluto/docker-compose.prod.yml` and replace
the `nginx.volumes` block with:

```yaml
    volumes:
      - ./docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./docker/common-proxy.inc:/etc/nginx/conf.d/common-proxy.inc:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
```

And change the two `ssl_certificate*` lines in
`/opt/assoluto/docker/nginx.conf` to:

```nginx
ssl_certificate     /etc/letsencrypt/live/portal.YOUR-DOMAIN.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/portal.YOUR-DOMAIN.com/privkey.pem;
```

> Don't commit these VPS-specific edits back to the repo — they are
> per-environment. Keep `docker/nginx.conf.example` as the template in
> git. (Alternative: template the host via env substitution; for one
> VPS that's overkill.)

### 6.2. The env file — `/etc/assoluto/env`

Every credential lives here. It is read by `docker compose --env-file`
and is never in git.

```bash
# Generate strong random passwords and a session secret
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # APP_SECRET_KEY
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # PORTAL_OWNER_PASSWORD
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # PORTAL_APP_PASSWORD

sudo tee /etc/assoluto/env >/dev/null <<'EOF'
# ---- Image tag (overwritten by the CI job on every deploy) ----
APP_IMAGE_TAG=latest

# ---- Core ----
APP_SECRET_KEY=<paste the 48-byte token>
APP_BASE_URL=https://assoluto.eu
LOG_LEVEL=INFO

# ---- Database ----
PORTAL_OWNER_PASSWORD=<paste the 32-byte token>
PORTAL_APP_PASSWORD=<paste another 32-byte token>

# ---- S3 (Backblaze B2 / R2) ----
S3_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_PUBLIC_ENDPOINT_URL=
S3_ACCESS_KEY=<keyID>
S3_SECRET_KEY=<applicationKey>
S3_BUCKET=assoluto-attachments
S3_REGION=eu-central-003

# ---- SMTP (Postmark) ----
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=<postmark token>
SMTP_PASSWORD=<postmark token>
SMTP_FROM=no-reply@assoluto.eu

# ---- Platform / SaaS (set true only when billing + signup are live) ----
FEATURE_PLATFORM=false
PLATFORM_COOKIE_DOMAIN=.assoluto.eu

# ---- Limits ----
MAX_UPLOAD_SIZE_MB=50
EOF

sudo chmod 600 /etc/assoluto/env
sudo chown root:root /etc/assoluto/env
```

> **Postgres password gotcha.** `docker/postgres-init.sql` ships with a
> hardcoded placeholder password for `portal_app`. On first boot,
> immediately ALTER it to match `PORTAL_APP_PASSWORD`:
>
> ```bash
> # After the stack is up for the first time (see §7):
> sudo docker compose --env-file /etc/assoluto/env \
>     -f docker-compose.yml -f docker-compose.prod.yml \
>     exec postgres psql -U portal -d portal \
>     -c "ALTER ROLE portal_app WITH PASSWORD '$(sudo grep PORTAL_APP_PASSWORD /etc/assoluto/env | cut -d= -f2)';"
> ```
>
> This is a one-time fix. For a cleaner setup, fork `postgres-init.sql`
> in your deploy repo and template the password.

---

## 7. 🖥️ First manual deploy — Claude on the VPS

Before wiring up GitHub Actions, do one manual deploy to prove the
stack works end-to-end.

### 7.1. Authenticate the VPS with GHCR

The `deploy` user needs to be able to pull images. Create a GitHub
**Personal Access Token (classic)** with scope `read:packages`, then:

```bash
# On the VPS, as deploy
echo "<your-PAT>" | sudo docker login ghcr.io -u <your-github-username> --password-stdin
```

(If your image is in a public GitHub package you can skip this.)

### 7.2. 💻 Build + push a first image from your laptop — optional

You don't strictly need to — GitHub Actions will build one when you
push to `production`. But for the first manual boot it's nice to have
a known-good image in GHCR already. Either run this yourself or hand
the docker commands to Claude on your laptop:

```bash
# On your laptop, from the repo root
docker buildx build --platform linux/amd64 \
    -t ghcr.io/<your-org>/sme-client-portal:bootstrap \
    --push .
```

### 7.3. Boot the stack

```bash
# On the VPS, as deploy
cd /opt/assoluto

# Pin the bootstrap tag
sudo sed -i 's/^APP_IMAGE_TAG=.*/APP_IMAGE_TAG=bootstrap/' /etc/assoluto/env

sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml \
    pull

sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml \
    up -d

# Watch the web container come up — migrations run via entrypoint.sh
sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml \
    logs -f web
```

You should see:

```
[entrypoint] Postgres reachable at postgres:5432
[entrypoint] Running alembic upgrade head...
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_*, ...
INFO  Uvicorn running on http://0.0.0.0:8000
```

Fix the Postgres password now (see the admonition at the end of §6.2).

### 7.4. Verify

```bash
# Healthz via the web container
sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml \
    exec web curl -fsS http://127.0.0.1:8000/healthz
# {"status":"ok"}

# From your laptop
curl -fsS https://assoluto.eu/healthz
# {"status":"ok"}
```

### 7.5. Create the first tenant

```bash
sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml \
    exec web python -m scripts.create_tenant acme admin@acme.com
```

Visit `https://acme.assoluto.eu` and log in with the credentials
the script printed.

---

## 8. Automatic deploy on merge to `production`

The workflow is already in the repo at
[`.github/workflows/deploy-production.yml`](../.github/workflows/deploy-production.yml).
After this section is set up correctly, every push to `production` is
⚙️ fully automated.

### 8.1. 🧑 Add GitHub secrets — you

Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name        | Value                                                                              |
|--------------------|------------------------------------------------------------------------------------|
| `DEPLOY_HOST`      | Public IPv4 of the VPS (e.g. `1.2.3.4`)                                            |
| `DEPLOY_USER`      | `deploy`                                                                            |
| `DEPLOY_SSH_KEY`   | **Private key** of a dedicated deploy keypair (full file contents, OpenSSH format) |
| `DEPLOY_PORT`      | Optional, leave unset for 22                                                        |

Generate a dedicated keypair for CI (don't reuse your personal one):

```bash
# On your laptop
ssh-keygen -t ed25519 -f ~/.ssh/assoluto_deploy -C "gha-deploy@assoluto" -N ''
# Paste the .pub into /home/deploy/.ssh/authorized_keys on the VPS
# Paste the private key (entire contents of ~/.ssh/assoluto_deploy) into DEPLOY_SSH_KEY
```

### 8.2. 🧑 Create the `production` branch — you (one-time)

```bash
# From your laptop, on main (or whatever has been tested)
git checkout -b production
git push -u origin production
```

Your workflow now triggers whenever you push to `production`. The
typical flow is:

```bash
# On main after a feature has been tested
git checkout production
git merge --ff-only main
git push
# …wait for GitHub Actions green tick…
```

### 8.3. ⚙️ What happens during a deploy — automatic

The workflow does exactly this:

1. Checks out the `production` ref.
2. Builds `Dockerfile` for `linux/amd64` using GHA build cache.
3. Pushes two tags to GHCR: `:<short-sha>` and `:production`.
4. SSHes in as `deploy` and:
   - Rewrites `APP_IMAGE_TAG` in `/etc/assoluto/env` to the new SHA.
   - `docker compose pull web`
   - `docker compose up -d --no-deps --remove-orphans web` — this
     recreates only the web container. Postgres and nginx keep running.
   - The container entrypoint runs `alembic upgrade head` before
     uvicorn starts.
   - Polls `curl /healthz` inside the container for 60 s.
   - Fails the job with the last 200 lines of web logs if it never
     goes healthy.

Deploy time: ~2–4 minutes cold, ~90 s warm (GHA cache).

### 8.4. First automated deploy

Merge anything into `production` and watch the **Actions** tab. The
first run will build from scratch (no cache), subsequent runs pull
cached layers.

If the run fails at the SSH step:
- Check the VPS fingerprint got recorded. `appleboy/ssh-action` sets
  `StrictHostKeyChecking no` by default, so this is usually the
  secrets being wrong.
- Test the key manually: `ssh -i ~/.ssh/assoluto_deploy deploy@<IP>`.
- Verify `deploy` can run `sudo docker …` without a password.

---

## 9. Alternative: Caddy instead of Nginx

If your DNS is not on Cloudflare, or you don't want to fiddle with
certbot, replace the `nginx` service in `docker-compose.prod.yml` with
a Caddy container. Caddy handles ACME DNS-01 against most providers
natively.

Minimal `Caddyfile`:

```
assoluto.eu, *.assoluto.eu {
    tls {
        dns hetzner {env.HETZNER_API_TOKEN}   # or cloudflare, route53, digitalocean, …
    }
    encode gzip
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
    reverse_proxy web:8000
}
```

Compose service:

```yaml
  caddy:
    image: caddy:2
    restart: always
    depends_on: [web]
    ports: ["80:80", "443:443"]
    environment:
      HETZNER_API_TOKEN: ${HETZNER_API_TOKEN:?}
    volumes:
      - ./docker/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
```

Plus `caddy_data` and `caddy_config` in the top-level `volumes:` block.
Caddy trades a few MB of memory for not having to think about certs
again.

---

## 10. Operations

### 10.1. 🖥️ Postgres backups — Claude on the VPS (once)

Schedule a daily `pg_dump` to S3. Create
`/opt/assoluto/scripts/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
DEST="s3://assoluto-backups/postgres/${STAMP}.sql.gz"

sudo docker compose --env-file /etc/assoluto/env \
    -f /opt/assoluto/docker-compose.yml \
    -f /opt/assoluto/docker-compose.prod.yml \
    exec -T postgres pg_dump -U portal portal \
    | gzip -9 \
    | aws --endpoint-url "${S3_ENDPOINT_URL}" s3 cp - "${DEST}"
```

Then:

```bash
chmod +x /opt/assoluto/scripts/backup.sh

# /etc/cron.d/assoluto-backup
0 3 * * * deploy /opt/assoluto/scripts/backup.sh >> /var/log/assoluto-backup.log 2>&1
```

Test the restore path against a staging instance at least once before
you pretend you have backups.

### 10.2. Uptime monitoring

Point **UptimeRobot** or **Better Stack** at
`https://assoluto.eu/healthz`. A 5-minute interval is plenty.

### 10.3. Logs

```bash
# Live tail
sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml logs -f web

# Last hour
sudo docker compose … logs --since 1h web
```

The app emits structured JSON when `LOG_JSON=true` (default in the
prod overlay). Ship to Loki, Datadog, or Better Stack Logs once you
need searchable history.

### 10.4. Rollback

Every deploy keeps the previous image on the VPS. To roll back:

```bash
# List tags currently pulled on the VPS
sudo docker image ls | grep sme-client-portal

# Edit /etc/assoluto/env and set APP_IMAGE_TAG to a previous SHA
sudo $EDITOR /etc/assoluto/env

# Roll web only — Postgres stays up
sudo docker compose --env-file /etc/assoluto/env \
    -f docker-compose.yml -f docker-compose.prod.yml \
    up -d --no-deps --remove-orphans web
```

**Migrations that added columns** are safely backward-compatible if
you added them nullable or with defaults (which Alembic does by
default in this repo). **Migrations that dropped columns** require an
explicit forward-fix — rolling back past them means recreating the
column. The usual discipline: do schema changes over two deploys
(add → backfill → switch → drop), never in one.

---

## 11. Security checklist

Before you point real users at the box:

- [ ] SSH password auth disabled, root login disabled, fail2ban running
- [ ] UFW enabled, only 22/80/443 open
- [ ] `unattended-upgrades` enabled (monthly reboot for kernel CVEs)
- [ ] `/etc/assoluto/env` is `chmod 600 root:root`
- [ ] `APP_SECRET_KEY` is ≥ 32 bytes of true randomness and unique
      per environment (never copied from dev)
- [ ] `APP_DEBUG=false`, `LOG_JSON=true` in the prod env
- [ ] Postgres only listens on the Docker network (check: no port
      published in `docker-compose.prod.yml` — it isn't, by design)
- [ ] TLS cert auto-renews (`sudo certbot renew --dry-run` succeeds)
- [ ] HSTS header is served (`curl -I` the apex returns
      `Strict-Transport-Security`)
- [ ] `FEATURE_PLATFORM` only turned on when billing is configured
      and webhook signing secret is in place
- [ ] GHCR image is either private with a PAT on the VPS, or public
      with no secrets baked in (it shouldn't have any — check
      `docker history`)
- [ ] A database backup has been taken AND restored to a scratch
      instance at least once
- [ ] You've actually read `SECURITY.md` for the disclosure policy
      you're promising users

---

## 12. Troubleshooting

**The workflow fails at "Roll web service on VPS" with `permission denied`.**
`deploy` can't run docker. Re-check `sudo usermod -aG docker deploy`
and `/etc/sudoers.d/deploy-docker`.

**Workflow succeeds but `https://assoluto.eu` 502s.**
Nginx can't reach `web`. Check `docker compose ps` — if `web` is
`unhealthy`, `docker compose logs web` will show why. The usual
suspect is a bad `DATABASE_URL` or the `portal_app` password mismatch
from §6.2.

**Healthz fails with `tenant not resolved`.**
It shouldn't — `/healthz` is registered outside tenant resolution.
If you see this, you're hitting a different route. Check Nginx is
forwarding `/healthz` (it is in the example config) and that DNS
resolves to your VPS, not some other server.

**Wildcard TLS cert fails to renew.**
`sudo certbot renew --dry-run` will tell you exactly why. Most often:
the Cloudflare API token expired or lost zone scope. Regenerate,
update `/etc/letsencrypt/secrets/cloudflare.ini`, retry.

**`alembic upgrade head` fails on deploy.**
The web container won't start, the workflow reports the log tail.
Fix the migration, push a revert to `production`, or SSH in and
manually `alembic downgrade -1` against the Postgres container before
re-deploying. Keeping migrations reversible saves this from being an
outage.

---

## 13. What to do next

- **Move Postgres off the box** once you have real paying customers.
  Managed Postgres (Hetzner Managed, Neon, RDS) is one env-var change:
  swap `DATABASE_URL`, `DATABASE_SYNC_URL`, `DATABASE_OWNER_URL` and
  remove the `postgres` service from the compose file.
- **Add a staging environment.** Second VPS or Hetzner project, same
  playbook, deployed from a `staging` branch on the same workflow
  pattern.
- **Add Sentry / GlitchTip** for error tracking — set `SENTRY_DSN` once
  you wire it into `app/main.py`.
- **Review [`docs/DEPLOY_SAAS.md`](DEPLOY_SAAS.md)** for the
  multi-tenant operational model (tenant provisioning, billing events,
  RLS verification) once the plumbing is live.

---

## A — Delegation brief for Claude on the VPS

After you've provisioned the VPS (§3.1) and ticked off §2a, SSH in
**as root** using your personal SSH key. Install Claude Code on the
box (one-liner from the docs), then paste the block below as the
first prompt. Claude will execute §3.2 through §10.1 end-to-end.

> The brief assumes you hand Claude the collected secrets inline. Do
> **not** commit the filled-in brief to git — it contains live
> credentials. Write it into a scratch file, paste it, then `shred`
> the file.

**Copy, fill the placeholders, paste to Claude:**

```
You are running as root on a freshly provisioned Hetzner Cloud VPS
(Ubuntu 24.04). Follow /opt/assoluto/docs/DEPLOY_HETZNER.md from §3.2
through §10.1 — but since the repo isn't cloned yet, work from the
values below. Do NOT ask me for confirmation on individual commands;
execute the plan end-to-end and report at each major milestone.

===== Values to use =====
PORTAL_DOMAIN=assoluto.eu
ACME_EMAIL=ops@assoluto.eu
REPO_URL=https://github.com/<your-org>/sme-client-portal.git

# Deploy keypair — public half goes into deploy@'s authorized_keys
DEPLOY_SSH_PUBLIC_KEY=ssh-ed25519 AAAA... gha-deploy@assoluto

# Cloudflare API token for Let's Encrypt DNS-01
CLOUDFLARE_API_TOKEN=<token>

# Object storage (Backblaze B2 or R2)
S3_ENDPOINT_URL=https://s3.eu-central-003.backblazeb2.com
S3_BUCKET=assoluto-attachments
S3_REGION=eu-central-003
S3_ACCESS_KEY=<keyID>
S3_SECRET_KEY=<applicationKey>

# SMTP
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USER=<token>
SMTP_PASSWORD=<token>
SMTP_FROM=no-reply@assoluto.eu

# Platform / SaaS (leave FEATURE_PLATFORM=false until Stripe is live)
FEATURE_PLATFORM=false

# GHCR pull — only if the repo image is private
GHCR_USER=<github username>
GHCR_PULL_TOKEN=<PAT with read:packages>

# Initial tenant to seed after first boot
FIRST_TENANT_SLUG=acme
FIRST_TENANT_ADMIN_EMAIL=admin@acme.com

===== Execute in order =====
1. §3.2 Baseline hardening: apt upgrade, install ufw/fail2ban/certbot
   tooling, create `deploy` user from my authorized_keys, add
   DEPLOY_SSH_PUBLIC_KEY to /home/deploy/.ssh/authorized_keys, grant
   the NOPASSWD sudoers rule, disable SSH password + root login, UFW
   allow 22/80/443.
2. §3.3 Install Docker via get.docker.com, add deploy to docker group.
3. §4.1 Install python3-certbot-dns-cloudflare, drop the token file,
   request the wildcard cert for PORTAL_DOMAIN and *.PORTAL_DOMAIN.
4. §6 App layout: clone REPO_URL into /opt/assoluto as deploy,
   checkout the `production` branch if it exists (otherwise `main`),
   render docker/nginx.conf from the .example template with
   PORTAL_DOMAIN substituted, create docker/common-proxy.inc, patch
   docker-compose.prod.yml so the nginx service mounts
   /etc/letsencrypt and common-proxy.inc (see §6.1 block), update the
   ssl_certificate* lines in nginx.conf to point at the certbot live
   directory.
5. §6.2 Generate three strong secrets with
   `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`
   (one 48-byte, two 32-byte), write /etc/assoluto/env with every
   value from the block above plus APP_IMAGE_TAG=latest,
   APP_BASE_URL=https://PORTAL_DOMAIN, LOG_LEVEL=INFO,
   PLATFORM_COOKIE_DOMAIN=.PORTAL_DOMAIN, MAX_UPLOAD_SIZE_MB=50,
   chmod 600 root:root.
6. §7.1 If GHCR_PULL_TOKEN is set, docker login ghcr.io.
7. §7.3 `docker compose --env-file /etc/assoluto/env -f
   docker-compose.yml -f docker-compose.prod.yml pull && up -d`.
   Wait for `web` to report healthy.
8. §6.2 admonition: immediately ALTER ROLE portal_app with the
   PORTAL_APP_PASSWORD from the env file so the app's DSN works.
9. §7.4 curl /healthz inside the web container — retry up to 60s.
   If it fails, show me the last 200 lines of web logs and stop.
10. §7.5 Run scripts.create_tenant with FIRST_TENANT_SLUG and
    FIRST_TENANT_ADMIN_EMAIL. Capture the printed credentials into
    your report (do not put them in any file).
11. §10.1 Install /opt/assoluto/scripts/backup.sh + the cron entry
    at 03:00 daily.
12. Final report: paste back
    - `docker compose ps`
    - `curl -fsS http://127.0.0.1:8000/healthz` (from inside web)
    - the first-tenant credentials
    - a list of any step you had to adapt and why
    - any outstanding "you should do this" follow-ups for me

===== Guardrails =====
- Do not run `rm -rf` on anything outside /tmp/.
- Do not push anything to git. Keep all edits local to the VPS.
- Do not expose Postgres on the host — check that port 5432 is only
  on the Docker network.
- If /etc/assoluto/env already exists with a non-zero APP_SECRET_KEY,
  stop and ask me before overwriting.
- If certbot fails, do not proceed past step 3 — report the error.
- After you're done, remove the Cloudflare token file's contents
  only if the cert is on auto-renew and you've verified
  `certbot renew --dry-run` succeeds.
```

### A.1. Safety notes on running Claude on a production server

- **Run as `deploy`, not `root`, after §3.2.** The brief starts as
  root only because Ubuntu defaults have no other sudo user yet;
  Claude should switch to the `deploy` user it creates and escalate
  via `sudo` from then on.
- **Scope the secrets paste.** Only paste the preflight values into
  the Claude prompt. If you use a cloud-hosted Claude, the prompt is
  transmitted to Anthropic — treat the secrets the same way you'd
  treat them being in any SaaS log: rotate them after you're done if
  that's your threat model. For the strictest setups, run Claude
  Code offline against a local model, or narrow the API token scopes
  to what §A actually needs and rotate them once deploy succeeds.
- **Hooks for guardrails.** If you run Claude repeatedly on this
  host, configure a project `CLAUDE.md` under `/opt/assoluto/` that
  codifies the guardrails block above, plus hooks in
  `/opt/assoluto/.claude/settings.json` that veto anything writing
  outside `/opt/assoluto`, `/etc/assoluto`, `/etc/letsencrypt`,
  `/etc/cron.d`, `/etc/sudoers.d`, and `/home/deploy`.

### A.2. When you should stop delegating and take over

Some operations warrant a human hand on the wheel:

- **Any rollback during a production incident.** Don't ask an agent
  to "fix it" — paste the rollback commands from §10.4 yourself so
  you're reading the output in real time.
- **Migrations that drop columns or rewrite rows.** Follow the
  add-backfill-switch-drop pattern (§10.4) and take each step on a
  separate deploy. Agents are happy to collapse the steps; you know
  why they shouldn't be collapsed.
- **First cut-over of real customer data.** Do the restore from
  backup yourself, verify row counts, flip DNS only after smoke
  tests pass.

Everything else — routine deploys, dependency bumps, certificate
rotation, log rotation, adding a new tenant — is safe to delegate.

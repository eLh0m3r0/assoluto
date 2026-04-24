# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Assoluto, please
**do not open a public GitHub issue**. Instead, report it privately by
email so we can investigate and release a fix before details become public.

**Contact:** team@assoluto.eu

Please include:

- A description of the vulnerability
- Steps to reproduce (or a proof-of-concept)
- Affected version(s) / commit hash
- Your assessment of impact (confidentiality / integrity / availability)
- Any suggested mitigation

We aim to:

- Acknowledge receipt within **2 business days**
- Provide an initial assessment within **7 days**
- Release a fix (or a mitigation plan) within **30 days** for high-severity
  issues; lower-severity items may take longer

You will be credited in the release notes of the fix, unless you prefer
to remain anonymous.

## Supported Versions

Only the `main` branch and the latest released `v*.*.*` tag receive
security updates. Older releases are not patched — upgrade to the latest
version to receive fixes.

| Version | Supported |
|---|---|
| `main` (development) | Yes |
| Latest `v*.*.*` release | Yes |
| Older releases | No |

## Scope

In scope:

- `app/` — the FastAPI application
- `migrations/` — schema migrations
- `scripts/` — CLI tools
- Published Docker images (`ghcr.io/elh0m3r0/sme-client-portal`)

Out of scope:

- Third-party dependencies (please report upstream)
- Hosted infrastructure (report to the hosting provider)
- Self-hosted deployments misconfigured by operators (RLS disabled,
  weak `APP_SECRET_KEY`, etc.)

## Hardening Checklist for Self-Hosted Deployments

Before exposing the portal to the public internet:

- [ ] Generate a strong `APP_SECRET_KEY` (32+ random bytes)
- [ ] Use separate Postgres roles: `portal` (owner) + `portal_app`
  (non-owner, subject to RLS)
- [ ] Apply all migrations and verify RLS is enabled on tenant tables
- [ ] Enable TLS (HTTPS) on all endpoints — never expose port 8000 directly
- [ ] Configure `APP_BASE_URL` to your real HTTPS URL
- [ ] Set `APP_DEBUG=false` and `APP_ENV=production`
- [ ] Rotate SMTP credentials and use STARTTLS where supported
- [ ] Restrict Postgres network access (same VPC / Docker network only)
- [ ] Back up the database daily (see `scripts/backup.sh`)
- [ ] Subscribe to this repository's Releases for security updates

---

## Česká verze (Czech version)

### Bezpečnostní politika

### Oznámení zranitelnosti

Pokud objevíte bezpečnostní zranitelnost v Assolutu,
**neotevírejte prosím veřejný GitHub issue**. Nahlaste ji soukromě
e-mailem, ať ji můžeme prozkoumat a vydat opravu dřív, než se detaily
dostanou na veřejnost.

**Kontakt:** team@assoluto.eu

Prosím uveďte:

- Popis zranitelnosti
- Kroky k reprodukci (nebo proof-of-concept)
- Zasažené verze / commit hash
- Váš odhad dopadu (důvěrnost / integrita / dostupnost)
- Případný návrh mitigace

Snažíme se:

- Potvrdit přijetí do **2 pracovních dnů**
- Poskytnout první posouzení do **7 dnů**
- Vydat opravu (nebo mitigační plán) do **30 dnů** u zranitelností
  s vysokou závažností; u nižší závažnosti to může trvat déle

Budete uveden v release notes opravy, pokud si nepřejete zůstat
v anonymitě.

### Podporované verze

Bezpečnostní aktualizace dostává pouze větev `main` a poslední
vydaný tag `v*.*.*`. Starší releasy se nepatchují — upgradujte na
nejnovější verzi.

### Rozsah

V rozsahu: `app/`, `migrations/`, `scripts/`, publikované Docker
image `ghcr.io/elh0m3r0/sme-client-portal`.

Mimo rozsah: závislosti třetích stran (nahlaste upstream), hostovaná
infrastruktura (nahlaste poskytovateli hostingu), self-hosted
nasazení špatně nakonfigurovaná operátorem (vypnutá RLS, slabý
`APP_SECRET_KEY` apod.).

### Hardeningový checklist pro self-hosted nasazení

Než portál vystavíte na veřejný internet:

- [ ] Vygenerujte silný `APP_SECRET_KEY` (32+ náhodných bajtů)
- [ ] Použijte oddělené Postgres role: `portal` (owner) +
  `portal_app` (non-owner, podléhá RLS)
- [ ] Aplikujte všechny migrace a ověřte, že RLS je zapnutá na
  tenant tabulkách
- [ ] Zapněte TLS (HTTPS) na všech endpointech — nikdy nevystavujte
  port 8000 přímo
- [ ] Nastavte `APP_BASE_URL` na skutečnou HTTPS URL
- [ ] `APP_DEBUG=false` a `APP_ENV=production`
- [ ] Rotujte SMTP přihlašovací údaje a používejte STARTTLS, kde je
  to možné
- [ ] Omezte síťový přístup k Postgresu (pouze stejná VPC / Docker
  network)
- [ ] Zálohujte databázi denně (viz `scripts/backup.sh`)
- [ ] Sledujte Releases tohoto repozitáře kvůli bezpečnostním
  aktualizacím

# Release engineering — větve, CI, deploy (orchestrátor)

## Stav větví

**Před:** 9 větví (5 lokálních navíc k `main`/`production`, 4 vzdálené
feature větve), 1 worktree, 1 stash, lokální `main` 6 commitů a lokální
`production` 105 commitů za originem.

**Zjištění:** veškerý kód ze všech větví už byl v `origin/main`.
`worktree-order-status-stepper` měla jediný „nesloučený" commit, ale ten
byl squash-mergnutý jako PR #5 (`d65161b`). Stash z 2026-04-21 obsahoval
CSV export a bulk transition — obojí v `main` je. `origin/main` ==
`origin/production` == `015496f`.

**Po (provedeno):**

| akce | detail |
|---|---|
| fast-forward lokálních `main` a `production` | na `015496f` |
| smazány lokální větve | `feat/notifications-redesign`, `docs/git-authority`, `feat/dark-mode`, `worktree-agent-a27200bb`, `worktree-order-status-stepper` |
| smazány vzdálené větve | `feat/notifications-redesign`, `worktree-e2e-audit-2026-07-26`, `worktree-order-status-stepper`, `claude/customer-portal-sme-UtWmL` |
| odstraněn worktree | `.claude/worktrees/agent-a27200bb` (jen ignorovaná binárka Tailwindu) |
| stash | zakotven do **lokálního** tagu `archive/stash-2026-04-21` (nepushnuto), poté dropnut |

Zůstávají jen `main` a `production` + `chore/release-pipeline` (PR #8).

## Nálezy

### REL-1 — Deploy na produkci neběžel až po CI
- Severity: P1
- Status: new — **opraveno v PR #8**
- Evidence: `.github/workflows/deploy-production.yml` (před PR) nemá žádnou závislost na `ci.yml`; `ci.yml` se na `production` vůbec nespouští. PR #7: CI na `main` start 16:37:48, deploy start 16:37:55, deploy hotový za 2m31s, CI za 5m29s.
- Dopad: červené testy by se ohlásily až ve chvíli, kdy produkce už běží na rozbitém kódu.
- Návrh: job `ci-gate` čeká na zelený CI běh na `main` pro přesně ten SHA; jinak deploy selže. Nouzový bypass přes `workflow_dispatch`.
- Effort: S · Auto-fixable: yes

### REL-2 — Žádná záloha před migrací, žádný rollback
- Severity: P1
- Status: new — **opraveno v PR #8**
- Evidence: entrypoint (`docker/entrypoint.sh`) spouští `alembic upgrade head` při každém startu; deploy skript při selhání `/healthz` jen vypíše logy a skončí s chybou — rozbitý kontejner zůstane běžet. Noční záloha (`scripts/backup.sh`) může být až 24 h stará.
- Dopad: nevratná migrace bez čerstvé zálohy; výpadek do ručního zásahu.
- Návrh: `pg_dump` před rolloutem (selhání = stop), automatický návrat na předchozí `APP_IMAGE_TAG`, veřejná sonda přes Caddy.
- Effort: S · Auto-fixable: yes

### REL-3 — `main` ani `production` nemají ochranu větví; repo je veřejné
- Severity: P2
- Status: new
- Evidence: `gh api repos/eLh0m3r0/assoluto/branches/{main,production}/protection` → 404 „Branch not protected"; `visibility: PUBLIC`.
- Dopad: překlep (`git push --force`, push na špatnou větev) jde rovnou na produkci; CI gate v PR #8 to částečně kryje (commit bez CI na `main` se nenasadí), ale ne force-push ani smazání větve.
- Návrh (nastavení repa, operátor): `production` — zakázat force-push a smazání; `main` — vyžadovat PR + status check „Lint + test", povolit admin bypass (sólo founder).
- Effort: S · Auto-fixable: no (nastavení GitHubu, potřebuje souhlas)

### REL-4 — Po merge se větve nemažou
- Severity: P3
- Status: new
- Evidence: `deleteBranchOnMerge: false`; 4 vzdálené mrtvé větve uklizeny až teď.
- Návrh: zapnout „Automatically delete head branches".
- Effort: S · Auto-fixable: no (nastavení repa)

### REL-5 — Žádná automatizace aktualizací závislostí
- Severity: P2
- Status: new — **opraveno v PR #8** (`.github/dependabot.yml`)
- Evidence: `.github/` neobsahuje `dependabot.yml` ani Renovate. Akce jsou od F-19 pinované na SHA, takže bez automatu nikdy nedostanou bezpečnostní opravu; CVE v `python-multipart` (F-03) se našla jen díky ručnímu auditu.
- Effort: S · Auto-fixable: yes

### REL-6 — SSH host key VPS se neověřuje
- Severity: P2
- Status: new — **připraveno v PR #8**, operátor musí přidat secret
- Evidence: `appleboy/ssh-action` bez `fingerprint` = `StrictHostKeyChecking no` (`docs/DEPLOY_HETZNER.md` §8.4 to dokonce uvádí). Akce drží privátní deploy klíč.
- Návrh: secret `DEPLOY_HOST_FINGERPRINT`.
- Effort: S · Auto-fixable: no (operátor)

### REL-7 — `APP_IMAGE_TAG` má fallback `latest`, který ukazuje na dubnový release
- Severity: P2
- Status: new
- Evidence: `docker-compose.prod.yml:34` `image: ghcr.io/elh0m3r0/assoluto:${APP_IMAGE_TAG:-latest}`. Deploy pushuje jen `:<sha>` a `:production`; `:latest` vzniká pouze z `release.yml` na tag `v*` (metadata-action přidává `latest` k semver tagům) — jediný tag je `v0.1.0`. (Existenci `:latest` v GHCR jsem neověřil — token nemá `read:packages`.)
- Dopad: pokud z `/etc/assoluto/env` zmizí `APP_IMAGE_TAG`, produkce tiše nastartuje půl roku starý kód proti novému schématu.
- Návrh: `${APP_IMAGE_TAG:?set by deploy}` nebo fallback `:production`.
- Effort: S · Auto-fixable: yes

### REL-8 — Mrtvé/alternativní deploy cesty a bordel v kořeni
- Severity: P3
- Status: new
- Evidence: `railway.toml` + `docs/DEPLOY_RAILWAY.md` (od dubnového rebrandu nedotčeno); `release.yml` (multi-arch image na tag, použito jednou); `ultraplan.md` v kořeni repa (dubnová roadmapa, stav položek neznámý).
- Návrh: Railway smazat, nebo výslovně označit „community, nepodporováno"; `release.yml` ponechat jen pokud self-host je součást strategie (viz business/market); `ultraplan.md` přesunout do `docs/` nebo zrušit ve prospěch `ACTION_PLAN.md`.
- Effort: S · Auto-fixable: no (rozhodnutí)

### REL-9 — Chybí staging
- Severity: P3
- Status: new
- Evidence: jediné prostředí je produkce; migrace se poprvé potkají s reálnými daty při deployi.
- Návrh: levná varianta — v CI obnovit anonymizovaný dump a spustit `alembic upgrade head` proti němu; plnohodnotný staging až s placenými zákazníky.
- Effort: M · Auto-fixable: no

### REL-10 — CI drobnosti
- Severity: P3
- Status: new
- Evidence: `ci.yml` pinuje `uv` 0.5.4 (výrazně starší než lock), trigger `claude/**` je pozůstatek; pytest bez `-x`/paralelizace (5 min).
- Návrh: aktualizovat `uv`, zvážit `pytest-xdist`.
- Effort: S · Auto-fixable: yes

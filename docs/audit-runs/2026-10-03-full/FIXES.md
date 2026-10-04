# Co bylo opraveno — audit 2026-10-03

Realizace podle [EXECUTION_BRIEF.md](EXECUTION_BRIEF.md): šest paralelních
týmů (WEB, BILLING, ORDERS, BACKEND, GROWTH, POHODA) + provoz + integrace.
Nasazeno 2026-10-04 (`0fd800b`, PR #8, #16, #17).

## Ověření
- `pytest`: **872 passed** (z 573), ruff + mypy čisté, mypy nově v CI.
- Migrace `1008 → 1010 → 1011 → 1009 → 1013 → 1012`: celý řetěz od nuly,
  round-trip, a **up + down na obnovené kopii produkčních dat** bez ztráty řádků.
- Prohlížečový E2E proti lokálnímu serveru (moto S3, zachytávací SMTP):
  13/13 kroků, 0 chyb konzole, 0 × 5xx.
- Produkce po nasazení (jen čtení + přihlášení do fiktivního tenanta
  `ukazka`): 7/7.

## Témata (viz findings.md)

| # | Téma | Stav | Poznámka |
|---|---|---|---|
| T1 | Zálohy | **fixed** | Šifrovaný dump + všechny přílohy denně do `assoluto-backups` (fsn1, verzování); `/healthz/backups` v uptime; zkouška obnovy provedena. Dump 0600. |
| T2 | Disk | **fixed** | 88 % → 20 %; prune po každém deployi; rotace logů kontejnerů. |
| T3 | Billing demo režim | **fixed** | Bez Stripe v produkci žádné plány zdarma → ruční fakturace; odpočet trialu; datum odříznutí; CTA po ověření vede do aplikace. |
| T4 | Stripe automat | **fixed (mock)** | Modify místo druhé subskripce, generace subskripcí, nároky pro každý status, idempotentní zrušení, guard webhook secretu, operátorské pozastavení. **Před zapnutím Stripe ověřit 12 scénářů v test mode** (seznam v reportu týmu BILLING, PR #17). |
| T5 | Pravdivý web | **fixed** | Reference, „Nejoblíbenější“, šifrování, SLA, revize, telefon, vlastní doména, mock-up; nový positioning; /security, security.txt. DPA a foto zakladatele čekají na foundera. |
| T6 | Billing dodavatele u jeho zákazníků | **fixed** | Kontakty nevidí bannery ani 402; neutrální stránka po odříznutí. |
| T7 | Integrita nabídky | **fixed** | Snapshot potvrzené částky + optimistická kontrola; žádné prázdné nabídky; audit mazání výkresů. DPH poznámka na PDF čeká na nastavení tenanta. |
| T8 | Validace peněz | **fixed** | NaN/Infinity/záporné → hláška, ne 500 (ověřeno útokem v E2E). |
| T9 | Upload / export | **fixed** | S3 mimo event loop, streamovaný upload i export. |
| T10 | Slepý provoz | **fixed / partial** | E-mail upozornění na 500 (`OPS_ALERT_EMAIL`), S3 v readyz, outbox s retry, audit jobů. GitHub uptime cron je dál „best effort“ (~4 h); externí monitor potřebuje účet. |
| T11 | GDPR a retence | **fixed** | Export/výmaz platformní identity, úplné exporty, retenční job (**dry-run**, `RETENTION_ENFORCE`). |
| T12 | Termíny a SLA | **fixed** | Editace hlavičky, slíbený termín, po termínu, SLA bez zrušených. |
| T13 | Limity plánů | **fixed** | Reaktivace, souběh (advisory lock), support uživatel. |
| T14 | DACH lokalizace | **deferred** | Rozhodnutí CEO: až po 10 platících CZ zákaznících. Chyby platformy už jdou přes `_t`; cs/de katalogy 100 %. |

Nalezeno a opraveno až během integrace / E2E:
- náhledy příloh blokovalo CSP (302 na presigned S3) → servírováno ze stejného originu;
- `pattern` subdomény neplatný v režimu `v` → validace v prohlížeči nefungovala;
- dva nové joby se stejným advisory lock ID → test hlídá unikátnost;
- PDF náhledy: chyběl `pdf2image` i poppler (+ timeout 30 s).

## Nové funkce (prodejnost / náskok)
Pracovní fronta „Vyžaduje akci“, Objednat znovu, cenová paměť, připomínka
nepotvrzené nabídky, e-mail s nabídkou a potvrzením, export do POHODA a
Money S3, aktivační funnel, „Powered by“ smyčka, archivace zákazníka,
správce za zákazníka zve kolegy, týdenní souhrn (opt-in), ukázkový tenant
`ukazka.assoluto.eu` pro online ukázky a obchodní hovory (na MSV 2026 se nejede — rozhodnutí foundera 2026-10-04).

## Odloženo vědomě
- **Dependabot major verze** (Actions v deploy pipeline, reportlab 5,
  python-slugify 9, structlog 26): kritické cesty, samostatná změna.
- **AI příjem objednávek (MKT-4)**, revize výkresů (IDEA-8), formální nabídka
  s e-podpisem (MKT-6), import BOM: Vlna 3, vyžaduje rozhodnutí (API klíč, náklady).
- **Trial import do skutečné POHODY / Money S3** — export validuje proti XSD,
  web zatím netvrdí „funguje s POHODA“ bez zkušebního importu.
- **Časová pásma v UI** (LOGIC-16) a EUR (LOGIC-5) — s DACH.

## Operátor

Hotovo 2026-10-04:
- journal vyčištěn (2,9 GB → 15 MB);
- `DEPLOY_HOST_FINGERPRINT` = ECDSA host key VPS (`SHA256:S9t8SIJ6…`), ověřeno deployem;
- životní cyklus záložního bucketu: dumpy `pg/` 180 dní, staré verze 30 dní;
- dry-run retence prošel (jen `test-a`, `test-b`, `testfirma`, 0 osiřelých objektů) → `RETENTION_ENFORCE=true`;
- texty aktivačních e-mailů zkontrolovány v češtině → `ACTIVATION_NUDGES_ENABLED=true`.

Zbývá (vyžaduje founderův přístup nebo účet):
- privátní GPG klíč záloh uložit do správce hesel (`~/assoluto-backup-PRIVATE-KEY.asc`), soubor smazat;
- `SystemMaxUse=500M` v `/etc/systemd/journald.conf` (root), aby journal znovu nenarostl;
- Stripe: nové ceny 1 490 / 2 990 Kč, 12 scénářů v test mode, pak klíče;
- zkušební import exportu do POHODA / Money S3.

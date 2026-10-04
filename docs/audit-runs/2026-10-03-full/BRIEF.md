# Zadání — „Mother of all reviews" (2026-10-03)

## Proč

Udělat z Assoluta produkt, který je **lepší, chytřejší, efektivnější,
bezpečnější, použitelnější, konkurenceschopnější a prodatelnější**.
Výstupem není seznam chyb, ale **akční plán**: co opravit, co postavit,
co přestat slibovat — seřazené podle dopadu na tržby a riziko.

## Baseline

| | |
|---|---|
| Auditovaný commit | `015496f` (`main` = `production` = co běží na assoluto.eu) |
| Testy | 573 passed (lokálně, PG 16) |
| Předchozí běh | [`2026-07-26-e2e`](../2026-07-26-e2e/) — 43 nálezů, většina opravena; otevřené viz `FIXES.md` § Still open |
| Co přibylo od té doby | Notifikační redesign (PR #7): routing podle události, souhlasu a relevance |

**Pravidlo navázání:** každý otevřený nález z 2026-07-26 se znovu ověří
proti kódu a dostane status `persisted` / `resolved` / `regressed`.
Nové nálezy se značí `new`. Neopakovat to, co už je opravené a otestované.

## Perspektivy (kdo co dělá)

| # | Perspektiva | Kdo | Výstup |
|---|---|---|---|
| 1 | Bezpečnost — authn/authz, RLS, CSRF, tokeny, upload, Stripe webhook, tajemství v historii veřejného repa | `security-auditor` | `security.md` |
| 2 | Backend & provoz — správnost, výkon, migrace, background tasky, observabilita, zálohy, testy | `backend-auditor` | `backend.md` |
| 3 | Byznys logika — životní cyklus zakázky, ceny/nabídky, notifikace, limity plánů, stavy předplatného; hledání invariantů, které kód porušuje | general-purpose | `logic.md` |
| 4 | UI/UX — živý web (jen čtení, žádné mutující POSTy na produkci), CS/EN/DE, mobil, dark mode, onboarding, prázdné stavy, přístupnost | `ux-auditor` | `ux.md` |
| 5 | Obchod — sliby v marketingu vs. realita v kódu, pricing, trial, konverzní funnel, právní texty | `business-auditor` | `business.md` |
| 6 | Trh & konkurence — kdo jsou alternativy (CZ/DE i globální), cenové benchmarky, chybějící funkce, „chytré" funkce, kanály akvizice, positioning | general-purpose + web | `market.md` |
| 7 | Nezávislý druhý pohled — adversariální review billingu/Stripe, izolace tenantů a notifikačního routingu | Codex (GPT-6 Astra, šetrně — sdílený limit) | `codex.md` |
| 8 | Release engineering — větve, CI → production → Hetzner, rollback, ochrana větví | orchestrátor (Claude) | `release.md` |

## Formát nálezu (povinný pro všechny)

```
### <PREFIX>-<n> — <titulek, jedna věta>
- Severity: P0 | P1 | P2 | P3
- Status vs 2026-07-26: new | persisted (F-xx) | regressed (F-xx) | resolved (F-xx)
- Evidence: `soubor:řádek` nebo URL + co přesně se tam děje
- Dopad: kdo to pocítí a jak (zákazník, dodavatel, operátor, tržby, právo)
- Návrh: konkrétní oprava / funkce
- Effort: S (< 2 h) | M (≤ 1 den) | L (> 1 den)
- Auto-fixable: yes | no (potřebuje rozhodnutí / operátora / Stripe účet)
```

Severity: **P0** = únik dat, ztráta dat, peníze špatně, nebo produkce
nefunkční · **P1** = blokuje prodej / spuštění plateb, nebo vážná díra
s podmínkou · **P2** = znatelně zhoršuje produkt · **P3** = kosmetika.

Pravidla: žádný nález bez evidence; „nevím jistě" se píše jako
`unverified`, ne jako fakt. Žádné úpravy kódu během auditu — jen zápis
do `docs/audit-runs/2026-10-03-full/`.

## Výstup orchestrátora

1. `findings.md` — sloučené, deduplikované, seřazené nálezy s indexem.
2. `ACTION_PLAN.md` — vlny:
   - **Vlna 0 (hned):** únik/ztráta dat, rozbitá produkce, release pipeline.
   - **Vlna 1 (před spuštěním plateb):** billing integrita, GDPR, observabilita.
   - **Vlna 2 (prodejnost):** aktivace, onboarding, důvěra, marketing ↔ realita.
   - **Vlna 3 (náskok):** chytré funkce a diferenciace vůči konkurenci.
   Každá položka: proč, co, effort, kdo (kód / operátor / rozhodnutí foundera).
3. Úklid větví + PR s úpravou release pipeline (bez zápisu do `main`/`production`
   bez výslovného příkazu — CLAUDE.md §20).

# Akční plán — Assoluto, říjen–prosinec 2026

Vychází z [findings.md](findings.md) (154 nálezů z 8 perspektiv, 14 témat).
Odkazy `T#` = téma ve findings, ostatní ID = konkrétní nález.

## TL;DR

1. **Data nejsou zálohovaná mimo server a disk se za ~9 deployů zaplní.** To je jediné skutečné P0 riziko pro existenci produktu. Oprava je otázka jednoho večera.
2. **Web tvrdí věci, které nejsou pravda**: vymyšlené reference, „šifrováno at rest", 99,9 % SLA, termíny, náhledy PDF. Pro B2B nákupčího je to nejrychlejší způsob, jak ztratit důvěru, a u referencí jde i o právní riziko (klamavá reklama). Úprava textů zabere hodinu.
3. **Billing je v demo režimu.** Tlačítko „Upgrade" dá plán zdarma a za pár dní tenanta odřízne. Stripe stavový automat má navíc ~10 chyb, které se projeví až s ostrými penězi. Platících zákazníků je zatím **nula**, takže doporučuju: hned zablokovat demo cestu a ukázat „fakturujeme ručně, napište nám". Stripe spustit až s opraveným automatem (Vlna 1).
4. **Produkt je technicky solidní**: 573 testů, RLS, CSRF, žádné tajemství v historii veřejného repa a předchozí audit opravený. Neprodává se kvůli **positioningu, ceně a chybějícím blokerům prodeje**, ne kvůli bezpečnosti. Hlavně jde o export do Pohody/Money, demo bez registrace a reálné reference.
5. **MSV Brno je 6.–9. 10.** Nejlevnější zdroj 30 rozhovorů s cílovkou za celý rok. Přijít s připraveným demo tenantem.

## Rozhodnutí, která potřebuju od tebe

| # | Rozhodnutí | Moje doporučení |
|---|---|---|
| D1 | Reference na homepage (BIZ-04) | **Smazat hned.** Místo nich „Program zakládajících zákazníků" (MKT-10) |
| D2 | Kdy spustit Stripe | **Až po Vlně 1.** Do té doby ruční fakturace + guard (BIZ-01) |
| D3 | Nový ceník (MKT-1, MKT-2, BIZ-14) | Start 990 / Pro 2 490 / Business 4 990 Kč bez DPH, **neomezení odběratelé**, platí se za staff a funkce, roční platba = 2 měsíce zdarma. Prvním zákazníkům držet dnešní ceny výměnou za referenci |
| D4 | Záporné řádky (slevy) v zakázce (LOGIC-1) | Zakázat, pro slevy později vlastní typ řádku |
| D5 | Self-host/AGPL tier „Community" na ceníku jako první (BIZ-14) | Posunout na konec, ponechat jako důvěryhodnostní signál, ne jako nabídku |
| D6 | Retence: audit log, uzavřené zakázky, smazaní tenanti (BIZ-08, BE-19) | Smazaný tenant 30 dní (jak slibují Terms), audit 3 roky, uzavřené zakázky po dobu života tenanta |
| D7 | Ochrana větví + automatické mazání větví (REL-3, REL-4) | Zapnout. `production`: zákaz force-push a smazání. `main`: PR + CI check, admin bypass |
| D8 | Jednorázový `docker image prune` na VPS teď (BE-02) | Ano. Všechny image jsou v GHCR, nic se neztratí |

---

## Vlna 0 — tento týden

Cíl: žádná ztráta dat, žádná nepravda na webu, žádný pád z jednoho POSTu.

| # | Co | Proč | Kdo | Effort | Nálezy |
|---|---|---|---|---|---|
| 0.1 | **Merge PR #8** (CI gate, dump před migrací, auto-rollback, prune, Dependabot) + jednorázový prune na VPS | Deploy dnes běží souběžně s testy, bez zálohy a bez rollbacku; disk 88 % | operátor (příkaz) | 10 min | REL-1, REL-2, REL-5, BE-02, BE-10 |
| 0.2 | **Off-site zálohy**: Hetzner Storage Box (BX11, řádově jednotky €/měs.) nebo B2, `rclone` + `BACKUP_GPG_RECIPIENT`, cron s env, **versioning bucketu příloh** + noční `rclone copy` bucketu, **zkouška obnovy** | Ztráta VPS = ztráta všech dat všech zákazníků | operátor + kód (cron/skript) | 2–3 h | BE-01, SEC-4, BIZ-05, BIZ-07, T1 |
| 0.3 | **Pravdivý web**: smazat reference a „Nejoblíbenější"; „encrypted at rest" → přesné znění; pryč „redundantní infrastruktura" a 99,9 %; termíny/revize jen to, co kód umí; sjednotit časy podpory; telefon doplnit nebo vyhodit; vlastní doménu „na vyžádání"; opravit mobilní hero a mock-up | Klamavá tvrzení + ztráta důvěry | kód | 2 h | T5: BIZ-04, BIZ-05, BIZ-06, BIZ-10, BIZ-18, UX-02, UX-03, UX-16, UX-19, UX-20, UX-26 |
| 0.4 | **PDF náhledy**: přidat `poppler-utils` do image | Web je inzeruje, v produkci 0 ze 44 | kód | 30 min | BE-04 |
| 0.5 | **Billing guard**: v produkci bez Stripe nedávat plány zdarma → „Online platba se připravuje, fakturujeme ručně"; trial countdown pro admina; datum odříznutí v banneru; po verifikaci e-mailu nevést na platební údaje | Dnes „Upgrade" = plán zdarma → tenant odříznut | kód | 3 h | BIZ-01, BIZ-02, LOGIC-4, UX-01 |
| 0.6 | **Validace peněz**: `_parse_money()` (konečné, ≥ 0, v rozsahu), `money_major` odolné vůči NaN, kontrola dat v produkci | Jeden POST zákazníka shodí dodavateli seznam zakázek | kód | 1 h | LOGIC-1 |
| 0.7 | **Monitoring**: Better Stack / UptimeRobot (1–5 min) + veřejná status stránka (`STATUS_PAGE_URL`); Sentry nebo GlitchTip (free tier) | 500 ze září nikdo neviděl; „15min" check běží jednou za ~4 h | operátor 30 min + kód 1 h | BE-05, BE-06, BIZ-06 |
| 0.8 | **MSV Brno 6.–9. 10.** — demo tenant s realistickými daty (výkresy, revize, materiál zákazníka), 30 rozhovorů, cíl 10 kontaktů na pilot | Validace positioningu a ceny | founder | 1 den příprava | market.md §5 |

## Vlna 1 — před spuštěním plateb (týdny 2–4)

Cíl: když zákazník zaplatí, stane se přesně to, co čeká, a nic jiného.

| # | Co | Kdo | Effort | Nálezy |
|---|---|---|---|---|
| 1.1 | **Stripe stavový automat**: změna plánu přes `Subscription.modify` / Billing Portal místo nového checkoutu; „generace" subskripcí (události starých ID ignorovat); explicitní pravidla oprávnění pro každý status (`unpaid`, `incomplete_expired`, `past_due`); idempotentní zrušení; operátorské pozastavení oddělit od billingového; refundy mimo pořadí; guard na prázdný `STRIPE_WEBHOOK_SECRET` + boot guard; billing jen pro aktivního admina. **Testy proti Stripe test mode** pro každý scénář | kód + Stripe test účet | 3–4 dny | T4: SEC-1, LOGIC-6, BIZ-03, Codex-1…8, Codex-12, LOGIC-18, LOGIC-23, SEC-11, BE-12 |
| 1.2 | **Stav předplatného dodavatele skrýt před jeho zákazníky**: kontakt nikdy nedostane 402/banner; upload nad limitem přijmout a upozornit admina; po odříznutí neutrální stránka | kód | 1 den | T6: LOGIC-3, BIZ-13 |
| 1.3 | **Integrita nabídky**: `confirmed_total` + `confirmed_at/by` snapshot, optimistická kontrola (formulář nese částku, změněná nabídka → „nabídka se změnila"), zákaz prázdných/nenaceněných nabídek, mazání výkresu po potvrzení jen s auditem, DPH řádek a zaokrouhlení na PDF | kód (1 migrace) | 1,5 dne | T7: LOGIC-2, LOGIC-7, LOGIC-13, LOGIC-17, UX-06 |
| 1.4 | **Limity plánů**: kontrola i při reaktivaci, zámek na tenantu přes count+insert, support uživatel mimo limit | kód | 0,5 dne | T13: LOGIC-8, Codex-9, Codex-10, LOGIC-20, LOGIC-9 |
| 1.5 | **GDPR**: export/výmaz pro platformní `Identity`, kompletní exporty, retenční job (nejdřív dry-run report, pak ostře), cookies stránka podle reality | kód + D6 | 2 dny | T11: SEC-2, SEC-5, BIZ-08, BE-19, SEC-12 |
| 1.6 | **Upload**: boto3 v threadpoolu (nebo presigned upload), streamovaná kontrola velikosti, export ZIP streamem | kód | 1 den | T9: SEC-3, BE-03, BE-17, BE-16 |
| 1.7 | **E-mail spolehlivost**: explicitní commit před plánováním mailu, jednoduchý outbox v DB s retry, audit událostí z plánovaných jobů | kód (1 migrace) | 1,5 dne | BE-08, BE-09, BE-13 |
| 1.8 | Operátor: Stripe live produkty, `STRIPE_*` env, **jedna reálná karta end-to-end**, pak teprve vypnout guard z 0.5 | operátor | 1 h | BIZ-01 |

## Vlna 2 — prodejnost (dny 15–45)

Cíl: majitel dílny pochopí hodnotu za 5 vteřin, vyzkouší si to bez registrace a jeho účetní neprotestuje.

| # | Co | Kdo | Effort | Nálezy |
|---|---|---|---|---|
| 2.1 | **Positioning „Zákaznický portál pro zakázkovou výrobu. Vedle Pohody, ne místo ní."**: hero, meta, features; materiál zákazníka jako headline; technické detaily přesunout na /security | kód (copy) | 0,5 dne | MKT-7, market.md §2 |
| 2.2 | **Nový ceník** podle D3, neomezení odběratelé, roční platba self-serve, varování před limitem | kód + D3 | 1 den | MKT-1, MKT-2, MKT-11, BIZ-14, MKT-8 |
| 2.3 | **Export do Pohody (XML objednávek) a ISDOC**, ověřený na 2 reálných Pohodách pilotů | kód | 3–4 dny | MKT-3 |
| 2.4 | **Demo bez registrace**: veřejný read-only demo tenant + rezervace 15min hovoru (Cal.com) + 60s video | kód + founder | 1–2 dny | BIZ-12 |
| 2.5 | **Důvěra**: stránka o zakladateli, /security, `security.txt`, DPA ke stažení, status stránka, IČO/adresa | kód + founder | 1 den | BIZ-17, BIZ-19 |
| 2.6 | **Aktivace místo signupů**: měřit návštěva → trial → první pozvaný odběratel → první zakázka od odběratele; lifecycle e-maily podle chování; boti pryč z funnelu a z MRR | kód | 2 dny | BIZ-09, BIZ-16 |
| 2.7 | **Virální smyčka**: patička v e-mailech a portálu odběratele „Taky dodavatel? Assoluto zdarma 30 dní" | kód | 0,5 dne | MKT-9 |
| 2.8 | **Pracovní fronta pro dílnu**: „čeká na mě" na dashboardu, slíbený termín při potvrzení + upozornění po termínu (oživí SLA), editace hlavičky zakázky, zakázky na detailu klienta | kód | 3 dny | IDEA-1, IDEA-4, T12, UX-11, UX-12, UX-13, LOGIC-12 |
| 2.9 | **Opakovaná objednávka** + **připomínka nepotvrzené nabídky** + **e-mail s nabídkou, PDF a potvrzením jedním klikem** | kód | 3 dny | IDEA-3, IDEA-2, IDEA-5, MKT-5 |
| 2.10 | **UX dávka**: klávesnice a middle-click v seznamech, potvrzení hromadných akcí, zachování filtrů, čitelný feed aktivit, jednotná terminologie (zakázky/zákazníci), labely, cache-busting assetů, mobilní menu | kód | 2 dny | UX-05, UX-07…UX-10, UX-14, UX-17, UX-18, UX-21…UX-25 |
| 2.11 | Obchod: 10 pilotních dílen „zavedení na klíč zdarma" výměnou za týdenní call a referenci; 20 oslovení denně (ARES, 2 kraje); 5 účetních kanceláří jako partneři | founder | průběžně | market.md §5 |

## Vlna 3 — náskok (dny 46–90)

Cíl: funkce, které ERP ani Excel nemají a které ušetří hodiny týdně.

| # | Co | Proč to vyhrává | Effort | Nálezy |
|---|---|---|---|---|
| 3.1 | **AI příjem objednávek**: přeposlaný e-mail / PDF / výkres → návrh zakázky s položkami k potvrzení (Claude API, extrakce do strukturovaného výstupu). Beta u 3–5 pilotů, měřit přesnost | Odstraní přepisování e-mailů, hlavní bolest cílovky. V SME segmentu to nikdo nemá | 1–2 týdny | MKT-4 |
| 3.2 | **Cenová paměť → návrh ceny**: poslední cena zákazník × produkt, později podobné díly | Rychlejší nabídky, konzistentní ceny | 2–3 dny | IDEA-6, MKT-5 |
| 3.3 | **Revize výkresů**: nahrazení, označení aktuální, opětovné potvrzení | Řeší přesně „vyrobeno podle špatné revize" z hero | 3 dny | IDEA-8, BIZ-10 |
| 3.4 | **Formální nabídka + „Schvaluji" s auditní stopou**, volitelně e-podpis (Signi) pro OEM | Větší odběratelé to vyžadují | 3 dny | MKT-6 |
| 3.5 | Import položek (CSV/vložení BOM), týdenní přehled otevřených zakázek odběrateli, automatický odpis materiálu zákazníka, odhad dodací lhůty z historie, zákaznický admin zve kolegy | Menší úspory, každá S–M | 1–2 týdny | IDEA-7, IDEA-9…IDEA-12 |
| 3.6 | 3 případové studie s čísly („hovorů týdně před/po") + 6 SEO landing pages | Opakovatelná akvizice | founder + kód | market.md §5 |

**Odloženo:** DACH (EUR, časová pásma, DE chyby, indexovatelné EN/DE URL — T14, MKT-12) až po 10 platících CZ zákaznících. Dotace OP TAK (MKT-13) nejsou prodejní páka.

---

## Release & repo hygiena

**Hotovo v této session:**
- Větve sjednocené: zůstávají jen `main` a `production` (oba `015496f`). 4 vzdálené a 5 lokálních mrtvých větví smazáno, starý worktree odstraněn. Všechno ověřeně sloučené, stash archivovaný do lokálního tagu `archive/stash-2026-04-21`.
- **PR #8** `chore/release-pipeline`: CI gate, `pg_dump` před migrací, auto-rollback, veřejná sonda, prune image, Dependabot, záloha s `0600`, dokumentace (DEPLOY_HETZNER §8, CLAUDE.md §11). Skript lokálně otestovaný proti stubu (úspěch / rollback / selhaný dump / retence).

**Zbývá:**
| Co | Kdo | Nález |
|---|---|---|
| Ochrana větví + mazání větví po merge (D7) | operátor / na příkaz | REL-3, REL-4 |
| Secret `DEPLOY_HOST_FINGERPRINT` | operátor | REL-6 |
| `APP_IMAGE_TAG` bez fallbacku na dubnový `latest` | kód | REL-7 |
| Smazat Railway, `ultraplan.md` → archiv (nahrazen tímto plánem) | kód + D5 | REL-8 |
| mypy do CI (regrese 0 → 1), novější `uv` | kód | BE-11, REL-10 |
| Levný staging: migrace proti anonymizovanému dumpu v CI | kód | REL-9 |

**Release flow od teď:** feature větev → PR → `main` (CI) → `git merge --ff-only origin/main` na `production` → push → deploy čeká na zelené CI → záloha → rollout → healthcheck → případně rollback.

## KPI na den 90 (2026-12-31)

- 0 nepravdivých tvrzení na webu (kontrola `/audit-verify`)
- Off-site záloha s ověřenou obnovou, error tracking, monitoring po 1–5 min
- 10 platících tenantů, z toho ≥ 5 s ≥ 5 aktivními odběrateli
- MRR ≥ 15 000 Kč
- ≥ 30 % trialů pozve aspoň jednoho odběratele do 7 dnů
- 3 jmenné reference na webu

## Hrubý odhad práce

| Vlna | Kód | Operátor/founder |
|---|---|---|
| 0 | ~1,5 dne | ~4 h + MSV |
| 1 | ~10–12 dní | ~2 h |
| 2 | ~15 dní | průběžně obchod |
| 3 | ~4–5 týdnů | piloti, případovky |

Doporučené pořadí v kódu: Vlna 0 se dá udělat najednou jako 3–4 PR (web, billing guard, validace, poppler). Ve Vlně 1 je kritická cesta 1.1, protože vyžaduje Stripe test účet; zbytek jde paralelně.

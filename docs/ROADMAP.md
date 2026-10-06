# Roadmapa Assoluto

Konsolidovaný stav k 2026-10-06. Zdroje: audit 2026-10-03
([ACTION_PLAN](audit-runs/2026-10-03-full/ACTION_PLAN.md),
[findings](audit-runs/2026-10-03-full/findings.md),
[market.md](audit-runs/2026-10-03-full/market.md),
[logic.md](audit-runs/2026-10-03-full/logic.md)) a rozhodnutí CEO z kola 2.
Kódy `MKT-*`, `IDEA-*`, `LOGIC-*`, `T*` odkazují na nálezy auditu.

Pravidlo pro celý dokument: na webu a v obchodě se slibuje jen sekce
**Hotovo**. Všechno níže je plán, ne funkce.

---

## 1. Hotovo

Podrobně v **[FIXES.md](audit-runs/2026-10-03-full/FIXES.md)** (nasazeno
2026-10-04, druhé kolo PR #20, #10, #15). Ve zkratce:

- **Provoz a data:** šifrované denní zálohy databáze + kopie všech příloh
  do druhé lokality Hetzner (dumpy 180 dní), ověřená obnova, `/healthz/backups`;
  disk, rotace logů, deploy s CI gate, dumpem před migrací a rollbackem;
  retenční job (smazaný tenant 30 dní, audit 3 roky) — `RETENTION_ENFORCE=true`.
- **Pravdivý web:** pryč vymyšlené reference, SLA, „šifrování at rest“;
  nový positioning „vedle Pohody“; `/security`, `security.txt`.
- **Billing:** bez Stripe ruční fakturace (žádné plány zdarma), opravený
  stavový automat Stripe (mock — před zapnutím 12 scénářů v test mode).
- **Integrita a funkce:** snapshot potvrzené nabídky, validace peněz,
  streamovaný upload/export, outbox e-mailů s retry, limity plánů.
- **Prodejnost (Vlna 2 z kódu):** pracovní fronta „Vyžaduje akci“
  (IDEA-1), slíbený termín + po termínu (IDEA-4), „Objednat znovu“
  (IDEA-3), cenová paměť zákazník × produkt (IDEA-6), připomínka
  nepotvrzené nabídky (IDEA-2), e-mail s nabídkou a potvrzením (IDEA-5),
  správce za zákazníka zve kolegy (IDEA-9), týdenní souhrn (IDEA-10, opt-in),
  export do POHODA / Money S3 (XML, validováno proti XSD), aktivační funnel,
  „Powered by“ smyčka, ukázkový tenant `ukazka.assoluto.eu`.
- **GDPR:** export/výmaz pro uživatele, kontakty i platformní identitu,
  ZIP export celého tenanta.

## 2. Rozpracováno — kolo 2 (2026-10-06)

| Položka | Rozhodnutí CEO | Stav |
|---|---|---|
| Early access zdarma do 31. 1. 2027, zakládající cena 490 Kč pro prvních 10 | E1 | tým EARLY |
| Časové pásmo tenanta (LOGIC-16) | E2 | tým TIMEZONE |
| Veřejné demo bez registrace | E3 | tým DEMO |
| Částky v PDF podle jazyka, „neposílat zákazníkovi e-mail“ | E4 | — |
| Smlouva o zpracování (`/dpa`), obchodní kit, tato roadmapa | E5 | tým LEGAL — viz `docs/sales/` |

## 3. Další na řadě (před Vlnou 3)

| # | Co | Proč | Kdo | Závisí na |
|---|---|---|---|---|
| N1 | **Zkušební import exportu do skutečné POHODA a Money S3** u pilotní dílny | Web zatím nesmí tvrdit „funguje s Pohodou“; účetní je spolurozhodovatel (market.md §2) | founder + pilot | první pilot |
| N2 | **Stripe:** nové ceny 1 490 / 2 990 Kč, 12 scénářů v test mode, pak live klíče | Automat je opravený jen proti mocku | operátor | konec early access 31. 1. 2027 — do té doby není nutné |
| N3 | **10 pilotů + 3 případové studie** podle `docs/sales/` | Žádné reference ani čísla zatím nemáme; KPI den 90 = 3 jmenné reference | founder | — |
| N4 | **Sjednotit Privacy a /security s DPA**: retence záloh (dnes „14 dní“, skutečně 14 dní lokálně + 180 dní off-site), audit log po smazání tenanta (Privacy slibuje 3 roky, retenční job ho maže s tenantem), lhůta hlášení incidentu (Privacy 72 h vs. DPA 48 h) | Právní texty si nesmí odporovat | kód + founder | rozhodnutí, která varianta platí |
| N5 | Kopie **jednotlivě smazaných** příloh v záložním bucketu (`attachments/` nemá expiraci; smazaný tenant už se maže) | Minimalizace uložení; výmaz na žádost subjektu | kód | rozhodnutí: lifecycle (např. X dní pro nereferencované objekty) vs. append-only |
| N6 | SEO landing pages (6 z market.md §5) | Opakovatelná akvizice | kód + founder | ověřit objemy klíčových slov |
| N7 | Partnerský program pro účetní kanceláře | Kanál s desítkami výrobních klientů | founder | N1 (export ověřený na reálné Pohodě) |
| N8 | Provoz: privátní GPG klíč záloh do správce hesel, `SystemMaxUse=500M` pro journald | Zbytky z FIXES „Operátor“ | founder (root) | — |

## 4. Vlna 3 — zaparkováno (náskok)

Pořadí podle poměru dopad/effort pro ICP. Žádná z položek nezačne bez
rozhodnutí v řádku „Rozhodnout“. Effort = odhad auditu (S ≈ do 1 dne,
M ≈ 2–4 dny, L ≈ 1–2 týdny).

### W3-1 AI příjem objednávek z e-mailu / PDF (MKT-4)

- **Problém:** i s portálem polovina odběratelů dál pošle PDF objednávku
  e-mailem; staff ji přepisuje, vzniká dvojí evidence a portál umře.
  Hlavní „wow“ pro demo; v CZ SME segmentu to dnes nikdo nemá (market.md §1c).
- **Návrh:** adresa `zakazky@<tenant>.assoluto.eu` → inbound e-mail
  (Postmark / Resend inbound) → extrakce LLM do strukturovaného výstupu
  (odběratel podle domény/IČO, položky, množství, materiál, požadovaný
  termín, číslo a revize výkresu z rohového razítka) → **koncept zakázky**
  ve stavu DRAFT s přílohami u položek; staff jen zkontroluje a potvrdí.
  Nikdy automatické odeslání odběrateli. Hosted-only.
- **Effort:** L (MVP 1–2 týdny), beta u 3–5 pilotů.
- **Rozhodnout:** poskytovatel LLM a API klíč; **náklad na dokument**
  (změřit na reálných PDF pilotů, stanovit strop Kč/dokument);
  model účtování (v ceně plánu / kredity, market.md §4); poskytovatel
  inbound e-mailu. **Nový dílčí zpracovatel** (LLM i inbound) = aktualizace
  `www/_subprocessors.html`, Privacy a DPA příloha 2 a **oznámení 30 dní
  předem** (DPA čl. 9) — naplánovat do harmonogramu.
- **Závislosti:** DNS/MX pro subdomény tenantů; retence vstupních e-mailů.
- **Metrika úspěchu:** podíl konceptů potvrzených bez opravy položek;
  přesnost extrakce položek a množství na vzorku pilotních PDF; čas od
  přijetí e-mailu do potvrzené zakázky (před/po podle case-study-template).

### W3-2 Revize výkresů (IDEA-8)

- **Problém:** „vyrobeno podle staré revize“ je nejdražší chyba zakázkové
  výroby; přílohy jsou dnes plochý seznam bez vazby revizí.
- **Návrh:** `order_attachments.supersedes_id` + popisek revize; nahrání
  „jako nová revize X“ označí starou jako nahrazenou, odznak „aktuální“,
  upozornění výrobě (notifikace o příloze už existuje); po stavu QUOTED
  nabídnout návrat k QUOTED k novému potvrzení (CLAUDE.md §18 — přechody
  staff neomezovat, jen nabídnout).
- **Effort:** M (1 migrace).
- **Rozhodnout:** je nové potvrzení po změně revize povinné, nebo jen
  doporučené?
- **Metrika úspěchu:** počet chyb ze špatné revize u pilotů (týden 0 vs. 12);
  podíl zakázek s více revizemi, kde je jedna označená jako aktuální.

### W3-3 Formální nabídka + „Schvaluji“, volitelně e-podpis (MKT-6)

- **Problém:** nákupčí OEM potřebuje dokument „Nabídka č. X“ do svého
  systému; bez něj vzniká paralelní e-mail s PDF a portál se obchází.
- **Návrh:** PDF nabídky s logem, platností, obchodními podmínkami a DPH
  generované při QUOTED; tlačítko „Schvaluji nabídku“ uloží jméno, čas,
  IP a hash PDF a pošle PDF oběma stranám. Kvalifikovaný e-podpis (Signi)
  až na poptávku Enterprise.
- **Effort:** M bez e-podpisu.
- **Rozhodnout:** DPH řádek a texty podmínek per tenant (navazuje na
  odloženou „DPH poznámku na PDF“ z T7); platnost nabídky výchozí/nastavitelná;
  e-podpis = nový dílčí zpracovatel (viz W3-1).
- **Závislosti:** snapshot potvrzené částky (hotovo, T7).
- **Metrika úspěchu:** podíl nabídek potvrzených v portálu (ne e-mailem);
  počet odběratelů, kteří PDF nabídky stáhli.

### W3-4 Import položek / BOM (IDEA-7)

- **Problém:** odběratelé posílají seznamy dílů o 30–200 řádcích; položky
  se dnes přidávají po jedné.
- **Návrh:** textové pole pro vložení z Excelu (TSV/CSV: SKU/popis,
  množství, jednotka), párování na katalog odběratele, náhled, pak
  hromadné přidání v jedné transakci.
- **Effort:** M.
- **Rozhodnout:** co s řádky bez shody v katalogu (volný text vs. odmítnout).
- **Metrika úspěchu:** čas založení zakázky s 30+ položkami; podíl zakázek
  založených importem.

### W3-5 Odhad dodací lhůty z historie (IDEA-12)

- **Problém:** slíbený termín se odhaduje od oka; SLA report pak ukazuje
  zpoždění.
- **Návrh:** při nacenění zobrazit medián CONFIRMED → DELIVERED pro
  daného odběratele / díl za posledních N zakázek vedle pole termínu.
  Žádné ukládání, počítá se za běhu.
- **Effort:** S.
- **Rozhodnout:** pracovní vs. kalendářní dny (svátky CZ).
- **Závislosti:** časová pásma (kolo 2, E2) kvůli správným datům.
- **Metrika úspěchu:** podíl zakázek dodaných do slíbeného termínu (SLA
  report) před/po.

### W3-6 Automatický odpis materiálu zákazníka (IDEA-11)

- **Problém:** pohyby materiálu zákazníka se zadávají ručně; zůstatek,
  který odběratel vidí, se rozchází se skutečností.
- **Návrh:** volitelně `order_items.asset_id` + `asset_qty`; odpis při
  přechodu do IN_PRODUCTION / DELIVERED v side effects **a v
  `_backfill_milestones`** (CLAUDE.md §18, jinak skoky stav přeskočí).
- **Effort:** M (1 migrace).
- **Rozhodnout:** v kterém stavu se odepisuje; co při návratu stavu zpět
  (storno pohybu vs. ponechat).
- **Metrika úspěchu:** podíl pohybů materiálu vzniklých automaticky;
  počet ručních korekcí zůstatku.

### W3-7 DACH: EUR + lokalizace (LOGIC-5, T14, MKT-12)

- **Problém:** měna je napevno CZK — německý tenant (i český dodavatel
  fakturující do DE/AT v EUR) má v nabídkách a PDF „Kč“.
- **Návrh:** výchozí měna tenanta (CZK/EUR) při registraci a v nastavení,
  přepis na úrovni zákazníka, `order.currency` se stanoví při založení,
  katalog jen v měně zakázky (bez kurzových přepočtů ve v1). Indexovatelné
  EN/DE URL (pak teprve hreflang), DE marketing. Časová pásma řeší kolo 2.
- **Effort:** M (měna) + marketing/SEO.
- **Rozhodnout:** CEO: DACH až po **10 platících CZ zákaznících** (T14;
  market.md MKT-12 doporučuje 20+). Měna per tenant vs. per zákazník.
  DE trh čeká e-fakturace (XRechnung/ZUGFeRD) a integrace (lexoffice,
  sevDesk, DATEV) — samostatné rozhodnutí.
- **Metrika úspěchu:** počet CZ tenantů s EUR odběrateli; první platící
  tenant z DE/AT.

## 5. Vědomě ne (teď)

- Dotace OP TAK jako prodejní argument (MKT-13).
- Vystavování na veletrzích (MSV 2026 rozhodnutím foundera vynecháno).
- Stránka o zakladateli s fotkou, externí monitoring dostupnosti
  (rozhodnutí kola 2).

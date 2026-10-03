# Trh & konkurence — Assoluto (2026-10-03)

Perspektiva 6 z [`BRIEF.md`](BRIEF.md). Prefix nálezů `MKT-`.
Všechny ceny jsou ověřené na webu **k 2026-10-03** (není-li uvedeno jinak).
Kurz ČNB k 2026-10-02 je 1 EUR = 24,465 Kč a 1 USD = 21,795 Kč
([cnb.cz](https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/)).
Co se ověřit nepodařilo, je označené **unverified**.

---

## 0. Co Assoluto dnes skutečně je (z kódu, commit `015496f`)

| Oblast | Stav v kódu | Evidence |
|---|---|---|
| Zakázky | 8krokový pipeline DRAFT → … → CLOSED, staff může skákat libovolně, milníky se doplní, komentáře (i interní), přiřazení, hromadné přechody, CSV export a PDF zakázky | `app/services/order_service.py`, `app/routers/orders.py:286,574,1186` |
| Nabídka / potvrzení | Staff nacení položky a termín, zákazník potvrdí přechodem stavu. Formální nabídka (PDF s podmínkami a platností) ani podpis neexistuje | `app/models/order.py:91-108` |
| Přílohy | PDF/DWG/DXF/STEP/STL až 50 MB, na S3, náhledy, k zakázce nebo k položce | `app/templates/www/features.html` |
| Katalog | Sdílený i per-zákazník, ceny a jednotky, autocomplete | `app/services/product_service.py` |
| Materiál zákazníka na skladě | Příjem / výdej / spotřeba / korekce, zákazník vidí jen čtení. **Na trhu vzácné.** | `app/models/asset.py`, `app/services/asset_service.py` |
| Notifikace | Pouze e-mail, routing podle události, souhlasu a relevance | `app/services/notification_service.py` |
| SLA report | Ano, z `delivered_at` vs. slíbený termín | `app/services/sla_service.py` |
| Integrace | **Žádné**: chybí API, webhooky, Pohoda/Money/ABRA i ISDOC. FAQ to přiznává („Pohoda XML connector is on our roadmap“) | `app/templates/www/index.html:427` |
| AI / „chytré“ funkce | **Žádné**: chybí extrakce dat, opakování objednávky i návrh ceny | grep `openai\|anthropic\|reorder` → nic |
| Jazyky | CS, EN, DE | `app/locale/{cs,de,en}` |
| SaaS | Stripe, 30denní trial bez karty, limity plánů | `migrations/versions/1003_billing.py:137-149` |
| Licence | AGPL-3.0, Community self-host zdarma bez limitů | `LICENSE`, `app/templates/www/pricing.html` |

**Aktuální ceník** (`1003_billing.py:144-147`, `pricing.html`). Ceny jsou konečné, protože provozovatel není plátce DPH.

| Plán | Kč/měs | ≈ EUR | Staff | Kontakty zákazníků | Úložiště |
|---|---|---|---|---|---|
| Community | 0 (self-host) | 0 | ∞ | ∞ | vlastní |
| Starter | 490 | 20 € | 3 | 20 | 2 GB |
| Pro | 1 490 | 61 € | 15 | 100 | 20 GB |
| Enterprise | dohodou | — | ∞ | ∞ | ∞ |

---

## 1. Konkurenční mapa

### 1a. Skutečný incumbent: e-mail + Excel + telefon

Prodáváte hlavně proti zvyku, ne proti softwaru. Váš vlastní homepage to
říká přesně („Phones that won't stop / Outlook as a drawing vault / Excel
only Jana updates“). Excel je zdarma, nikdo ho nemusí zavádět a zákazník
nemusí nic měnit. Assoluto proto musí ukázat úsporu v hodinách týdně a
v chybách kvůli špatné revizi výkresu. Funkce samotné nestačí.

### 1b. Tabulka alternativ

| Produkt | Kategorie | Cílový zákazník | Cena (zdroj, ověřeno 2026-10-03) | Zákaznický portál? | Čím vyniká | Hrozba pro Assoluto |
|---|---|---|---|---|---|---|
| **Pohoda** (Stormware) | CZ účetnictví/ERP | malé CZ firmy, de facto standard | mPohoda Basic 7,40 €/měs (SK ceník) — [stormware.sk PDF](https://www.stormware.sk/dnload/2026-09-01_Cennik_produktov_STORMWARE.pdf); CZ desktop ceník **unverified** (pohoda.cz/cenik → 404) | Ne (jen doplňky třetích stran) | Účetní ho znají, má XML import přijatých objednávek přes mServer ([pohoda-mcp popis](https://mcpservers.org/ko/servers/dg/pohoda-mcp)) | **Nízká jako konkurent, vysoká jako nutná integrace** |
| **Money S3 / S4 / S5** | CZ účetnictví/ERP | S3: mikro a malé, S5: střední „na míru“ | S3 Basic 2 990 Kč, Economy 7 990 Kč, Advanced 12 990 Kč jednorázově bez DPH ([money.cz](https://money.cz/konfigurator/)); S4/S5 jen přes kalkulačku ([moneyerp.com/cena](https://moneyerp.com/cs-cz/cena)) | S5 má „Zákaznický portál“ a e-shop konektor ([moneyerp.com](https://moneyerp.com/cs-cz/money-s5)), je to ale implementační projekt | EDI, konektory | Střední: u firem, které už S5 implementují |
| **Helios (Asseco) EasyCloud / iNuvio** | CZ ERP | malé až střední výroba | Cena na poptávku ([helios.eu](https://helios.eu/en/easy-for-small-company)) | Web rozhraní pro faktury a dodací listy + e-mail o stavu objednávky, implementací ([bart.sk case](https://www.bart.sk/en/case-studies/zemplin)) | Silná výroba (iNuvio), síť konzultantů | Střední u firem nad ~50 lidí |
| **ABRA Flexi / Gen** | CZ ERP | malé a střední firmy | **unverified** (ceník se nepodařilo načíst) | Ne nativně (unverified) | REST API, integrace | Nízká |
| **Factorify** | CZ APS/MES/ERP | výrobní firmy, spíš 50+ lidí | Od 3 500 Kč/měs za každých 10 mil. Kč obratu, support 10–30 tis. Kč/měs ([factorify.cz](https://www.factorify.cz/)) | Není zmíněn | Automatické plánování výroby; 40 zákazníků, 2 500+ uživatelů | Nízká: jiný problém (plánování), může být partner |
| **Raynet CRM** | CZ CRM | obchodní týmy SME | 449 / 949 / 1 199 Kč za uživatele/měs, e-podpis 250–490 Kč/měs ([raynet.cz/cenik](https://www.raynet.cz/cenik/)) | Ne | Obchodní pipeline, e-podpis | Nízká (CRM ≠ zakázky) |
| **Caflou** | CZ business management | služby, projekty | 11–16 $/uživatel/měs, nejvyšší „Shark“ 230 $ ([Capterra](https://www.capterra.com/p/166938/Caflou/pricing/)); CZ ceník **unverified** | Klientský portál **unverified** | Zakázky + fakturace + projekty | Střední u „univerzálních“ kupců |
| **Flowii** | SK/CZ CRM + zakázky | malé firmy | Free; CRM 9,35 €; Premium 22,10 €/uživatel/měs bez DPH ([flowii.com](https://www.flowii.com/cs/cenik)) | Ne (neuvedeno) | Řízení zakázek v Premium | Nízká |
| **Fakturoid** | CZ fakturace | OSVČ a mikro | Na maximum 393 Kč/měs, další uživatel 91 Kč ([fakturoid.cz/cenik](https://www.fakturoid.cz/cenik)) | **Ano**, klientský portál pro faktury a ISDOC, jen v nejvyšším plánu ([fakturoid.cz](https://www.fakturoid.cz/podpora/kontakty/klientsky-portal)) | Ukotvuje očekávání „portál = pár set Kč“ | Cenová kotva (pozor) |
| **Odoo** | Globální ERP | SME, s partnerem | Standard 11,90 €/uživatel/měs (14,90 € bez ročního závazku), Custom 22,90 €; portálové uživatele **zdarma** ([odoo.com/pricing](https://www.odoo.com/pricing)) | Ano: nabídky s online podpisem, objednávky, faktury | Šíře funkcí, AI-OCR moduly pro objednávky z PDF ([apps.odoo.com](https://apps.odoo.com/apps/modules/17.0/automaited_sales_order_ai)) | **Vysoká** u firem s IT/partnerem; implementace je ale projekt |
| **MRPeasy** | Globální MRP pro malé výrobce (estonský) | 10–200 zaměstnanců | Starter 49 $, Professional 69 $, Enterprise 99 $, Unlimited 149 $/uživatel/měs; od 11. uživatele 79 $ za 10 ([mrpeasy.com/pricing](https://www.mrpeasy.com/pricing/)) | **B2B Customer Portal od Professional** | Plnohodnotné MRP, CZ lokalizace (unverified) | **Vysoká**: nejbližší „levný MRP s portálem“ |
| **Katana** | Globální MRP (D2C/e-com) | e-commerce výrobci | Free, Core od 299 $/měs, Advantage na míru ([katanamrp.com](https://katanamrp.com/pricing/)); add-ony 49–298 $/měs ([costbench](https://costbench.com/software/manufacturing-erp/katana/hidden-costs/)) | Ne (B2B portál přes třetí stranu) | Shopify integrace | Nízká |
| **Fulcrum Pro** | US cloud ERP pro job shopy | 1–200 zaměstnanců | Na poptávku ([softwareconnect](https://softwareconnect.com/manufacturing/fulcrum)) | unverified | Quoting, scheduling, kvalita | Nízká v CZ/DE (US trh) |
| **ProShop ERP** | US paperless job-shop ERP | CNC dílny | Per-user, na poptávku ([Capterra](https://www.capterra.co.uk/software/155436/proshop)) | unverified | QMS, AS9100 | Nízká v CZ/DE |
| **Paperless Parts** | US quoting platforma pro job shopy | 5–500 lidí | Na poptávku ([paperlessparts.com/pricing](https://www.paperlessparts.com/pricing/)); „od 39 $/měs“ na GetApp je **unverified** a nepravděpodobné | Ano: Smart RFQ Form, buyer portal | **AI**: přeposlaný RFQ e-mail se rozparsuje (kontakt, díly, revize, množství), quote setup o 90 % rychlejší ([press](https://paperlessparts.com/press/paperless-parts-cuts-quote-setup-time-by-90-with-new-ai-supported-workflow)) | Nízká přímo, **vysoká jako vzor** |
| **Spanflug MAKE** | DE kalkulace CNC dílů z CAD | DE Lohnfertiger | Free 5 dílů/měs; Pro 399 €/měs, ročně 3 990 € ([ki-syndikat](https://www.ki-syndikat.de/tools/spanflug/)) | Ne (nástroj na kalkulaci) | Automatický odhad časů z CAD | Partner/kotva pro „AI nabídku“ v DACH |
| **Xometry / Laserhub** | Marketplace + instant quote | nákupčí OEM | Pro dodavatele zdarma (Xometry Partner Network) ([xometry.eu](https://xometry.eu/en/xometry-europe-expands-into-the-u-k/)) | Pro kupce ano | 18 marketplaces, 71 000 kupců ([investors](https://investors.xometry.com/node/10506/pdf)) | **Nepřímá**: kupce učí, že instant quote a online stav jsou normální, takže sílí tlak na malé dílny |
| **weclapp** | DE cloud ERP | DACH SME | 39–179 €/uživatel/měs ([zeeg.me](https://zeeg.me/de/blog/content/weclapp-preise)) | Kundenportal **unverified** | Angebot → Auftrag → Fertigung | Střední v DACH |
| **Xentral** | DE ERP (e-com) | DACH e-commerce | Starter 349 $/měs a výš ([pricingsaas](https://pricingsaas.com/companies/xentral)) | Ne (unverified) | Produkce jako add-on | Nízká |
| **ITM Customer Portal (SAP B1)** | Portál nad ERP | SAP B1 uživatelé | Od 350 €/měs ([GetApp](https://www.getapp.co.uk/software/2082000/itm-customer-portal)) | Ano | Hluboká integrace s ERP | **Cenová kotva**: „portál nad ERP“ = 350 €/měs |
| **Zoho Inventory** | Globální inventory | malé obchody | 29 / 79 / 129 / 249 $/měs ročně ([zoho.com](https://www.zoho.com/inventory/pricing/)) | Ano, od Standard | Ekosystém Zoho | Nízká (obchod, ne zakázková výroba) |
| **SuiteDash** | Generický portál | agentury, služby | 19 / 49 / 99 $/měs, neomezení uživatelé ([suitedash.com](https://suitedash.com/pricing/)) | Ano (generický) | Levné, white-label | Nízká: neumí výkresy, revize, výrobní stav |
| **Clinked** | Generický portál / VDR | profesní služby | Standard 239 $/měs (100 členů), Premium 599 $/měs ([clinked.com](https://www.clinked.com/pricing)) | Ano | Dokumenty, white-label | Nízká, ale je to kotva „portál = stovky €“ |
| **Assembly (dříve Copilot.com)** | Generický portál | služby | Free; Starter 49 $; Professional 99 $; Advanced 399 $+/měs ([assembly.com](https://assembly.com/pricing)). Že jde o přejmenovaný Copilot, je **unverified** | Ano | Billing, formuláře | Nízká |
| **Moxo** | Workflow portál (enterprise) | finance, služby | Team 5 000 $/rok ([moxo.com](https://www.moxo.com/pricing)) | Ano | AI agenti, e-podpis | Nízká |

### 1c. Co z mapy vyplývá

1. **V CZ chybí samostatný zákaznický portál pro zakázkovou výrobu za rozumnou cenu.**
   Portály dnes existují jen jako součást ERP (Money S5, Helios, Odoo,
   MRPeasy Professional), tedy s implementací a s výměnou celého IS. Druhá
   možnost jsou generické portály (SuiteDash, Clinked), které neznají
   výkres, revizi, stav výroby ani materiál zákazníka. Assoluto patří
   přesně mezi ně. Uniká mu ale, že toto místo existuje **jen tehdy, když
   Assoluto funguje vedle stávající Pohody nebo Money, ne místo nich.**
2. **Nejbližší přímý soupeř je MRPeasy Professional** (portál + MRP,
   69 $/uživatel). Pro 3 uživatele je to 207 $/měs ≈ 4 500 Kč.
   Assoluto Starter stojí 490 Kč, tedy asi 9× méně.
3. **Globální trend určuje AI na vstupu** (Paperless Parts, Odoo AI-OCR moduly,
   Werk24 API). Malé české dílny k tomu nemají dostupný nástroj.
4. **Cenové kotvy jsou dvě a míří proti sobě.** Fakturoid má portál za
   393 Kč, takže portál vypadá jako drobnost. ITM, Clinked a Paperless
   chtějí 239–600 $ měsíčně, protože portál nad ERP je profesionální
   nástroj. Assoluto se dnes postavilo ke kotvě Fakturoidu, i když řeší
   problém z té druhé kategorie.

---

## 2. Positioning a ICP

### ICP (ideální zákazník)

- **Kdo:** česká (později DACH) zakázková výroba s **5–50 zaměstnanci**,
  tedy kovovýroba, CNC obrábění, laser a ohraňování, svařování, lakovna,
  plasty, nástrojárna. Vyrábí podle výkresu zákazníka a má
  **20–200 opakujících se B2B odběratelů**.
- **Stav:** účetnictví v Pohodě nebo Money S3, zakázky v Excelu nebo
  v sešitě, výkresy v Outlooku. Majitel nebo mistr denně odpovídá na
  telefony „kdy to bude“.
- **Spouštěč nákupu:** chyba z diskuse „podle které revize výkresu“,
  odchod „Jany, která vede Excel“, tlak velkého odběratele (OEM) na
  transparentnost, nebo růst nad ~10 aktivních zakázek týdně.
- **Kdo rozhoduje:** majitel nebo jednatel, často spolu s účetní (ta
  hlídá, aby nevznikla dvojí evidence). **Proto je integrace s Pohodou
  podmínkou prodeje, ne bonus.**
- **Bonus segment:** výroba ze **svěřeného materiálu** (zákazník dodá
  plech, polotovary nebo přípravky). Evidenci materiálu zákazníka Assoluto
  už umí a v SME nástrojích ji skoro nikdo nemá.

### Positioning statement (doporučení)

> **Pro malé zakázkové výrobce, kteří vyrábějí podle výkresů svých
> zákazníků a utápějí se v telefonech, e-mailech a Excelu, je Assoluto
> zákaznický portál, kam odběratel nahraje výkres, schválí nabídku
> a sám vidí stav výroby. Na rozdíl od ERP (Helios, Money S5, Odoo,
> MRPeasy) se nic nemění: běží vedle Pohody nebo Money, zavede se za
> odpoledne a zakázky do účetnictví pošle jedním kliknutím.**

Krátká verze na web a LinkedIn:
**„Zákaznický portál pro zakázkovou výrobu. Vedle Pohody, ne místo ní.“**

Co z dnešního sdělení vyhodit nebo zmírnit:
- „Halfway between Excel and a 600K CZK ERP“ je srozumitelné, ale
  generické a vede ke srovnání s ERP, které Assoluto prohraje funkcemi.
  Lepší je stavět na tom, že ERP **doplňuje**.
- Bezpečnostní detaily (Argon2, CSRF, RLS) na features stránce cílovku
  nezajímají. Stačí jedna věta „data v EU, každý zákazník vidí jen své“
  a technické detaily přesunout na /security.

### Funkce: table stakes vs. diferenciace

| Table stakes (bez nich neprodáte, Assoluto **má**) | Table stakes, které Assoluto **nemá** | Diferenciace (Assoluto má) | Diferenciace k postavení |
|---|---|---|---|
| Login zákazníka, jen jeho data | **Export do Pohody/Money (XML) a ISDOC** | Materiál zákazníka na skladě | **AI příjem objednávek z e-mailu / PDF / výkresu** |
| Objednávka s přílohami (DWG/STEP) | **Formální nabídka PDF + závazné „Schvaluji“ s auditní stopou** | Revize výkresů vázané na položku | **Opakovaná objednávka z historie** („objednat znovu díl X rev. C“) |
| Stav zakázky + e-mail notifikace | Vlastní logo a barvy portálu (white-label) | SLA report včasnosti dodávek | **Návrh ceny z historie** (podobný díl, materiál, množství) |
| Komentáře, interní poznámky | Import zákazníků a katalogu z CSV/Pohody | CS/EN/DE, data v EU, open-source (AGPL) | Ověřené potvrzení nabídky (e-podpis, Signi/eIDAS) pro větší OEM |
| Katalog a ceníky per zákazník | API / webhooky | Lehké zavedení (minuty, ne měsíce) | SMS/WhatsApp o „hotovo k vyzvednutí“ (P3) |
| CSV/PDF export | Ročníplatba self-serve | | |

---

## 3. Nálezy

### MKT-1 — Ceny jsou výrazně pod trhem a signalizují hračku, ne nástroj na řízení zakázek
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: `migrations/versions/1003_billing.py:145-146` (Starter 49 000 haléřů, Pro 149 000). Srovnání: MRPeasy Professional s portálem 69 $/uživatel → 3 uživatelé ≈ 4 500 Kč ([mrpeasy.com/pricing](https://www.mrpeasy.com/pricing/)); ITM portál nad SAP B1 od 350 €/měs ([GetApp](https://www.getapp.co.uk/software/2082000/itm-customer-portal)); Clinked 239 $/měs ([clinked.com](https://www.clinked.com/pricing)); Assembly Professional 99 $/měs ([assembly.com](https://assembly.com/pricing)); Raynet 3 uživatelé START = 1 347 Kč ([raynet.cz](https://www.raynet.cz/cenik/))
- Dopad: Při 490 Kč (~20 €) se nevyplatí žádný placený akviziční kanál (veletrh, obchodní hovor, partner s provizí). Na 100 000 Kč MRR je potřeba ~204 Starter zákazníků. Nízká cena navíc u konzervativních majitelů budí podezření „co to je za levnou věc, neskončí to?“.
- Návrh: Nový ceník podle §4 (Start 990 Kč / Pro 2 490 Kč / Business 4 990 Kč). Stávajícím a early-access zákazníkům ponechat původní cenu 12 měsíců jako „zakládající zákazník“.
- Effort: S (DB řádky + šablony + Stripe ceny)
- Auto-fixable: no (cenové rozhodnutí foundera + nové Stripe price ID)

### MKT-2 — Limit „kontaktů zákazníka“ trestá přesně to chování, které produkt potřebuje
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: `1003_billing.py:145-146` (`max_contacts` 20 / 100), vynucováno v `app.platform.usage.ensure_within_limit` (CLAUDE.md §17). Konkurence portálové uživatele nepočítá: Odoo „External users … are not paying users“ ([odoo.com/pricing](https://www.odoo.com/pricing)), SuiteDash má neomezené kontakty ([suitedash.com](https://suitedash.com/pricing/))
- Dopad: Hodnota Assoluta roste s počtem přihlášených odběratelů (méně telefonů). Dílna s 25 odběrateli na Starteru narazí na limit ve chvíli, kdy produkt začíná fungovat, a buď přestane zvát, nebo odejde. Zákaznický kontakt vás přitom stojí skoro nic.
- Návrh: Kontakty zákazníků na všech plánech neomezené (fair-use). Měřit staff uživatele a odemykat funkce (integrace, AI, branding). Pro Enterprise případně „aktivní zákaznické firmy“.
- Effort: S
- Auto-fixable: no (rozhodnutí foundera)

### MKT-3 — Chybí export do Pohody/Money (XML) a ISDOC, přitom je to podmínka prodeje
- Severity: P1
- Status vs 2026-07-26: new
- Evidence: `app/templates/www/index.html:427` přiznává „Direct integration with Pohoda or Money S3 is not available yet … on our roadmap“. Pohoda přijímá přijaté objednávky přes XML/mServer (agenda `prijate_objednavky`, [popis](https://mcpservers.org/ko/servers/dg/pohoda-mcp)). ISDOC je český XML standard podporovaný „virtually all Czech accounting and ERP software“ ([eConnect](https://econnect.eu/en/docs/knowledge/document-formats/formats/isdoc); [portal.pohoda.cz](https://portal.pohoda.cz/dane-ucetnictvi-mzdy/ucetnictvi/kompatibilita-na-kterou-se-muzete-spolehnout-isdoc-propojuje-firmy-napric-systemy/)). Konektory třetích stran na Pohodu stojí 25–100 $/měs ([Alena](https://apps.shopify.com/alena?locale=cs)), takže trh za ně platí.
- Dopad: Účetní (spolurozhodovatel) zablokuje nástroj, který znamená přepisovat zakázky dvakrát. Bez integrace zůstává Assoluto „další evidencí“.
- Návrh: V1 (bez API klíčů, bez mServeru) přidá tlačítko „Export do Pohody“ u potvrzené a dodané zakázky, které vygeneruje Pohoda XML `dataPack` s přijatou objednávkou (adresa odběratele podle IČO, položky, ceny). Hromadný export za období. V2 přidá Money S3 XML a ISDOC pro podklady k fakturaci. V3 přidá obousměrný mServer (stav faktury zpět). Na web dát „Funguje s Pohodou“ jako hlavní argument.
- Effort: L (V1 ≈ 2–3 dny včetně testů proti reálné Pohodě)
- Auto-fixable: no (potřeba ověřit XSD a import v reálné Pohodě)

### MKT-4 — Žádná „chytrá“ funkce na vstupu: objednávky stále přicházejí e-mailem a staff je přepisuje
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Bez AI kódu (grep). Konkurence: Paperless Parts AI quote setup „cuts quote setup time by 90 %“, parsuje přeposlané RFQ e-maily ([press](https://paperlessparts.com/press/paperless-parts-cuts-quote-setup-time-by-90-with-new-ai-supported-workflow)). Odoo AI-OCR moduly dělají sales order z PDF ([apps.odoo.com](https://apps.odoo.com/apps/modules/17.0/automaited_sales_order_ai)). Werk24 API čte rohová razítka výkresů za 1,25 €/strana ([werk24.io](https://werk24.io/pricing)).
- Dopad: Realita ICP je, že i s portálem polovina odběratelů pošle dál PDF objednávku e-mailem. Když to Assoluto neumí pohltit, dílna vede dvě evidence a portál umře. Pro majitele je to zároveň nejsilnější „wow“ v demu.
- Návrh: **„Přepošlete objednávku na `zakazky@vasefirma.assoluto.eu`“.** Inbound e-mail (Postmark/Resend inbound) → LLM extrakce (odběratel podle domény a IČO, položky, množství, materiál, požadovaný termín, číslo a revize výkresu z rohového razítka) → **koncept zakázky** ve stavu DRAFT/SUBMITTED, přílohy u položek, staff jen potvrdí. Vytěžování účtovat kredity (viz §4). Hosted-only, protože vyžaduje API klíč.
- Effort: L (MVP 1–2 týdny)
- Auto-fixable: no (volba poskytovatele LLM, DPA/subprocesor do Privacy Policy)

### MKT-5 — Chybí opakovaná objednávka, přitom zakázková výroba je z velké části repeat business
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/routers/orders.py`: žádná route pro duplikaci nebo reorder. Model `OrderItem` má `product_id` a přílohy per položka (`app/models/order.py:114-138`), takže data pro reorder existují.
- Dopad: Odběratel, který objednává stejný díl každý měsíc, dnes zakládá zakázku znovu a nahrává znovu výkres. Repeat objednávka je přitom nejsilnější důvod, proč se odběratel do portálu vůbec přihlásí.
- Návrh: Tlačítko „Objednat znovu“ na uzavřené zakázce (kopie položek, výkresů a poslední ceny jako orientační, nový požadovaný termín). Pro staff: „poslední cena pro tohoto zákazníka“ u položky. To je zárodek funkce „návrh ceny z historie“.
- Effort: M
- Auto-fixable: yes (čistě aplikační funkce, bez externích závislostí)

### MKT-6 — Potvrzení nabídky není formální dokument; pro větší odběratele chybí auditovatelné „Schvaluji“
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: Přechod QUOTED → CONFIRMED je jen změna stavu (`order_service.transition_order`). PDF zakázky existuje (`orders.py:574`), ale nejde o nabídku s platností, obchodními podmínkami a identitou schvalujícího. Odoo má online podpis nabídky na portálu ([odoo.com](https://www.odoo.com/pricing)), Raynet e-podpis za 250–490 Kč/měs ([raynet.cz](https://www.raynet.cz/cenik/)).
- Dopad: Nákupčí OEM potřebuje dokument „Potvrzení objednávky / Nabídka č. X“ do svého systému. Bez něj vznikne paralelní e-mail s PDF a portál se obchází.
- Návrh: Šablona „Nabídka“ (PDF s logem, platností, OP, DPH) generovaná při QUOTED. Tlačítko „Schvaluji nabídku“ uloží jméno, čas, IP a hash PDF a pošle PDF oběma stranám. Kvalifikovaný e-podpis (Signi) až na poptávku Enterprise.
- Effort: M
- Auto-fixable: yes (bez e-podpisu třetí strany)

### MKT-7 — Unikátní funkce „materiál zákazníka na skladě“ je na webu schovaná a positioning je generický
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `app/templates/www/features.html` ji má jako čtvrtou sekci, homepage ji nezmiňuje v hero ani v „Why Assoluto“. Žádný z produktů v §1b ji jako hlavní funkci SME portálu neuvádí (pro MRPeasy, Odoo a Money **unverified**, mohou ji řešit přes sklad v ERP).
- Dopad: Pro výrobu ze svěřeného materiálu (běžné u laseru a ohraňování, lakoven a zinkoven) je to argument, který Excel ani Fakturoid nepřebijí. Dnes ho návštěvník nevidí.
- Návrh: Landing page `/svereny-material` („Evidence materiálu zákazníka pro zakázkovou výrobu“) + zmínka v hero. Homepage claim přepsat podle §2.
- Effort: S
- Auto-fixable: yes (copy + šablona; texty schválí founder)

### MKT-8 — Ceny jsou uvedené „konečné, bez DPH“; po překročení limitu 2 mil. Kč bude nutné zdražit o 21 %
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `pricing.html` („We are not VAT-registered, so the listed price is the final price“). Limit registrace je 2 000 000 Kč za kalendářní rok, okamžitá registrace při 2 536 500 Kč ([finance.cz](https://www.finance.cz/542189-limit-pro-registraci-k-dph-pro-osvc-vzroste-na-2-miliony-kc); [pohoda.cz](https://portal.pohoda.cz/dane-ucetnictvi-mzdy/dph/dph-v-roce-2025-1-dil-registrace-platce)). Při ceníku z §4 to odpovídá zhruba 70 zákazníkům na Pro.
- Dopad: B2B zákazník (plátce DPH) si DPH odečte, takže by mu „cena bez DPH“ nevadila. Ceník „konečná cena“ ale po registraci znamená buď 21% ztrátu marže, nebo komunikačně ošklivé zdražení. Prodej do DE/AT jako neplátce navíc vyžaduje režim identifikované osoby (**unverified**, konzultovat s daňovým poradcem).
- Návrh: Hned přejít na formulaci „ceny bez DPH; dokud nejsme plátci, DPH neúčtujeme“. Po registraci se pak nic nemění.
- Effort: S
- Auto-fixable: no (právní a daňová formulace)

### MKT-9 — Chybí virální smyčka: každý pozvaný odběratel je potenciální další zákazník, ale nic ho k tomu nevede
- Severity: P2
- Status vs 2026-07-26: new
- Evidence: `grep -ril assoluto app/email/templates` najde jen platformní e-maily (verifikace, trial). Pozvánky odběratelů a notifikace k zakázkám Assoluto vůbec nezmiňují, žádné „Powered by / Chcete vlastní portál?“. Odběratelé zakázkových výrobců jsou často sami výrobci se svými subdodavateli.
- Dopad: Nejlevnější akviziční kanál zůstává nevyužitý. U „supplier → customer“ portálů je to typicky hlavní růstová smyčka (obecný princip, pro tento trh **unverified**).
- Návrh: Patička e-mailů a portálu „Portál provozuje Assoluto · Chcete totéž pro své dodavatele/zákazníky?“ s UTM. Enterprise ji může vypnout (white-label jako placená hodnota).
- Effort: S
- Auto-fixable: yes

### MKT-10 — Reference na homepage vypadají jako ilustrativní, ne skutečné
- Severity: P1
- Status vs 2026-07-26: new (překryv s `business.md`, sloučit při konsolidaci)
- Evidence: `app/templates/www/index.html`: sekce „Early access / What our first customers say … Logos will appear here as soon as clients agree“ se třemi anonymními citacemi („Owner, metalwork shop — Morava, 12 employees“…).
- Dopad: Pro konzervativní cílovku je důvěra hlavní bariéra. Pokud citace nejsou od reálných zákazníků, jde o riziko nekalé obchodní praktiky a o reputační riziko v malém, propojeném oboru, kde se lidé znají.
- Návrh: Pokud nejsou reálné, okamžitě odstranit a nahradit „Hledáme 10 zakládajících zákazníků — cena 990 Kč navždy za případovou studii“. Reálné reference získávat podle §5.
- Effort: S
- Auto-fixable: no (founder musí potvrdit původ citací)

### MKT-11 — Roční platba jen „na požádání e-mailem“
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: `pricing.html` („Annual billing on request … email team@assoluto.eu“). MRPeasy dává „1 month free“ ročně self-serve ([mrpeasy.com](https://www.mrpeasy.com/pricing/)), Clinked −20 % ([clinked.com](https://www.clinked.com/pricing)).
- Dopad: Ztráta cash-flow a nižší churn. Ruční fakturace zatěžuje sólo foundera.
- Návrh: Roční Stripe price (10× měsíc), přepínač měsíčně/ročně na ceníku. Bankovní převod na fakturu ponechat pro Business a Enterprise.
- Effort: S
- Auto-fixable: no (Stripe price ID)

### MKT-12 — DACH expanze je předčasná; DE trh navíc čeká e-fakturační povinnost
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: DE lokalizace existuje (`app/locale/de`). B2B e-faktura je v DE povinná pro příjem od 2025, pro odesílání od 2027 (obrat > 800 000 €) a od 2028 pro všechny, ve formátech XRechnung/ZUGFeRD ([IHK Frankfurt](https://www.frankfurt-main.ihk.de/recht/uebersicht-alle-rechtsthemen/steuerrecht/umsatzsteuer-national/e-rechnungspflicht-ab-2025-6055774)). Konkurence v DACH: weclapp, Xentral, Spanflug, Laserhub.
- Dopad: DE zákazník bude čekat integraci s lexoffice/sevDesk/DATEV a ZUGFeRD. Bez lokálního obchodu a podpory je DACH drahý.
- Návrh: DACH až po 20+ platících CZ zákaznících. Do té doby DE jen pro české firmy s německými odběrateli (odběratel vidí portál německy), což je dnešní stav a dobrý argument.
- Effort: —
- Auto-fixable: no

### MKT-13 — Dotace OP TAK „Digitální podnik“ nejsou pro ICP prodejní páka
- Severity: P3
- Status vs 2026-07-26: new
- Evidence: Výzva má způsobilé výdaje 2,5–100 mil. Kč, uzávěrka 18. 2. 2026, jen v přechodových regionech ([businessinfo/optak](https://optak.gov.cz/UserFiles/File/1758822140v-zva-digit-ln-technologie.pdf))
- Dopad: Assoluto (~30–60 tis. Kč/rok) je hluboko pod minimem projektu. Slibovat „hradí dotace“ by bylo zavádějící.
- Návrh: Nesázet na dotace. Nanejvýš se nabídnout jako komponenta většího digitalizačního projektu přes integrátora (partner kanál, §5).
- Effort: —
- Auto-fixable: no

---

## 4. Pricing: benchmark a doporučení

### Benchmark (měsíčně, pro typickou dílnu: 3 staff uživatelé, 30 odběratelů na portálu)

| Řešení | Přepočet | ≈ Kč/měs | Poznámka |
|---|---|---|---|
| Fakturoid Na maximum + 2 uživatelé | 393 + 2×91 | **575 Kč** | jen portál faktur ([zdroj](https://www.fakturoid.cz/cenik)) |
| **Assoluto Starter (dnes)** | — | **490 Kč** | 20 kontaktů → těch 30 **nesplní** |
| Odoo Standard ×3 (ročně) | 3×11,90 € | 873 Kč | + implementace partnerem (unverified 50–300 tis. Kč) ([zdroj](https://www.odoo.com/pricing)) |
| Raynet START ×3 | 3×449 | 1 347 Kč | CRM, bez portálu ([zdroj](https://www.raynet.cz/cenik/)) |
| **Assoluto Pro (dnes)** | — | **1 490 Kč** | |
| SuiteDash Pinnacle | 99 $ | 2 158 Kč | generický ([zdroj](https://suitedash.com/pricing/)) |
| Assembly Professional | 99 $ | 2 158 Kč | 3 interní uživatelé ([zdroj](https://assembly.com/pricing)) |
| Factorify Standard (firma s 30 mil. Kč obratu) | 3×3 500 | 10 500 Kč | APS, ne portál ([zdroj](https://www.factorify.cz/)) |
| MRPeasy Professional ×3 | 3×69 $ | 4 512 Kč | MRP + B2B portál ([zdroj](https://www.mrpeasy.com/pricing/)) |
| Clinked Standard | 239 $ | 5 209 Kč | generický, 100 členů ([zdroj](https://www.clinked.com/pricing)) |
| ITM Customer Portal (SAP B1) | 350 € | 8 563 Kč | portál nad ERP ([zdroj](https://www.getapp.co.uk/software/2082000/itm-customer-portal)) |
| Spanflug MAKE Pro | 399 € | 9 762 Kč | jen kalkulace ([zdroj](https://www.ki-syndikat.de/tools/spanflug/)) |

**Závěr:** Assoluto je dnes na úrovni fakturačního doplňku, ale řeší
problém, za který vertikální a portálové nástroje berou 2 000–9 000 Kč
měsíčně. Prostor pro zdražení je zhruba 2–3×, pokud zároveň přibude
integrace (MKT-3) a odstraní se limit kontaktů (MKT-2).

### Doporučený ceník (ceny bez DPH; EUR pro DE/AT a anglický web)

| Plán | Kč/měs | EUR/měs | Ročně (2 měsíce zdarma) | Pro koho | Obsah |
|---|---|---|---|---|---|
| **Community** | 0 | 0 | — | technicky zdatní, self-host | beze změny (AGPL, bez limitů) |
| **Start** | **990** | **39 €** | 9 900 Kč / 390 € | dílna do ~10 lidí | 3 staff, **neomezení odběratelé**, 10 GB, e-mail notifikace, katalog, materiál zákazníka, opakovaná objednávka, CSV/PDF |
| **Pro** ★ | **2 490** | **99 €** | 24 900 Kč / 990 € | 10–50 lidí | 10 staff, 50 GB, **export do Pohody/Money + ISDOC**, formální nabídka PDF + „Schvaluji“, vlastní logo a barvy, SLA report, **AI příjem 100 dokumentů/měs** |
| **Business** | **4 990** | **199 €** | 49 900 Kč / 1 990 € | 50+ lidí nebo hodně odběratelů | 25 staff, 200 GB, vlastní doména, API/webhooky, AI 500 dokumentů/měs, bez patičky „Powered by“, prioritní podpora |
| **Enterprise / on-prem** | od 9 900 | od 399 € | faktura | OEM dodavatelé, on-prem | neomezeně, písemné SLA, e-podpis (Signi), instalace na vlastní server s podporou |

Doplňky:
- **Zavedení na klíč:** jednorázově 4 900 Kč / 199 €. Obsahuje import
  odběratelů a katalogu, nastavení stavů a 1 h školení přes Meet. Čeští
  majitelé implementaci očekávají a zvyšuje to aktivaci. Ve Startu
  volitelné, v Business v ceně.
- **AI kredity navíc:** 490 Kč / 19 € za 100 dokumentů. Pro kontrolu: Werk24
  stojí 1,25 €/strana pay-as-you-go ([werk24.io](https://werk24.io/pricing)),
  vlastní LLM extrakce je výrazně levnější (unverified, ověřit na vzorku 50
  reálných objednávek).
- **Zakládající zákazníci:** prvních 10–20 platících dostane Start nebo Pro
  za dnešních 490 / 1 490 Kč **napořád** výměnou za jmennou referenci
  a případovou studii.

Proč takhle:
- **Měří se staff uživatelé a funkce, ne odběratelé** (MKT-2). Čím víc
  odběratelů je v portálu, tím víc hodnoty a tím menší churn.
- **Pro za 2 490 Kč** je ~45 % ceny MRPeasy Professional pro 3 uživatele,
  ale vyžaduje nula implementace a nula změny účetnictví. Na úrovni
  generických portálů (Assembly, SuiteDash Pinnacle) by byl s vertikálními
  funkcemi navíc.
- **Funkce, za které se platí víc (integrace, AI, branding), jsou
  „hosted-first“.** AGPL kód zůstane otevřený, ale AI vyžaduje API klíč
  a provozní náklady, takže self-host Community ji fakticky mít nebude.
  To je férové open-core bez zavírání kódu.
- **Ekonomika:** blended ARPA ~2 000 Kč znamená, že 100 000 Kč MRR
  vyžaduje ~50 zákazníků místo ~200. To je dosažitelné founder-led
  prodejem během 12–18 měsíců (odhad, unverified).

---

## 5. Go-to-market pro sólo foundera

### Velikost trhu (hrubě)

- Jeden dokument ČSÚ uvádí v NACE C 5 952 podniků, z toho 4 843 malých
  (10–49) a 954 středních, a v NACE 24–25 (kovy a kovodělné výrobky)
  1 397 podniků ([czso.cz](https://www.czso.cz/documents/10180/36741187/2130031641B.pdf)).
  **Rozsah tabulky je unverified** (může jít o podmnožinu). Ber to jako
  dolní mez. Firmy pod 10 zaměstnanců tam nejsou, a i ty jsou ICP.
- Pracovní odhad ICP v CZ: **2 000–5 000 zakázkových výrob s 5–50 lidmi**
  (unverified). 2 % penetrace × 2 000 Kč ARPA ≈ 80–200 tis. Kč MRR.

### Akviziční kanály (podle poměru dopadu a nákladů pro sólo foundera)

| # | Kanál | Proč | Konkrétně | Náklad |
|---|---|---|---|---|
| 1 | **Přímé oslovení podle NACE (founder-led)** | Malá firma kupuje od člověka, ne od webu | Seznam z ARES/RES podle CZ-NACE 25.62 (obrábění), 25.11, 25.50, 25.61, 22.29 a kraje. Hledat firmy s webem typu „zakázková výroba podle výkresu“. 20 telefonátů nebo LinkedIn zpráv denně, nabídka: „15min ukázka na vašich datech“. | čas |
| 2 | **Virální smyčka přes odběratele** (MKT-9) | Odběratel strojírenské dílny je často také výrobce se svými dodavateli | Patička „Chcete vlastní portál?“ + měsíc zdarma navíc pro doporučeného i doporučujícího | ~0 |
| 3 | **Účetní a partneři Pohody/Money** | Účetní rozhoduje o „další evidenci“. Stormware a Money mají sítě partnerů (**unverified** podmínky). Účetní kanceláře mají desítky výrobních klientů. | Po dodání MKT-3: partnerský program 20 % opakovaná provize 12 měsíců + bezplatný účet pro kancelář. Webinář „Zakázky z portálu rovnou do Pohody“. | provize |
| 4 | **MSV Brno 6.–9. 10. 2026** | Hlavní průmyslový veletrh střední Evropy, 1 300+ vystavovatelů, 55 000 návštěvníků ([businessinfo.cz](https://www.businessinfo.cz/akce/mezinarodni-strojirensky-veletrh-msv-2026/)) | **Za 3 dny.** Nevystavovat (drahé, pozdě). Jít jako návštěvník s tabletem a demem, obejít menší české vystavovatele v halách obrábění a subdodávek, 30+ rozhovorů, sběr bolestí. Ověřit, zda jde ještě registrace do matchmakingu Kontakt-Kontrakt MSV 2026 ([businessinfo.cz](https://www.businessinfo.cz/akce/kontakt-kontrakt-msv-2026/), stav registrace **unverified**). | vstupné + den |
| 5 | **Asociace a komory** | Důvěra a přístup k členům | SST sdružuje výrobce obráběcích strojů (>70 % domácí produkce, [sst.cz](https://www.sst.cz/)), tedy spíš odběratele dílen než ICP, ale jejich subdodavatelé jsou ICP. Regionální HK ČR: přednášky „digitalizace zakázek bez ERP“. AMSP ČR. Regionální strojírenské klastry (**unverified** aktivita). | nízký |
| 6 | **SEO v češtině (dlouhý ocas)** | Levné, kumulativní, sólo zvládne | Viz klíčová slova níže. 1 landing page týdně. | čas |
| 7 | **LinkedIn obsah foundera** | Majitelé a ředitelé výroby jsou na LinkedInu aktivní (unverified pro mikrofirmy) | 2 posty týdně: příběh dílny, screenshot „zakázka podle revize C“, čísla z pilotů | čas |
| 8 | **Poptávkové servery** (epoptavka, poptavej a podobné) | Dílny tam už platí za poptávky | Reklama nebo partnerství „poptávky rovnou do vašeho portálu“ (**unverified** možnosti) | střední |
| 9 | Placené PPC | Nízké objemy, drahé B2B kliky | Až po validaci konverze landing page | střední |

**SEO klíčová slova (CZ).** Objemy jsou **unverified**, je nutné ověřit
v Google Keyword Planneru nebo Marketing Mineru.

| Klíčové slovo | Intent | Priorita |
|---|---|---|
| software pro zakázkovou výrobu | nákupní, přesně ICP | ★★★ |
| evidence zakázek výroba / evidence zakázek program | nákupní | ★★★ |
| zákaznický portál pro firmy / B2B zákaznický portál | nákupní, širší (konkurence e-shopů) | ★★ |
| sledování stavu zakázky zákazníkem | problémový | ★★ |
| správa výkresů zakázek / revize výkresů evidence | problémový, velmi specifický | ★★★ |
| evidence materiálu zákazníka / svěřený materiál evidence | problémový, nízká konkurence | ★★★ |
| Pohoda import objednávek XML / přijaté objednávky Pohoda | technický, po MKT-3 velmi silný | ★★★ |
| kovovýroba software / program pro kovovýrobu / CNC zakázky program | oborový | ★★ |
| alternativa ERP pro malou firmu / levné ERP výroba | srovnávací | ★★ |
| objednávkový systém pro odběratele | nákupní | ★★ |

DE (později): *Kundenportal Lohnfertiger*, *Auftragsverwaltung Lohnfertigung*,
*Zeichnungsverwaltung Kunden*, *Beistellmaterial verwalten* (unverified objemy).

### Prvních 90 dní (od 2026-10-03)

**Dny 1–14: validace a ostré nástroje**
- MSV Brno 6.–9. 10.: 30 rozhovorů, cíl 10 kontaktů na pilot. Mít
  připravené demo tenant s realistickými daty (výkresy, revize, materiál
  zákazníka).
- Rozhodnout o MKT-10 (reference) a MKT-8 (DPH formulace). Nasadit nový
  ceník (MKT-1, MKT-2) se zakládajícími cenami pro stávající.
- Napsat positioning podle §2 do hero a meta description.
- Nastavit trackování funnelu: návštěva → trial → první pozvaný odběratel
  → první zakázka od odběratele (to je aktivační metrika, ne signup).

**Dny 15–45: odstranit blokery prodeje**
- **MKT-3 V1:** export Pohoda XML (přijaté objednávky), otestovaný na
  2 reálných Pohodách pilotů.
- **MKT-5:** „Objednat znovu“.
- **MKT-9:** virální patička.
- 10 pilotních dílen na „zavedení na klíč“ zdarma výměnou za týdenní
  15min call a souhlas s referencí.
- Denně 20 oslovení (ARES seznam, 2 kraje: Zlínský + Jihomoravský nebo
  Vysočina, unverified volba, zvolit podle sítě kontaktů).

**Dny 46–90: náskok a opakovatelnost**
- **MKT-4 beta:** AI příjem objednávek z e-mailu u 3–5 pilotů.
  Měřit přesnost extrakce na reálných PDF.
- **MKT-6:** formální nabídka + „Schvaluji“.
- 3 případové studie (číslo „hovorů týdně před/po“, „hodin na přepis“).
- Partnerský program pro účetní: 5 kanceláří, 1 webinář.
- SEO: 6 landing pages z tabulky klíčových slov
  (`/zakazkova-vyroba`, `/svereny-material`, `/pohoda-objednavky`,
  `/revize-vykresu`, `/kovovyroba`, `/alternativa-erp`).

**Cílové KPI na den 90** (odhad, ambiciózní, ale reálné):
- 10 platících tenantů, z toho ≥ 5 s ≥ 5 aktivními odběrateli.
- MRR ≥ 15 000 Kč.
- ≥ 30 % trialů pozve aspoň jednoho odběratele do 7 dnů.
- 3 jmenné reference na webu.

---

## 6. Shrnutí pro ACTION_PLAN

| Vlna | Položka | Nález |
|---|---|---|
| 2 (prodejnost) | Nový ceník + neomezení odběratelé + DPH formulace | MKT-1, MKT-2, MKT-8 |
| 2 | Pravdivé reference / program zakládajících zákazníků | MKT-10 |
| 2 | Positioning „vedle Pohody“ + landing svěřený materiál | MKT-7 |
| 2 | Export Pohoda XML / ISDOC | MKT-3 |
| 2 | Virální patička | MKT-9 |
| 3 (náskok) | AI příjem objednávek z e-mailu/PDF | MKT-4 |
| 3 | Opakovaná objednávka → návrh ceny z historie | MKT-5 |
| 3 | Formální nabídka + Schvaluji | MKT-6 |
| — | Roční self-serve platba | MKT-11 |
| odloženo | DACH, dotace | MKT-12, MKT-13 |

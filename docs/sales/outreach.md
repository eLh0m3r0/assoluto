# Obchodní kit — oslovení zakázkových výrobců

Pracovní podklad pro founder-led prodej (market.md §5, ACTION_PLAN 2.11).
Platí k 2026-10-06. Všechno, co se tu o produktu tvrdí, musí sedět
s webem a kódem — při změně ceníku nebo funkcí kit aktualizuj.

> **Nic nevymýšlej.** Žádné reference, čísla „ušetří 10 hodin týdně“ ani
> loga zákazníků, dokud je nemáme podepsané v [pilot-agreement.md](pilot-agreement.md)
> a změřené podle [case-study-template.md](case-study-template.md).

---

## 1. Komu voláme (ICP — výtah z market.md §2)

| | |
|---|---|
| **Kdo** | Česká zakázková výroba s **5–50 zaměstnanci**: kovovýroba, CNC obrábění, laser a ohraňování, svařování, lakovna / povrchové úpravy, plasty, nástrojárna. Vyrábí **podle výkresu zákazníka**. |
| **Odběratelé** | 20–200 opakujících se B2B odběratelů. |
| **Dnešní stav** | Účetnictví v Pohodě nebo Money S3, zakázky v Excelu / sešitě, výkresy v Outlooku. Majitel nebo mistr denně odpovídá na „kdy to bude“. |
| **Spouštěč** | Výroba podle špatné revize výkresu; odchod člověka, který „vede Excel“; tlak velkého odběratele (OEM) na přehled; růst nad ~10 aktivních zakázek týdně. |
| **Rozhoduje** | Majitel / jednatel, často s účetní (hlídá, aby nevznikla dvojí evidence). |
| **Bonus** | Výroba ze **svěřeného materiálu** zákazníka — evidenci materiálu zákazníka Assoluto umí, v SME nástrojích je vzácná. |

**Jednou větou:** *Zákaznický portál pro zakázkovou výrobu. Vedle Pohody, ne místo ní.*

**Co dnes skutečně umíme** (jen tohle slibovat):
zakázky v 8 krocích od poptávky po předání, nacenění a termín, potvrzení
nabídky zákazníkem, výkresy a CAD soubory u zakázky i u položky (do 50 MB),
komentáře včetně interních, e-mailová upozornění, katalog s cenami pro
konkrétního zákazníka, materiál zákazníka na skladě, „Objednat znovu“,
připomínka nepotvrzené nabídky, report včasnosti dodávek, export zakázek
do XML pro POHODA a Money S3 (**zkušební import do skutečné Pohody/Money
zatím neproběhl** — na pilotu ho uděláme spolu), CSV/ZIP export všech dat,
CS/EN/DE rozhraní, data v EU (Hetzner, Německo), open source (AGPL).

**Co neumíme** (neslibovat, viz [ROADMAP.md](../ROADMAP.md)): AI příjem
objednávek z e-mailu/PDF, revize výkresů s nahrazením, formální nabídka
s e-podpisem, import BOM, odhad dodací lhůty, API/webhooky, vlastní
doména a logo jen na vyžádání (Enterprise).

**Nabídka (early access):**
- Zdarma do **31. 1. 2027**, bez karty. Data zůstávají.
- Potom 1 490 Kč / měsíc (Starter).
- **Prvních 10 dílen** si drží zakládající cenu **490 Kč / měsíc napořád**
  (výměnou za jmennou referenci — viz pilot-agreement.md).
- Veřejná ukázka bez registrace: **https://ukazka.assoluto.eu/demo**

---

## 2. Právní minimum před oslovením (přečti, než pošleš první e-mail)

Toto není právní rada; před rozjetím ve větším objemu si to nech potvrdit
advokátem.

- **E-mail:** obchodní sdělení e-mailem upravuje § 7 zákona č. 480/2004 Sb.
  (o některých službách informační společnosti) — zasílání je podmíněno
  předchozím souhlasem adresáta. ÚOOÚ, který zákon vymáhá, jej uplatňuje
  i na adresy firem. **Proto: první kontakt telefonem nebo přes LinkedIn,
  e-mail až jako navázání na rozhovor** („jak jsme se domluvili“) nebo
  když o něj člověk sám řekne. Šablony v §5 jsou psané takto.
- Každý e-mail: kdo píše (jméno, firma provozovatele, IČO v podpisu),
  jasně obchodní sdělení, a **jednoduchá možnost odmítnout** („Stačí
  odepsat ‚ne‘ a už se neozvu.“). Odmítnutí okamžitě zapsat do seznamu.
- **GDPR k seznamu kontaktů:** jméno a pracovní kontakt konkrétní osoby
  jsou osobní údaje. Zpracování na základě oprávněného zájmu (B2B
  oslovení) — vést jen nutné sloupce, zdroj údaje, datum; při prvním
  kontaktu umět říct, odkud kontakt máme; na námitku smazat. Seznam
  neukládat do sdílených veřejných tabulek.

---

## 3. Jak sestavit seznam firem (ARES / RES / firmy.cz)

Cíl: **200–300 firem ve 2 krajích**, z nich 20 oslovení denně.

### 3.1 Obory (CZ-NACE)

| Kód | Obor | Priorita |
|---|---|---|
| 25.62 | Obrábění | ★★★ (CNC dílny — jádro ICP) |
| 25.11 | Výroba kovových konstrukcí a jejich dílů | ★★★ |
| 25.61 | Povrchová úprava a zušlechťování kovů | ★★ (lakovny, zinkovny — často zakázkově) |
| 25.50 | Kování, lisování, ražení, válcování a protlačování kovů | ★★ |
| 25.73 | Výroba nástrojů | ★★ (nástrojárny) |
| 25.99 | Výroba ostatních kovodělných výrobků j. n. | ★ (smíšené, ručně třídit) |
| 22.29 | Výroba ostatních plastových výrobků | ★ (plasty podle výkresu) |

Kódy jsou z market.md §5; názvy oborů si při stahování ověř v číselníku
ČSÚ (CZ-NACE se aktualizuje). Kód v registru je to, co firma uvedla — často
neaktuální, rozhoduje web firmy.

### 3.2 Zdroje a postup

1. **RES (Registr ekonomických subjektů, ČSÚ — otevřená data).** Stáhni
   datovou sadu RES z katalogu otevřených dat ČSÚ / data.gov.cz. Obsahuje
   IČO, název, sídlo, převažující činnost (CZ-NACE) a kategorii počtu
   zaměstnanců. Strukturu sloupců si ověř v dokumentaci sady.
   Filtr: aktivní subjekty, NACE z tabulky výše, kategorie počtu
   zaměstnanců **5–49** (případně 50–99 jako rezerva), kraj sídla.
2. **ARES (ares.gov.cz).** Pro každé IČO dotáhni aktuální stav (zánik,
   insolvence, změna sídla). ARES má veřejné REST API — limity a parametry
   vyhledávání si ověř v dokumentaci API na ares.gov.cz, nedělej
   tisíce dotazů najednou.
3. **firmy.cz + web firmy.** Vyhledej podle oboru a kraje (např. „CNC
   obrábění“, „zakázková kovovýroba“, „laserové pálení“, „ohraňování“)
   a doplň firmy, které v registru chybí. Na webu firmy hledej
   kvalifikační signály (níže) a kontakt na majitele / jednatele /
   vedoucího výroby.
4. **LinkedIn.** Dohledej konkrétní osobu (jednatel, majitel, výrobní
   ředitel, mistr). Žádný scraping — ručně.

**Kraje:** market.md doporučuje začít ve **2 krajích** (např. Zlínský
+ Jihomoravský nebo Vysočina — volba není ověřená; vyber podle vlastní
sítě kontaktů a dojezdu na osobní schůzku).

**Kvalifikační signály na webu firmy** (stačí 2):
- „zakázková výroba“, „výroba podle výkresové dokumentace“, „kooperace“,
  „malé a střední série“;
- seznam technologií (CNC, laser, ohraňování, svařování, lakování);
- formulář „poptávka“ s přílohou výkresu, nebo e-mail typu `poptavky@`;
- reference na OEM odběratele (tlak na transparentnost).

**Vyřadit:** vlastní produkt bez zakázkové výroby, nad ~100 zaměstnanců
s nasazeným ERP typu Helios/SAP (není ICP), firmy v likvidaci.

### 3.3 Sloupce seznamu (tabulka)

`IČO · název · kraj · NACE · kategorie zaměstnanců · web · signály ·
kontaktní osoba · role · telefon · e-mail · LinkedIn · zdroj kontaktu ·
datum 1. kontaktu · kanál · stav (nový / volal / demo / pilot / ne) ·
souhlas s e-mailem (ano/ne + datum) · další krok + datum · poznámky`

---

## 4. Telefonát — 5minutový scénář

**Cíl hovoru:** ne prodat, ale (a) zjistit, jestli má problém, a (b)
domluvit 15minutovou ukázku nebo poslat odkaz na demo **se souhlasem**.

**0:00 — Otevření (15 s)**
> „Dobrý den, {jméno}, tady {vaše jméno}, Assoluto. Volám kvůli tomu,
> jak k vám chodí zakázky od odběratelů — máte minutu, nebo se mám
> ozvat jindy?“

Když ne: „Kdy se vám to hodí?“ → zapsat termín, zavěsit.

**0:30 — Důvod (20 s)**
> „Dělám zákaznický portál pro zakázkové výrobce. Mluvím s dílnami,
> kterým odběratelé posílají výkresy e-mailem a pak volají, kdy to bude.
> Jak to máte vy?“

**0:50 — Discovery (3 min, nech mluvit, zapisuj)**
1. Jak k vám dnes přijde objednávka — e-mail, telefon, portál odběratele?
2. Kolikrát týdně vám někdo volá nebo píše „kdy to bude“? Kdo to vyřizuje?
3. Kde máte zakázky — Excel, sešit, Pohoda/Money, něco jiného?
4. Stalo se vám, že se vyrábělo podle staré revize výkresu? Kolik to stálo?
5. Kolik hodin týdně někdo přepisuje objednávky z e-mailu do Excelu / účetnictví?
6. Držíte u sebe materiál nebo přípravky zákazníků? Jak ho evidujete?
7. Kdo by o takové věci rozhodoval — vy, nebo ještě účetní / společník?

**3:50 — Most (20 s)** — jen pokud zazněl problém:
> „To, co popisujete, je přesně to, na co je Assoluto: odběratel nahraje
> výkres do zakázky, potvrdí nabídku a sám vidí, v jakém je to stavu —
> a vám přestanou chodit dotazy. Běží vedle Pohody, nic neměníte.“

**4:10 — Uzavření (40 s)** — vyber jedno:
- „Můžu vám to za 15 minut ukázat přes obrazovku na příkladu z vaší
  dílny? Hodí se {den} v {čas}?“
- „Mám vám poslat odkaz na ukázku, kterou si proklikáte bez registrace?
  Na jaký e-mail?“ → **tím máš souhlas s e-mailem, zapiš ho.**
- Pokud nemá problém: „Rozumím, díky za čas. Můžu se ozvat za půl roku?“

**Vzkaz na záznamník (20 s):**
> „{vaše jméno}, Assoluto, zákaznický portál pro zakázkové výrobce.
> Volám kvůli tomu, jak k vám chodí výkresy a dotazy ‚kdy to bude‘.
> Zkusím to znovu {den}, případně {telefon}. Díky.“

---

## 5. E-mailové šablony

Krátké, bez příloh (PDF přílohy spouštějí spamfiltry), odkaz na demo,
podpis s IČO, možnost odmítnout. Posílat **po telefonu / LinkedInu nebo na
vyžádání** (viz §2).

### Šablona A — problém „kdy to bude“

**Předmět:** Dotazy „kdy to bude“ — ukázka, jak jsme se domluvili

> Dobrý den, {jméno},
>
> díky za čas po telefonu. Říkal jste, že vám týdně volá {X} odběratelů
> kvůli stavu zakázek. Assoluto je zákaznický portál pro zakázkovou
> výrobu: odběratel vidí stav své zakázky sám a vy mu ji jen posouváte
> v osmi krocích od poptávky po předání. Běží vedle Pohody, nic neměníte.
>
> Ukázka bez registrace (pohled dílny i odběratele):
> https://ukazka.assoluto.eu/demo
>
> Teď běží early access: zdarma do 31. 1. 2027, bez karty. Prvních 10
> dílen si drží cenu 490 Kč měsíčně napořád.
>
> Hodí se vám {den} na 15 minut, ať to projdeme na vaší zakázce?
>
> {vaše jméno} · Assoluto · {telefon}
> {provozovatel}, IČO {IČO} · assoluto.eu
> Nechcete další e-maily? Stačí odepsat „ne“.

### Šablona B — problém „špatná revize výkresu“

**Předmět:** Výkres u zakázky, ne v Outlooku

> Dobrý den, {jméno},
>
> jak jsme se bavili — výkresy chodí e-mailem a pak se dohledává, která
> verze platí. V Assoluto nahraje odběratel výkres přímo k zakázce nebo
> ke konkrétní položce (PDF, DWG, DXF, STEP, STL do 50 MB) a dílna ho
> má u zakázky, ne v něčí schránce.
>
> Proklikněte si to bez registrace: https://ukazka.assoluto.eu/demo
>
> Early access je zdarma do 31. 1. 2027. Prvním 10 dílnám necháváme
> 490 Kč měsíčně napořád.
>
> Stačí 15 minut — kdy se vám to hodí?
>
> {podpis jako výše}

*(Neslibovat „správu revizí“ — nahrazení a označení aktuální revize je
v roadmapě, ne v produktu.)*

### Šablona C — materiál zákazníka

**Předmět:** Evidence materiálu, který vám nechávají odběratelé

> Dobrý den, {jméno},
>
> zmínil jste, že u vás odběratelé drží {plech / polotovary / přípravky}.
> Assoluto vede příjem, výdej a spotřebu materiálu zákazníka a každý
> odběratel vidí jen svůj zůstatek — k tomu zakázky a výkresy na jednom
> místě.
>
> Ukázka: https://ukazka.assoluto.eu/demo
>
> Zdarma do 31. 1. 2027, prvních 10 dílen 490 Kč měsíčně napořád.
> Mám vám to ukázat na 15 minut {den}?
>
> {podpis jako výše}

---

## 6. LinkedIn

**Žádost o spojení (max. 300 znaků):**
> Dobrý den, {jméno}, dělám zákaznický portál pro zakázkové výrobce
> (výkresy, nabídky, stav zakázek pro odběratele, vedle Pohody). Rád
> bych se spojil s lidmi z {obor} na {kraj}. {vaše jméno}

**Zpráva po přijetí:**
> Díky za spojení. Krátká otázka: jak k vám dnes chodí objednávky od
> odběratelů — e-mailem s výkresem a pak telefon „kdy to bude“? Stavím
> na to portál, který běží vedle Pohody. Kdybyste chtěl, tady je ukázka
> bez registrace: https://ukazka.assoluto.eu/demo — a rád to projdu
> 15 minut na vaší zakázce. Early access je zdarma do 31. 1. 2027.

Bez automatizace a hromadných zpráv; 10–20 personalizovaných denně.

---

## 7. Námitky

| Námitka | Odpověď (pravdivě, bez přehánění) |
|---|---|
| **„Máme ERP / Pohodu / Money.“** | „Assoluto ERP nenahrazuje — je to okno pro vaše odběratele. Zakázky můžete exportovat do XML pro POHODA a Money S3. Zkušební import do vaší konkrétní Pohody bychom udělali spolu na pilotu.“ U Helios / Money S5 s vlastním portálem: „Pokud portál už máte a odběratelé ho používají, Assoluto nepotřebujete.“ |
| **„Stačí nám Excel.“** | „Excel funguje pro vás — odběratel do něj ale nevidí, takže volá. Portál nemění vaši evidenci, jen odběrateli ukáže stav a vezme od něj výkres na správné místo. Kolik těch telefonů týdně je?“ (nechat ho spočítat) |
| **„Moji zákazníci se nebudou přihlašovat.“** | „Nemusí všichni. Zakázku můžete založit vy za ně; upozornění na změnu stavu chodí e-mailem s odkazem. Přihlásit se musí, až když chtějí něco udělat — potvrdit nabídku, nahrát výkres. Začněte s 2–3 odběrateli, kteří nejvíc volají.“ |
| **„Je to drahé.“** | „Do 31. 1. 2027 to nestojí nic. Pak 1 490 Kč měsíčně, prvních 10 dílen 490 Kč napořád. Spočítejte si, kolik stojí hodina, kterou týdně strávíte telefonem a přepisováním — a jedna zakázka vyrobená podle špatného výkresu.“ (čísla ať řekne on; **naše čísla nemáme**) |
| **„Bezpečnost dat / kde to běží?“** | „Data jsou v EU, u Hetzneru v Německu. Každý dodavatel je oddělený přímo v databázi a každý odběratel vidí jen své zakázky. Denní zálohy (databáze šifrovaně) i kopie výkresů ve druhé lokalitě. Všechno je popsané na **assoluto.eu/security**, smlouva o zpracování osobních údajů je na **assoluto.eu/dpa**. Kód je open source.“ Neříkat „certifikace“, „ISO“, „šifrování na disku“, „99,9 % dostupnost“ — nemáme. |
| **„Jste malá firma, co když skončíte?“** | „Data si kdykoli stáhnete jako ZIP (CSV + všechny soubory). A protože je kód open source (AGPL), můžete si Assoluto provozovat i sami na vlastním serveru.“ |
| **„Teď nemám čas.“** | „Rozumím. Ukázka bez registrace trvá 5 minut, kdykoli: https://ukazka.assoluto.eu/demo. Můžu se ozvat {za 2 týdny}?“ |
| **„Pošlete mi to e-mailem.“** | „Rád — na jakou adresu? A co z toho, co jsem říkal, je pro vás nejdůležitější, ať pošlu jen to?“ (= souhlas s e-mailem + kvalifikace) |

---

## 8. Kadence navazování

| Den | Krok | Kanál |
|---|---|---|
| 0 | Telefonát (scénář §4) nebo žádost o spojení na LinkedIn | telefon / LinkedIn |
| 0 | Po souhlasu: e-mail podle šablony A/B/C | e-mail |
| 2 | Pokud nezvedl: druhý pokus o hovor jindy (ráno 7–8 h dílny často zvedají) | telefon |
| 5–7 | Navázání: jedna nová informace (např. konkrétní funkce k jeho problému), otázka na termín | e-mail (jen se souhlasem) / LinkedIn |
| 14 | Poslední zpráva: „Nechci otravovat — mám to uzavřít, nebo se ozvat v lednu?“ | e-mail / LinkedIn |
| 90 | Nurture: jednou za čtvrtletí, jen pokud řekl „později“ | dle souhlasu |

Pravidla: max. 4 pokusy, pak stav „ne / později“. Každé „ne“ a každé
odmítnutí e-mailů zapsat **ten den**. Týdně vyhodnotit: počet oslovení →
rozhovorů → ukázek → pilotů (metriky pilotu v case-study-template.md).

---

## 9. Po ukázce

1. Do 24 h shrnutí e-mailem: co řešíme, kdo z odběratelů začne, datum
   založení portálu.
2. Nabídnout pilot ([pilot-agreement.md](pilot-agreement.md)).
3. **Před spuštěním** změřit výchozí stav podle
   [case-study-template.md](case-study-template.md) — bez toho
   nebude případová studie.

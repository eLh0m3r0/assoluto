# Realizační zadání — oprava všeho z auditu 2026-10-03 (CEO brief)

## Cíl
Opravit nálezy z `docs/audit-runs/2026-10-03-full/` (kopie v této složce) tak,
aby produkt byl pravdivý, bezpečný, spolehlivý a prodejný — **a nic se
nerozbilo**. Hotovo = nasazeno na produkci, ověřeno E2E, testy zelené.

## Rozhodnutí CEO (závazná pro všechny týmy)
- **D1** Smyšlené reference z homepage pryč. Místo nich sekce „Program zakládajících
  zákazníků“: prvních 10 dílen dostane zavedení zdarma a cenu zamčenou na dnešních
  490 / 1 490 Kč napořád výměnou za jmennou referenci (řešeno ručně, bez kódu v billingu).
- **D2** Stripe zůstává vypnutý. Demo cesta v produkci nesmí rozdávat plány zdarma →
  „Online platba se připravuje, fakturujeme převodem — napište na team@assoluto.eu“.
  Stripe kód se opraví a otestuje s mocky, aby šel zapnout později.
- **D3** (upraveno 2026-10-04 po konzultaci s founderem) Nový ceník (ceny bez DPH, neplátce DPH): **Start 1 490 Kč / 59 €**, 3 staff,
  10 GB; **Pro 2 990 Kč / 119 €**, 10 staff, 50 GB; **odběratelé (kontakty) i zakázky
  neomezené** na všech placených plánech. Roční platba = 2 měsíce zdarma (fakturou).
  Enterprise = „napište nám“. Community (self-host, AGPL) až poslední karta.
  **Na ceníku a webu jen funkce, které v kódu skutečně existují.**
- **D4** Záporné / neplatné ceny a množství se odmítají (NaN, Infinity, < 0, mimo rozsah sloupce).
- **D6** Retence: smazání dat deaktivovaného tenanta po 30 dnech jako slibují Terms —
  job **ve výchozím stavu jen dry-run** (loguje, co by smazal); ostrý režim přepínač
  `RETENTION_ENFORCE=true`. Audit log 3 roky.
- **D7** Ochrana větví až po integraci (orchestrátor).

## Pravidla pro každý tým (POVINNÁ)
1. Pracuješ ve vlastním git worktree na vlastní větvi. **Nepushuješ, nesaháš na
   produkci, žádné SSH.** Commituješ po logických dávkách (Conventional Commits,
   zakonči commit řádkem `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`).
2. **Vlastní databáze** — před každým pytestem exportuj své 3 proměnné (viz zadání
   týmu). Nikdy nespouštěj testy proti DB `portal` ani proti DB jiného týmu.
3. Každá změna chování má test, který by bez opravy selhal. Na konci **celá sada**
   `uv run pytest -q` zelená, `uv run ruff check .` a `uv run ruff format --check .` čisté.
4. **Nesahej na `app/locale/`** (žádný pybabel extract/update/compile) — překlady
   udělá orchestrátor jednou na konci. Každý nový uživatelský text ale MUSÍ jít přes
   `{{ _("...") }}` v šablonách / `_t(request, "...")` v Pythonu, msgid anglicky.
   Pozor na `%` v msgid (CLAUDE.md §7).
5. **Migrace:** jen s přiděleným `revision` ID a `down_revision = "1008_order_assignment"`;
   orchestrátor je při integraci seřadí. Migrace musí mít funkční `downgrade()`.
6. Dodržuj invarianty v `CLAUDE.md` (§2 commit před BackgroundTasks, §3 CSRF,
   §6 core nikdy neimportuje `app.platform`, §8 redirecty, §9 flash na každý POST,
   §13–14, §18 staff přechody neomezovat, §19 souhlas s notifikacemi je absolutní).
7. Neměň URL ani chování, které nález nevyžaduje. Nepřidávej závislosti bez nutnosti
   (pokud ano, `uv add` a zdůvodni v reportu).
8. Nevymýšlej fakta (firmy, čísla, citace, certifikace). Co nevíš, vynech.
9. Pokud něco nestihneš nebo je to riskantní, raději to **vynech a napiš proč**, než
   abys commitnul polovičatou věc.
10. Závěrečný report (tvoje poslední zpráva): seznam commitů; tabulka nález → stav
    (`fixed` / `partial` / `deferred` + důvod); nové migrace; nové env/settings;
    co musí udělat operátor; výsledek posledního běhu testů (počet passed).

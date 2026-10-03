# Audit run 2026-10-03-full — „Mother of all reviews"

**Spuštěno**: 2026-10-03 23:20 CEST · **Commit**: `015496f` (`main` = `production` = produkce)
**Předchozí běh**: [`2026-07-26-e2e`](../2026-07-26-e2e/) · **Zadání**: [BRIEF.md](BRIEF.md) · **Plán**: [ACTION_PLAN.md](ACTION_PLAN.md)

## Počty

| Perspektiva | soubor | P0 | P1 | P2 | P3 | celkem |
|---|---|---|---|---|---|---|
| Bezpečnost | [security.md](security.md) | 0 | 3 | 2 | 7 | 12 |
| Backend & provoz | [backend.md](backend.md) | 1 | 2 | 12 | 8 | 23 |
| Byznys logika — vady | [logic.md](logic.md) | 0 | 6 | 14 | 4 | 24 |
| Byznys logika — nápady | [logic.md](logic.md) | — | 4 | 5 | 3 | 12 |
| UI/UX (živý web) | [ux.md](ux.md) | 0 | 1 | 19 | 9 | 29 |
| Obchod & GTM | [business.md](business.md) | 1 | 8 | 9 | 1 | 19 |
| Trh & konkurence | [market.md](market.md) | 0 | 4 | 6 | 3 | 13 |
| Release engineering | [release.md](release.md) | 0 | 2 | 4 | 4 | 10 |
| Codex (GPT-6 Astra) | [codex.md](codex.md) | 0 | 8 | 4 | 0 | 12 |
| **Celkem (surově)** | | **2** | **38** | **75** | **39** | **154** |

Po sloučení duplicit zbývá **~110 unikátních problémů ve 14 tématech** (níže).

## Kontext, který mění priority

Ověřeno přímo na produkci (read-only SSH, jen přítomnost proměnných, bez hodnot):

- **Stripe je kompletně vypnutý** — `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*` prázdné. Billing běží v demo režimu.
- **Žádný platící zákazník.** Signup funnel tvoří boti (BIZ-09), databáze má 9,8 MB.
- **Disk 88 %**, 97 starých image = 26 GB (BE-02).
- **Zálohy jen na stejném disku**, nešifrované, `0664`; `RCLONE_REMOTE` a `BACKUP_GPG_RECIPIENT` nenastavené (BE-01).
- 573 testů zelených, žádné úniky tajemství v historii veřejného repa (security.md § Git history scan).

Důsledek: nejdřív ochránit data a přestat slibovat, co není pravda; pak umožnit správně brát peníze; potom prodejnost. Polishing má smysl až s prvními piloty.

## Témata (deduplikováno)

Severity tématu = nejvyšší ověřená severity jeho členů.

| # | Téma | Sev | Členové | Kdo |
|---|---|---|---|---|
| T1 | **Zálohy nejsou zálohy** — jen lokálně, nešifrované, S3 přílohy bez zálohy; web slibuje „šifrované denní zálohy" | P0 | BE-01, BIZ-05, BIZ-07, SEC-4, REL (umask, PR #8) | operátor + kód |
| T2 | **Disk se zaplní za ~9 deployů** | P1 | BE-02, REL (prune, PR #8) | PR #8 + jednorázový prune |
| T3 | **Billing v demo režimu dává plány zdarma a pak tenanta odřízne**; trial končí potichu; trialista na Starteru nemá čím zaplatit | P0 | BIZ-01, BIZ-02, LOGIC-4, UX-01, BIZ-09 (MRR tile) | operátor + kód |
| T4 | **Stripe stavový automat není připravený na ostrý provoz** — změna plánu = druhé předplatné, události starých subskripcí přepisují nové, resubscribe zůstane zamčený, opakované zrušení prodlužuje přístup, prázdný webhook secret | P1 (P0 při spuštění) | SEC-1, LOGIC-6, BIZ-03, Codex-1, -3, -4, -5, -6, -7, -8, -12, Codex-2, LOGIC-18, LOGIC-23, SEC-11, BE-12 | kód (Stripe test mode) |
| T5 | **Marketing slibuje, co produkt nemá** — vymyšlené reference, „Nejoblíbenější" bez zákazníků, šifrování at rest, redundance + 99,9 % SLA, termíny/SLA, revize, PDF náhledy, vlastní doména, telefon, mock-up s neexistujícími funkcemi | P1 | BIZ-04, MKT-10, UX-20, BIZ-05, BIZ-06, BIZ-10, BE-04, BIZ-18, UX-03, UX-19, UX-16, BIZ-11, BIZ-15, UX-26 | kód (copy) + rozhodnutí |
| T6 | **Stav předplatného dodavatele vidí jeho zákazníci** (402 „Upgrade", banner po splatnosti, 404 po odříznutí) | P1 | LOGIC-3, BIZ-13 | kód |
| T7 | **Integrita nabídky** — potvrzení není vázané na částku, prázdné/nulové nabídky, mazání výkresu po potvrzení bez auditu, PDF bez DPH základu | P1 | LOGIC-2, LOGIC-7, LOGIC-13, LOGIC-17, UX-06 | kód |
| T8 | **Validace vstupů** — `NaN`/`Infinity`/záporné ceny shodí seznam zakázek | P1 | LOGIC-1 | kód |
| T9 | **Výkon a dostupnost uploadu** — blokující boto3 na jediném workeru, celé tělo v RAM, export ZIP v RAM | P1 | SEC-3, BE-03, BE-17, BE-16 | kód |
| T10 | **Slepý provoz** — žádný error tracking (500 ze září nikdo neviděl), uptime check reálně ~4 h, joby bez auditu, e-maily bez outboxu, ztráta při deployi | P2 | BE-05, BE-06, BE-13, BE-09, BE-08, BE-18 | operátor + kód |
| T11 | **GDPR a retence** — Identity bez exportu/výmazu, neúplné exporty, slib výmazu po 30 dnech bez kódu, bez retence auditů/S3 | P1 | SEC-2, SEC-5, SEC-10, BIZ-08, BE-19, SEC-12, BIZ-19 | kód + rozhodnutí |
| T12 | **Termíny a SLA jsou mrtvé** — `promised_delivery_at` nemá zapisovatele, hlavička zakázky je neměnná | P2 | BE-14, LOGIC-19, LOGIC-12, UX-11, IDEA-4, LOGIC-21 | kód |
| T13 | **Limity plánů jdou obejít** — disable→invite→reactivate, souběh, support uživatel se počítá do limitu | P2 | LOGIC-8, Codex-9, Codex-10, LOGIC-20, LOGIC-9, LOGIC-10 | kód |
| T14 | **Lokalizace pro DACH** — měna natvrdo CZK, vše v UTC, chyby platformy natvrdo česky, EN/DE bez URL | P2 | LOGIC-5, LOGIC-16, UX-04, BE-22, UX-29 | kód (odložit s DACH) |

Mimo témata: zbylé UX nálezy (pracovní fronta, klávesnice, potvrzení hromadných akcí, terminologie, přístupnost — UX-05…UX-28), drobnosti bezpečnosti (SEC-6…SEC-9), code health (BE-11, BE-20…BE-23), release (REL-3…REL-10). Nápady a tržní doporučení (IDEA-*, MKT-*) jsou zapracované přímo v akčním plánu.

## Ověření orchestrátorem

Nálezy s největším dopadem jsem ověřil přímo v kódu nebo na produkci, nespoléhal jsem jen na agenty:

| nález | výsledek | jak |
|---|---|---|
| BE-01 / T1 | potvrzeno | SSH: `RCLONE_REMOTE`/`BACKUP_GPG_RECIPIENT` prázdné, cron je nenastavuje, dumpy `-rw-rw-r--` v `/home/deploy/backups` |
| BE-02 / T2 | potvrzeno | SSH: `docker system df` → 97 images, 26,25 GB reclaimable, `/` 88 % |
| BIZ-01 / SEC-1 | potvrzeno | SSH: všechny `STRIPE_*` prázdné → SEC-1 dnes nezneužitelné, při spuštění P0 |
| BIZ-04 | potvrzeno | `app/templates/www/index.html:357-400`, šablona z 2026-04-16 |
| BE-04 | potvrzeno | `Dockerfile:84-87` neinstaluje poppler |
| BE-06 | potvrzeno | `gh run list` — rozestupy Uptime check 3,5–4,7 h |
| LOGIC-1 | potvrzeno | `orders.py:925` holé `Decimal(unit_price)`; agent reprodukoval 500 |
| UX-02 | potvrzeno | `ux-evidence/home-390.png` |
| Codex-2 | potvrzeno | `platform/routers/billing.py:88-99` nekontroluje `target.is_active` |
| Codex-3 | potvrzeno | `webhooks.py:206-219` nechá `canceled` + nové sub ID → `:249-259` pak zahodí `updated` pro nové ID jako „stale" |
| Codex-7 | potvrzeno (service vrstva) | `billing/service.py:197-207` posune `current_period_end` na teď při každém zrušení |
| Codex-11 | potvrzeno, sníženo na P3 | `notification_service.py:249-262` — týká se jen tenanta bez jediného aktivního staffu |
| Codex-1, -4, -5, -6, -8, -9, -10, -12 | `unverified` | věrohodné, ověřit testy ve Stripe test mode v rámci T4 |

## Úplný index

| id | sev | titulek |
|---|---|---|
| [SEC-1](security.md) | P1 | Stripe webhook accepts payloads signed with an empty secret when `STRIPE_WEBHOOK_SECRET` is unset |
| [SEC-2](security.md) | P1 | Platform `Identity` still has no GDPR export or erasure route |
| [SEC-3](security.md) | P1 | Attachment upload blocks the single event loop and reads the full body before the size check |
| [SEC-4](security.md) | P2 | `privacy.html` now promises encrypted B2 backups; the script and playbook still ship them unencrypted by default |
| [SEC-5](security.md) | P2 | Art. 15/20 exports are still incomplete for both principal types |
| [SEC-6](security.md) | P3 | Platform logout does not bump `Identity.session_version` |
| [SEC-7](security.md) | P3 | Production honours a client-supplied `X-Tenant-Slug` header |
| [SEC-8](security.md) | P3 | Order emails (comment excerpts, file names) reach invited-but-never-accepted addresses |
| [SEC-9](security.md) | P3 | No test pins that one customer's contacts never get mail about another customer's order |
| [SEC-10](security.md) | P3 | A contact can erase themselves and the controller (the tenant) is not told |
| [SEC-11](security.md) | P3 | The manual subscription editor still has no tests and still 500s on a malformed `quick_action` |
| [SEC-12](security.md) | P3 | The cookies policy still doesn't match the cookies the app sets |
| [BE-01](backend.md) | P0 | There is no off-site backup at all: nightly dumps sit on the same disk as the database, and the script can't even see… |
| [BE-02](backend.md) | P1 | Production disk is 88 % full: 97 old images (26 GB reclaimable) and no prune step, so about 9 more deploys fill the disk |
| [BE-03](backend.md) | P1 | Attachment upload and thumbnailing still block the single event loop on boto3, and the thumbnail task holds a DB tran… |
| [BE-04](backend.md) | P2 | PDF thumbnails have never worked in production: `pdf2image`/poppler aren't in the image (0 of 44 PDFs have a preview)… |
| [BE-05](backend.md) | P2 | Still no error tracking or alerting: a real production 500 on 2026-09-08 went unnoticed, and `/readyz` checks only th… |
| [BE-06](backend.md) | P2 | The "every 15 minutes" uptime tripwire actually runs every 3–7 hours |
| [BE-07](backend.md) | P3 | The signup honeypot branch returns a 500 instead of the fake "check your e-mail" page |
| [BE-08](backend.md) | P2 | Endpoints that schedule e-mail without an explicit commit hold their DB connection and open transaction through the w… |
| [BE-09](backend.md) | P2 | E-mail has no durable outbox: three attempts within ~6 s, then the mail is gone, and in-flight mails are lost on ever… |
| [BE-10](backend.md) | P2 | Migrations run unattended in the container entrypoint with no pre-migration backup, and the deploy gate probes a stat… |
| [BE-11](backend.md) | P2 | mypy regressed from 0 to 1 error, and CI doesn't run mypy, so nothing caught it |
| [BE-12](backend.md) | P2 | Test coverage is thinnest on money and cross-tenant auth paths: the subscription editor, cancel, refunds, and the sub… |
| [BE-13](backend.md) | P2 | Scheduled jobs still write no audit events; in production they have already disabled 5 tenants without a trace |
| [BE-14](backend.md) | P2 | The SLA dashboard is still dead: `promised_delivery_at` has no writer, and 0 of 29 production orders have one |
| [BE-15](backend.md) | P2 | The order-item product picker loads only the first 200 products alphabetically; product #201 onward can't be picked |
| [BE-16](backend.md) | P2 | Tenant export builds the whole ZIP, every attachment included, in RAM on a 3.8 GB box with no swap |
| [BE-17](backend.md) | P3 | Uploads are still read fully into memory before the size limit is checked |
| [BE-18](backend.md) | P3 | `LOG_LEVEL` is still shadowed by the base compose file in production |
| [BE-19](backend.md) | P3 | No retention for audit events or S3 objects, and attachment create/delete leave S3 and the DB inconsistent on failure |
| [BE-20](backend.md) | P3 | Versioned static assets are served with no `Cache-Control` by the single Python worker |
| [BE-21](backend.md) | P3 | Bulk status change accepts an unbounded list of order IDs and runs 4–6 recipient queries per order |
| [BE-22](backend.md) | P3 | i18n: catalogs are healthy, but cs carries 224 dead entries and platform flows still hard-code Czech |
| [BE-23](backend.md) | P3 | Code health: dead functions, and `alembic check` can't serve as a drift guard |
| [LOGIC-1](logic.md) | P1 | A customer contact can take down the supplier's order list with one `unit_price=NaN` POST |
| [LOGIC-2](logic.md) | P1 | Quote acceptance is not bound to an amount — the customer can "confirm" a price they never saw |
| [LOGIC-3](logic.md) | P1 | The supplier's SaaS billing state is shown to the supplier's *customers* (402 "Upgrade plan", overdue banner, 404 aft… |
| [LOGIC-4](logic.md) | P1 | Trial ends silently by default and the tenant is hard-cut 3 days later |
| [LOGIC-5](logic.md) | P1 | Currency is hard-coded CZK end-to-end — a German tenant cannot quote in EUR |
| [LOGIC-6](logic.md) | P1 | Plan change still opens a second Stripe subscription; deletion webhook does not check which subscription died |
| [LOGIC-7](logic.md) | P2 | Quotes can be sent and confirmed with unpriced or zero items; customers can submit empty orders |
| [LOGIC-8](logic.md) | P2 | Plan caps on users and contacts are bypassed by disable → invite → reactivate |
| [LOGIC-9](logic.md) | P2 | Last-admin guard counts support-access and never-accepted admins — a tenant can end up with no usable admin |
| [LOGIC-10](logic.md) | P2 | The 14-day invite purge ignores "Resend invite" — a re-sent link dies days later and the contact silently vanishes |
| [LOGIC-11](logic.md) | P2 | Customer activity feed reveals internal comments and staff assignment |
| [LOGIC-12](logic.md) | P2 | Order header is immutable — no way to fix customer, title, requested date or set a promised date |
| [LOGIC-13](logic.md) | P2 | Attachment deletion is unaudited and allowed after confirmation — the drawing the customer agreed to can disappear wi… |
| [LOGIC-14](logic.md) | P2 | Customer-specific price does not take precedence over the shared SKU; picker silently truncates at 200 |
| [LOGIC-15](logic.md) | P2 | Customer roles are cosmetic and a customer cannot be blocked / archived |
| [LOGIC-16](logic.md) | P2 | Every timestamp in UI, PDF and filters is UTC; Europe/Prague appears nowhere |
| [LOGIC-17](logic.md) | P2 | Order PDF / quote states no VAT basis and rounds half-even |
| [LOGIC-18](logic.md) | P2 | Manual subscription editor: still untested and crashable; "set active" never expires; no tenant reactivation |
| [LOGIC-19](logic.md) | P2 | SLA dashboard still structurally dead and still counts cancelled orders |
| [LOGIC-20](logic.md) | P2 | Support-access user counts against the tenant's paid user cap and receives tenant mail |
| [LOGIC-21](logic.md) | P3 | Backfill still fabricates `delivered_at` on DRAFT → CLOSED jumps |
| [LOGIC-22](logic.md) | P3 | Every staff move — including corrections and "back to Draft" — emails the customer, with no reason field |
| [LOGIC-23](logic.md) | P3 | An owner of two tenants can only ever bill the first one |
| [LOGIC-24](logic.md) | P3 | Customer-owned material: movements accept another customer's order, ADJUST can go negative, nothing is audited |
| [IDEA-1](logic.md) | P1 | "Needs action" queues on the staff dashboard |
| [IDEA-2](logic.md) | P1 | Quote follow-up: reminder + expiry for unconfirmed quotes |
| [IDEA-3](logic.md) | P1 | Reorder / duplicate a previous order |
| [IDEA-4](logic.md) | P1 | Promise date on confirmation + overdue alerts (also revives the SLA report) |
| [IDEA-5](logic.md) | P2 | Quote email that carries the total, the PDF and a one-click confirm |
| [IDEA-6](logic.md) | P2 | Price memory: suggest last price per customer × product when quoting |
| [IDEA-7](logic.md) | P2 | Paste / CSV import of line items (BOM) |
| [IDEA-8](logic.md) | P2 | Drawing revisions: supersede, mark current, require re-confirmation |
| [IDEA-9](logic.md) | P2 | Customer admins invite their own colleagues |
| [IDEA-10](logic.md) | P3 | Weekly open-orders statement to each customer |
| [IDEA-11](logic.md) | P3 | Auto-consume customer material when the order enters production |
| [IDEA-12](logic.md) | P3 | Lead-time estimate from history |
| [UX-01](ux.md) | P1 | After email verification the main button goes to billing details and Stripe Checkout, which contradicts "30 days free… |
| [UX-02](ux.md) | P2 | On a phone the homepage hero mock-up is broken: the content is squeezed into a 180 px column and half the table is cu… |
| [UX-03](ux.md) | P2 | The hero mock-up sells features the app does not have: a "Due" column, a "3 waiting on you" counter, an items summary |
| [UX-04](ux.md) | P2 | Signup, login, verification and billing error messages are hard-coded in Czech, so EN/DE prospects get Czech errors |
| [UX-05](ux.md) | P2 | Rows in the order, product and asset lists can't be opened with a keyboard or middle-click |
| [UX-06](ux.md) | P2 | Deleting an order item or a drawing takes one click with no confirmation |
| [UX-07](ux.md) | P2 | Bulk status change has no confirmation, yet it emails every contact on up to N orders |
| [UX-08](ux.md) | P2 | The orders list loses the "Responsible / Assigned to me" filter when you page |
| [UX-09](ux.md) | P2 | Dashboard "Recent activity" shows raw enums and unmapped action codes |
| [UX-10](ux.md) | P2 | Inconsistent Czech terms: Objednávky vs. Nová zakázka, Klienti vs. zákazníci, Majetek vs. Sklad |
| [UX-11](ux.md) | P2 | "Promised delivery" can't be filled in anywhere: the order detail always shows "—" and the SLA item in the main menu … |
| [UX-12](ux.md) | P2 | No work queue: no due date, no sorting and no "waiting on me" in the list, and the dashboard is three counters |
| [UX-13](ux.md) | P2 | The client detail page has no orders for that client and no "New order for this client" button |
| [UX-14](ux.md) | P2 | `customer_contacts.last_login_at` is stored but never shown |
| [UX-15](ux.md) | P2 | The tenant landing page `4mex.assoluto.eu/` doesn't name the supplier and says "Zákaznický portál pro SME" |
| [UX-16](ux.md) | P2 | Support response times contradict each other between the homepage and the pricing page |
| [UX-17](ux.md) | P2 | Static assets always carry `?v=0.1.0` and send no `Cache-Control`, so browsers can serve stale CSS/JS after a deploy |
| [UX-18](ux.md) | P2 | Inputs with no label (placeholder only) |
| [UX-19](ux.md) | P2 | The homepage says "Nebo napište / zavolejte" (write or call us), but no phone number appears anywhere on the site |
| [UX-20](ux.md) | P2 | Social proof: "Nejoblíbenější" (most popular) plus anonymous testimonials during an early-access phase |
| [UX-21](ux.md) | P3 | Anglicisms in customer-facing Czech |
| [UX-22](ux.md) | P3 | The mobile menu has no theme toggle, and the main navigation has no active state |
| [UX-23](ux.md) | P3 | The status stepper sits below the items and attachments, and the current step has no `aria-current` |
| [UX-24](ux.md) | P3 | The Confirmed and Delivered badges share one colour |
| [UX-25](ux.md) | P3 | Czech typography: closing quotes and straight quotes |
| [UX-26](ux.md) | P3 | "Osmikrokový proces" (8-step process) next to a list of 7 statuses on the homepage |
| [UX-27](ux.md) | P3 | The signup form doesn't show which plan you chose |
| [UX-28](ux.md) | P3 | Small copy and grammar issues |
| [UX-29](ux.md) | P3 | EN/DE have no indexable URL |
| [BIZ-01](business.md) | P0 | Production billing runs in demo mode: "Upgrade" gives the plan away free, says it worked, and then cuts the customer off |
| [BIZ-02](business.md) | P1 | A Starter trialist has no button to pay for Starter; the "choose a plan" email leads to a dead end |
| [BIZ-03](business.md) | P1 | "Change plans any time, prorated to the day; downgrade at next period" is not what the code does |
| [BIZ-04](business.md) | P1 | The homepage's "first customers" testimonials and the "Nejoblíbenější" badge are not backed by any customer |
| [BIZ-05](business.md) | P1 | "Encrypted at rest" and "encrypted backups" are false, and "daily backups" sit on the same disk as the database |
| [BIZ-06](business.md) | P1 | Uptime story ("UptimeRobot 24/7", "redundant infrastructure", "99.9 % SLA") has nothing behind it |
| [BIZ-07](business.md) | P2 | The subprocessor table names Backblaze for drawings and backups, but production uses Hetzner Object Storage and makes… |
| [BIZ-08](business.md) | P1 | Terms and Privacy promise deletion 30 days after deactivation; no code deletes anything |
| [BIZ-09](business.md) | P1 | The trial funnel is all bots, the nurture emails go to unconfirmed addresses, and the MRR tile shows 5 880 Kč of reve… |
| [BIZ-10](business.md) | P1 | Feature claims still ahead of the code: "you quote the price **and deadline**", "the right **revision** every time" |
| [BIZ-11](business.md) | P2 | Three different support promises, no tracker, and contact-form leads can vanish silently |
| [BIZ-12](business.md) | P2 | "Request a 15-min demo" is a contact form where you type "ukázka"; there is nothing to click without signing up |
| [BIZ-13](business.md) | P2 | The supplier's own customers see "Payment overdue" and "Subscription ended" banners |
| [BIZ-14](business.md) | P2 | Packaging fits a Czech job shop poorly: seat-based caps, a 3× jump, 2 GB for CAD, a free unlimited tier shown first, … |
| [BIZ-15](business.md) | P2 | Leftover "daňový doklad" wording contradicts the non-VAT-payer position, and "invoice emailed the same day" has no co… |
| [BIZ-16](business.md) | P2 | No lifecycle email beyond the three nurture steps, and the nurture ignores whether the trialist has done anything |
| [BIZ-17](business.md) | P2 | Trust surfaces a B2B buyer checks are missing: founder, /security, security.txt, DPA, status |
| [BIZ-18](business.md) | P2 | The Enterprise card promises a custom domain ("portal.yourfirm.cz") that hosting cannot serve |
| [BIZ-19](business.md) | P3 | Legal page housekeeping: version never bumped, Terms say a Starter-only trial, the EU ODR link is dead, the self-host… |
| [MKT-1](market.md) | P1 | Ceny jsou výrazně pod trhem a signalizují hračku, ne nástroj na řízení zakázek |
| [MKT-2](market.md) | P1 | Limit „kontaktů zákazníka“ trestá přesně to chování, které produkt potřebuje |
| [MKT-3](market.md) | P1 | Chybí export do Pohody/Money (XML) a ISDOC, přitom je to podmínka prodeje |
| [MKT-4](market.md) | P2 | Žádná „chytrá“ funkce na vstupu: objednávky stále přicházejí e-mailem a staff je přepisuje |
| [MKT-5](market.md) | P2 | Chybí opakovaná objednávka, přitom zakázková výroba je z velké části repeat business |
| [MKT-6](market.md) | P2 | Potvrzení nabídky není formální dokument; pro větší odběratele chybí auditovatelné „Schvaluji“ |
| [MKT-7](market.md) | P2 | Unikátní funkce „materiál zákazníka na skladě“ je na webu schovaná a positioning je generický |
| [MKT-8](market.md) | P2 | Ceny jsou uvedené „konečné, bez DPH“; po překročení limitu 2 mil. Kč bude nutné zdražit o 21 % |
| [MKT-9](market.md) | P2 | Chybí virální smyčka: každý pozvaný odběratel je potenciální další zákazník, ale nic ho k tomu nevede |
| [MKT-10](market.md) | P1 | Reference na homepage vypadají jako ilustrativní, ne skutečné |
| [MKT-11](market.md) | P3 | Roční platba jen „na požádání e-mailem“ |
| [MKT-12](market.md) | P3 | DACH expanze je předčasná; DE trh navíc čeká e-fakturační povinnost |
| [MKT-13](market.md) | P3 | Dotace OP TAK „Digitální podnik“ nejsou pro ICP prodejní páka |
| [REL-1](release.md) | P1 | Deploy na produkci neběžel až po CI |
| [REL-2](release.md) | P1 | Žádná záloha před migrací, žádný rollback |
| [REL-3](release.md) | P2 | `main` ani `production` nemají ochranu větví; repo je veřejné |
| [REL-4](release.md) | P3 | Po merge se větve nemažou |
| [REL-5](release.md) | P2 | Žádná automatizace aktualizací závislostí |
| [REL-6](release.md) | P2 | SSH host key VPS se neověřuje |
| [REL-7](release.md) | P2 | `APP_IMAGE_TAG` má fallback `latest`, který ukazuje na dubnový release |
| [REL-8](release.md) | P3 | Mrtvé/alternativní deploy cesty a bordel v kořeni |
| [REL-9](release.md) | P3 | Chybí staging |
| [REL-10](release.md) | P3 | CI drobnosti || [Codex-1](codex.md) | P1 | Plan changes create additional paid subscriptions |
| [Codex-1](codex.md) | P1 | Plan changes create additional paid subscriptions |
| [Codex-2](codex.md) | P1 | Disabled tenant administrators retain billing authority |
| [Codex-3](codex.md) | P1 | Successful resubscription can remain permanently canceled locally |
| [Codex-4](codex.md) | P1 | Late payment failures defeat cancellation enforcement |
| [Codex-5](codex.md) | P1 | Events for old subscriptions overwrite replacement subscriptions |
| [Codex-6](codex.md) | P1 | Stripe updates undo administrative suspension |
| [Codex-7](codex.md) | P1 | Repeated cancellation extends unpaid access indefinitely |
| [Codex-8](codex.md) | P1 | Nonpaying states other than canceled have no access cutoff |
| [Codex-9](codex.md) | P2 | Reactivation bypasses paid seat/contact limits |
| [Codex-10](codex.md) | P2 | Concurrent creation exceeds plan limits |
| [Codex-11](codex.md) | P2 | Notification fallback bypasses authorization and consent |
| [Codex-12](codex.md) | P2 | Out-of-order refunds are permanently lost |

## Status legend

Každý nález začíná jako `open`. `/audit-fix` a ruční opravy mění status v perspektivních souborech na `fixed`, `wontfix` nebo `manual`.

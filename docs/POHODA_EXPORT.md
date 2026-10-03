# Export of orders to POHODA (Stormware)

Assoluto exports orders as a POHODA XML file of **received orders**
(*Přijaté objednávky*), so the accountant imports them instead of
retyping them. Code: `app/services/accounting_export.py`, route
`app/routers/exports.py`, tests `tests/test_accounting_export.py`.

## Who can export and from where

* **Tenant admins only.** Staff with another role get 403, customer
  contacts get 403.
* **Admin → Accounting export** (`/app/admin/exports`): choose statuses,
  period (order creation date), client, VAT rate and your own IČO, then
  **Download for POHODA**.
* **Orders list → Export to POHODA** downloads the orders matching the
  filters currently applied to the list.
* By default **drafts and cancelled orders are left out**. Tick them on
  the export page if you really want them.
* One file holds at most 5,000 orders. Export a shorter period if you
  hit the limit.
* File name: `pohoda-objednavky-YYYYMMDD.xml`, encoding **Windows-1250**.
  Stormware's developer page says *"XML data jsou uložena v kódování
  Windows-1250"*. Characters outside that code page, such as emoji, are
  kept as XML character references.

## How the accountant imports the file

Stormware manual, chapter 16.7 *XML import/export*
(<https://www.stormware.cz/prirucka-pohoda-online/datova_komunikace/xml_import-export/>):

1. In POHODA open the accounting unit (company) the orders belong to.
2. Menu **Soubor → Datová komunikace → XML import/export…**
3. Choose **Soubor** (single file), then in **Vstupní složka nebo soubor
   (request)** pick the downloaded `pohoda-objednavky-….xml`.
4. In **Výstupní složka nebo soubor (response)** pick where POHODA should
   write the result report, and finish the wizard.
5. The result appears in the **XML log** agenda (*Request* and *Response*
   can be opened there). The imported orders appear in the
   **Přijaté objednávky** agenda.

Stormware's FAQ shows the same flow:
<https://www.stormware.cz/podpora/faq/pohoda/195/Jak-z-programu-POHODA-exportovat-a-nasledne-zpet-importovat-data-v-XML-formatu/?id=3256>.

### IČO of the accounting unit

The `dataPack` element carries an `ico` attribute. Stormware: it *"vybírá
účetní jednotku v programu POHODA do které se budou data načítat"*
(<https://www.stormware.cz/pohoda/xml/obecny-obchod/pro-vyvojare/>). It
must be the IČO of **the supplier (the tenant)**, not the customer's.

* By default Assoluto fills in the tenant's billing IČO
  (`tenants.settings.billing_ico`, entered under Billing details on
  hosted plans).
* The export page lets the admin change it, or clear it. When the field
  is empty the attribute is left out of the file.
* The XSD text says that without `ico` the `key` attribute is checked
  instead. Whether a plain GUI import into the open unit accepts a file
  with neither attribute has **not been tested in a real POHODA**. If the
  import complains, fill in your IČO.

## Field mapping

| POHODA element | Source in Assoluto |
|---|---|
| `dat:dataPack/@id` | `assoluto-<tenant>-<timestamp>` |
| `dat:dataPackItem/@id` | order number |
| `ord:orderType` | `receivedOrder` |
| `ord:numberOrder` | order number (e.g. `2026-000123`) |
| `ord:date` | date the customer submitted the order, otherwise the creation date (Europe/Prague) |
| `ord:dateTo` (*Vyřídit do*) | promised delivery date, otherwise the requested date |
| `ord:text` | order title (max 240 chars) |
| `ord:note` | order notes |
| `ord:intNote` | `Assoluto <number>` |
| `typ:company` | customer name |
| `typ:ico`, `typ:dic` | customer IČO / DIČ (left out if missing or longer than the schema allows, never truncated) |
| `typ:street`, `typ:city`, `typ:zip` | `customers.billing_address` JSON keys `street`/`line1`, `city`, `zip`/`postal_code` |
| `ord:orderItem/ord:text` | item description (max 90 chars; the `SKU — ` prefix of catalogue items is removed) |
| `ord:code` | catalogue SKU of the linked product |
| `ord:quantity`, `ord:unit` | quantity, unit (max 10 chars) |
| `ord:payVAT` | always `false`: prices are without VAT |
| `ord:rateVAT` | `none` by default; `high` / `low` when chosen on the export page |
| `typ:unitPrice` | unit price, under `homeCurrency` for CZK, otherwise under `foreignCurrency` |
| `ord:orderSummary/ord:foreignCurrency/typ:currency/typ:ids` | order currency when not CZK. No exchange rate is sent, so POHODA uses its own rate list. |

**VAT.** Assoluto has no tenant-level VAT model. Prices are exported
exactly as entered with `payVAT=false`. The default is `rateVAT=none`,
because a tenant that is not a VAT payer must not have VAT added. A VAT
payer selects **Standard rate** (`high`) or **Reduced rate** (`low`), and
POHODA then calculates VAT on top of the exported prices.

**Not exported:** country (POHODA expects a code from its own country
list), payment method, price level, stock links (items are imported as
text items), attachments and comments.

**Duplicates.** POHODA's *Kontrola duplicity dávek* compares the dataPack
and item IDs. Each download gets a new dataPack ID, so importing two
overlapping files creates duplicate orders. Export by period: for
example, the previous month once at the start of the next one.

## Schema sources and validation

* Import overview and official samples (*Objednávky*):
  <https://www.stormware.cz/pohoda/xml/dokladyimport/>
* Sample received order:
  <https://www.stormware.cz/xml/samples/version_2/import/Objednavky/order_01_v2.0.xml>
* XSDs: <https://www.stormware.cz/schema/version_2/data.xsd>,
  `order.xsd`, `type.xsd` (plus their imports, same directory)

The XSDs are not committed, because Stormware does not state
redistribution terms. To run the strict schema test:

```bash
mkdir -p /tmp/pohoda-xsd && cd /tmp/pohoda-xsd
curl -sSO https://www.stormware.cz/schema/version_2/data.xsd
# fetch every schemaLocation it (transitively) imports, ~70 files:
for i in 1 2 3; do for f in $(grep -aho 'schemaLocation="[^"]*"' *.xsd | sed 's/.*="//;s/"//' | sort -u); do
  [ -f "$f" ] || curl -sSO "https://www.stormware.cz/schema/version_2/$f"; done; done
cd - && POHODA_XSD_DIR=/tmp/pohoda-xsd uv run --with lxml pytest tests/test_accounting_export.py -k xsd
```

On 2026-10-04 the generated files validated against the downloaded XSDs.
This covered the builder test and a real HTTP response from the route,
for all three VAT rates and for CZK and EUR orders. **No import into a
real POHODA installation has been done yet.** Do one trial import
before advertising "works with POHODA".

## Flick E-Invoicing

UAE e-invoicing for ERPNext v15 through Flick (PINT-AE over Peppol).

Flick is the accredited access point: it builds the UBL, reports to the FTA and delivers to the buyer. This app sends invoices to Flick as JSON, follows both tracks (FTA reporting and Peppol delivery) until they finish, and receives the invoices suppliers send you.

### What it covers

Every case in Flick's UAE developer guide, each checked against Flick's own validator on pre-production:

- **Document types:** tax invoices (380) and credit notes (381), and their out-of-scope forms (480, 81). Debit notes go as a tax invoice that references the original, since PINT-AE has no debit note. Self-billed invoices (389) and credit notes (261) come from purchase invoices.
- **Transaction types:** free trade zone, deemed supply, summary invoice, continuous supply, disclosed agent billing, e-commerce, export, and the profit margin scheme (which Flick currently rejects).
- **Tax:** standard, zero-rated, exempt (with reason), out of scope, reverse charge (goods type, GTIN), and foreign currency with the AED totals.
- **Buyers:** Peppol IDs, and the predefined recipients for buyers abroad not on Peppol and for buyers outside UAE e-invoicing.
- **Content:** document-level discounts and charges (a negative discount goes as a charge), price discounts, delivery (address, date, incoterms), every reference field, attachments (embedded, link or reference), payment means by mode of payment, instalments, payee, tax representative, seller overrides, HS and SAC codes, origin, batch and variant attributes.
- **Operations:** live or batch sending (bulk endpoint optional), Check with Flick before submitting, Flick's PDF and UBL attached to the invoice, artifact downloads (FTA tax data document, MLS, signed envelope), Peppol lookup on parties, events feed or webhook, API key or OAuth client.
- **Guidance:** the Flick workspace with a dashboard, a setup checklist per company, and a banner on every invoice saying what e-invoicing will do with it, or did, and what it still lacks.

### Setup

1. Install on a site with ERPNext v15 and a UAE company. ERPNext's UAE regional setup provides the Emirate field on Address; the EDI module provides the code lists used for units of measure.
2. **E Invoicing Settings**, one record per company that issues e-invoices, named after the company. The settings list shows every UAE company with its progress, and each record's form lists the steps it still lacks:
   - Base URL from Flick (pre-production: `https://preprod-ae-api.flickeinvoice.com`), credential, participant ID. **Test Connection** lists the participants the credential covers.
   - Start date, submission mode, default payment means (with IBAN for credit transfer), and the emirate to state on foreign addresses.
   - **Tax Treatments:** map every VAT template, sales, purchase and item, to S, Z, E, O, AE or N. A line's Item Tax Template wins over the invoice's template. An unmapped template stops the submit; there is no default. Every invoice needs a tax template unless each of its lines has an Item Tax Template, and the invoice form marks the field required for companies with e-invoicing on.
   - **Charge Accounts:** map accounts charged "Actual" in the taxes table, such as freight, to a reason code and VAT category.
   - **Reason Code for Negative Discounts:** a negative invoice discount raises the total (to round it up, for instance) and goes on the e-invoice as a charge, which needs a reason code. Agree it with the client; until it is set, such invoices are blocked.
   - Both reason codes are picked from PINT-AE's UNCL 7161 list (type to search, by code or wording); other values are refused.
3. **Units of measure:** the Code List *UN/ECE Recommendation 20* holds every unit code PINT-AE allows. On each code's Common Code, add the ERPNext UOMs it stands for under Applies To. A UOM linked to a code outside that list is refused.
4. **Customers:** Peppol ID (`0235:` plus the 10-digit TIN) or a routing for buyers abroad, TRN in Tax ID, trade licence, and a billing address with its Emirate.
5. **Suppliers:** Peppol ID (matches invoices you receive), Self-Billing Agreement where you issue their invoices.
6. Tick Enabled.

### How it works

- On submit, the invoice is checked and the submit is blocked with a list of everything missing. An **E Invoice Submission** is then created and sent.
- Draft statuses: *Queued*; *Rejected* (Flick refused it and nothing was filed: fix the cause, then Retry); *Error* (no answer, or the credential was refused: retried automatically); *Cancelled*. Once Flick accepts: *Processing*, *Completed*, *Failed*. **Next Action** on each submission says what to do.
- Retries are safe: submissions go with `?no_error`, so Flick answers a number it already holds with that document, and the app adopts it only if it matches what would have been sent.
- **Nothing is ever reissued automatically.** A failed track that is not *settled* is still being retried by Flick. **Resubmit** appears only when both tracks failed for good, which is when Flick releases the number.
- An invoice can be cancelled only before it reached Flick, or after Flick released its number. Otherwise correct it with a credit note.
- A credit note needs a reason and the invoice it corrects; only reason VD (volume discount) may stand alone. A debit note needs the original too. Make a credit note from the original invoice (Create > Return / Credit Note), which fills Return Against: ERPNext keeps that field hidden and read-only on a credit note made by hand. When the original isn't in ERPNext (an invoice from the previous system), give its number and date under Preceding Invoices on the E-Invoicing tab.
- A sales order is referenced only beside the customer's PO number: PINT-AE allows no sales order reference without one.
- Money on the e-invoice has 2 decimals, whatever the site's precision. VAT is rounded half-up, as Flick derives it.

Skipped, with no submission: B2C invoices (customer type Individual, switchable), opening invoices, consolidated POS sales invoices, invoices before the start date, internal customers when that setting is on, and purchase invoices without a self-billing agreement.

### Workspace and guidance

- **Flick workspace:** a dashboard on top. As of now: each UAE company (not set up, setup steps left, or enabled with its environment, when status updates were last read and when an invoice was last sent, flagged when updates are over half an hour late) and the latest submissions that need attention. For the chosen period (7, 30 or 90 days, or all time): the share Flick accepted, submissions by outcome, and how far FTA reporting and Peppol delivery got.
- The dashboard is a Custom HTML Block built from `flick_einvoicing/dashboard/`. Custom HTML Blocks belong to no module, so migrate writes the block from those files each time: change the files, not the block on a site.
- **Setup checklist:** above the E Invoicing Settings list, one row per UAE company with a Set up or Continue button; on each record, the steps still open, such as VAT templates with no tax treatment, and a warning when no sales tax template is Default.
- **Invoice banner:** on Sales, POS and self-billed Purchase Invoices. A draft shows what it lacks as last saved (this app's checks) or that it is ready, with a Check with Flick link that has Flick validate it under a throwaway number (nothing filed); a submitted invoice shows its e-invoice status, next step and, when Flick failed it, the cause; others say why e-invoicing does not apply.

### Code lists

Two lists ship from the [PINT-AE specification's code lists](https://docs.peppol.eu/poac/ae/v1.0.1/pint-ae/trn-invoice/codelist/) (both issued 2025-07-30):

- `flick_einvoicing/einvoice/unit_codes.json`: UN/ECE Recommendation 20 with Recommendation 21 package codes. Every install and migrate loads them into the Code List: new codes are added and changed names updated. A code the specification drops stays on the site, since UOMs may link to it, and invoices using it are refused.
- `flick_einvoicing/einvoice/charge_reason_codes.json`: UNCL 7161 charge reasons, offered in E Invoicing Settings. A code the specification drops stays saved, and invoices using it are refused until another is picked.

When the specification publishes new lists, refresh the files from the bench folder. It prints what changed:

```bash
python apps/flick_einvoicing/flick_einvoicing/einvoice/code_lists.py [units] [charge-reasons]
```

Review the diff, commit and deploy; migrate brings every site up to date. A new version of the specification lives at a new URL: pass its code list folder with `--base-url`.

### Tests

```bash
bench --site <site> run-tests --app flick_einvoicing --skip-before-tests
```

The flow tests need a UAE company keeping its books in AED. Everything they create is rolled back. Flick is faked; no request leaves the machine.

### License

Proprietary. See `license.txt`.

## Flick E-Invoicing

UAE e-invoicing for ERPNext v15 through Flick (PINT-AE over Peppol).

Flick is the accredited access point: it builds the UBL, reports to the FTA and delivers to the buyer. This app sends invoices to Flick as JSON, follows both tracks (FTA reporting and Peppol delivery) until they finish, and receives the invoices suppliers send you.

### What it covers

Every case in Flick's UAE developer guide, each checked against Flick's own validator on pre-production:

- **Document types:** tax invoices (380) and credit notes (381), and their out-of-scope forms (480, 81). Debit notes go as a tax invoice that references the original, since PINT-AE has no debit note. Self-billed invoices (389) and credit notes (261) come from purchase invoices.
- **Transaction types:** free trade zone, deemed supply, summary invoice, continuous supply, disclosed agent billing, e-commerce, export, and the profit margin scheme (which Flick currently rejects).
- **Tax:** standard, zero-rated, exempt (with reason), out of scope, reverse charge (goods type, GTIN), and foreign currency with the AED totals.
- **Buyers:** Peppol IDs, and the predefined recipients for buyers abroad not on Peppol and for buyers outside UAE e-invoicing.
- **Content:** document-level discounts and charges, price discounts, delivery (address, date, incoterms), every reference field, attachments (embedded, link or reference), payment means by mode of payment, instalments, payee, tax representative, seller overrides, HS and SAC codes, origin, batch and variant attributes.
- **Operations:** live or batch sending (bulk endpoint optional), Check with Flick before submitting, Flick's PDF and UBL attached to the invoice, artifact downloads (FTA tax data document, MLS, signed envelope), Peppol lookup on parties, events feed or webhook, API key or OAuth client.

### Setup

1. Install on a site with ERPNext v15 and a UAE company. ERPNext's UAE regional setup provides the Emirate field on Address; the EDI module provides the code lists used for units of measure.
2. **E Invoicing Settings**, one per company:
   - Base URL from Flick (pre-production: `https://preprod-ae-api.flickeinvoice.com`), credential, participant ID. **Test Connection** lists the participants the credential covers.
   - Start date, submission mode, default payment means (with IBAN for credit transfer), and the emirate to state on foreign addresses.
   - **Tax Treatments:** map every VAT template, sales, purchase and item, to S, Z, E, O, AE or N. A line's Item Tax Template wins over the invoice's template. An unmapped template stops the submit; there is no default.
   - **Charge Accounts:** map accounts charged "Actual" in the taxes table, such as freight, to a reason code and VAT category.
3. **Units of measure:** in the Code List *UN/ECE Recommendation 20*, add the ERPNext UOMs each code stands for under Applies To.
4. **Customers:** Peppol ID (`0235:` plus the 10-digit TIN) or a routing for buyers abroad, TRN in Tax ID, trade licence, and a billing address with its Emirate.
5. **Suppliers:** Peppol ID (matches invoices you receive), Self-Billing Agreement where you issue their invoices.
6. Tick Enabled.

### How it works

- On submit, the invoice is checked and the submit is blocked with a list of everything missing. An **E Invoice Submission** is then created and sent.
- Draft statuses: *Queued*; *Rejected* (Flick refused it and nothing was filed: fix the cause, then Retry); *Error* (no answer, or the credential was refused: retried automatically); *Cancelled*. Once Flick accepts: *Processing*, *Completed*, *Failed*. **Next Action** on each submission says what to do.
- Retries are safe: submissions go with `?no_error`, so Flick answers a number it already holds with that document, and the app adopts it only if it matches what would have been sent.
- **Nothing is ever reissued automatically.** A failed track that is not *settled* is still being retried by Flick. **Resubmit** appears only when both tracks failed for good, which is when Flick releases the number.
- An invoice can be cancelled only before it reached Flick, or after Flick released its number. Otherwise correct it with a credit note.
- Money on the e-invoice has 2 decimals, whatever the site's precision. VAT is rounded half-up, as Flick derives it.

Skipped, with no submission: B2C invoices (customer type Individual, switchable), opening invoices, consolidated POS sales invoices, invoices before the start date, internal customers when that setting is on, and purchase invoices without a self-billing agreement.

### Tests

```bash
bench --site <site> run-tests --app flick_einvoicing --skip-before-tests
```

The flow tests need a UAE company keeping its books in AED. Everything they create is rolled back. Flick is faked; no request leaves the machine.

### License

Proprietary. See `license.txt`.

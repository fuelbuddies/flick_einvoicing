## Flick E-Invoicing

UAE e-invoicing for ERPNext v15 through Flick (PINT-AE over Peppol).

Flick is the accredited access point: it builds the UBL, reports to the FTA and delivers to the buyer. This app sends Sales Invoices, POS Invoices and credit notes to Flick as JSON, then follows both tracks (FTA reporting and Peppol delivery) until they finish.

### Setup

1. Install on a site with ERPNext v15 and a UAE company. ERPNext's UAE regional setup provides the Emirate field on Address; the EDI module provides the code lists used for units of measure.
2. **E Invoicing Settings**, one per company:
   - Base URL from Flick (pre-production: `https://preprod-ae-api.flickeinvoice.com`), API key, participant ID. **Test Connection** lists the participants the key covers.
   - Start date: invoices posted earlier are never sent.
   - Submission mode: Live (right after submit) or Batch (every ten minutes).
   - Payment means, with the IBAN for credit transfer.
3. **Tax Treatments**: map every Sales Taxes and Charges Template and Item Tax Template the company invoices with to S, Z, E or O. Exempt needs a reason code. A line's Item Tax Template wins over the invoice's template. An unmapped template stops the submit; there is no default.
4. **Units of measure**: in the Code List *UN/ECE Recommendation 20*, open the Common Code for each unit (H87 piece, C62 unit, KGM, MTR, LTR, DAY) and add the ERPNext UOMs it stands for under Applies To.
5. **Customers**: Peppol ID (`0235:` plus the 10-digit TIN), TRN in Tax ID, and a billing address with its Emirate.
6. Tick Enabled.

### How it works

- On submit, the invoice is checked and the submit is blocked with a list of everything missing. An **E Invoice Submission** is then created and sent.
- Draft statuses: *Queued*; *Rejected* (Flick refused it and nothing was filed: fix the cause, then Retry); *Error* (no answer, or the key was refused: retried automatically); *Cancelled*. Once Flick accepts: *Processing*, *Completed*, *Failed*. **Next Action** on each submission says what to do.
- Statuses arrive from Flick's events feed every five minutes, with a daily re-read of anything unfinished.
- Retries are safe: submissions go with `?no_error`, so Flick answers a number it already holds with that document, and the app adopts it only if it matches what would have been sent.
- **Nothing is ever reissued automatically.** A failed track that is not *settled* is still being retried by Flick. **Resubmit** appears only when both tracks failed for good, which is when Flick releases the number.
- An invoice can be cancelled only before it reached Flick, or after Flick released its number. Otherwise correct it with a credit note.
- Returns need a Credit Note Reason, on the E-Invoicing tab.

Skipped, with no submission: B2C invoices (customer type Individual, switchable), opening invoices, consolidated POS sales invoices, invoices before the start date, and internal customers when that setting is on.

Not supported yet: debit notes, buyers outside the UAE, reverse-charge lines, non-VAT rows in the taxes table (add freight and similar charges as item lines), and company currencies other than AED.

### Tests

```bash
bench --site <site> run-tests --app flick_einvoicing --skip-before-tests
```

The flow tests need a UAE company keeping its books in AED. Everything they create is rolled back. Flick is faked; no request leaves the machine.

### License

Proprietary. See `license.txt`.

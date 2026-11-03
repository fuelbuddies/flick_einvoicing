# ERPNext → Flick (UAE, PINT AE) field mapping

How `flick_einvoicing` turns a Sales Invoice, POS Invoice or self-billed Purchase Invoice into a Flick
Regional API document, and how Flick's statuses come back. Every row is read from the code, not
from intent. Field paths in the first column are Flick's, as its UAE developer guide names them.

- Payload builder: `flick_einvoicing/einvoice/payload.py` (`build_payload` → `_Builder.build`). Code
  lists: `einvoice/codes.py`. Lifecycle: `einvoice/service.py`, `einvoice/status.py`. Inbound:
  `einvoice/inbound.py`, `einvoice/reconcile.py`.
- Money goes as a **string with 2 decimals**: rounded first to the site's `grand_total` precision,
  then **half-up** to fils (`money()` / `_fils()`), whatever the site's precision (tested at 2, 3 and 9 decimals).
  `unit_price`, `gross_price`, `price_discount` go to up to 6 decimals; `quantity` up to 6, trailing
  zeros cut. Credit notes carry **positive** amounts (`abs()`), though ERPNext stores returns negative.
- Empty values (`None`, `""`, `[]`, `{}`) are dropped before sending (`_drop_empty`).
- Flick silently drops unknown keys, so every key the builder may send is listed in
  `ALLOWED_KEYS`; `check_keys()` raises on anything else before the request goes out.
- The same builder runs at invoice submit (`before_submit`), so all problems are listed at once and
  the invoice cannot be submitted until they are fixed. Optional "Validate with Flick before Submit"
  also calls Flick's `/validate` with a throwaway `CHECK-xxxx` identifier.

Notation: `SI` = Sales Invoice / POS Invoice, `PI` = Purchase Invoice (self-billed only),
`Settings` = E Invoicing Settings (one per company). Labels in [brackets].

## 1. Which invoices are sent (`einvoice/scope.py`)

| Condition | Result |
|---|---|
| Settings missing or [Enabled] off | not sent |
| PI with `einvoice_received` set (came in over Peppol) | not sent |
| PI whose Supplier has no `einvoice_self_billing` [Self-Billing Agreement] | not sent |
| `is_opening` = Yes | not sent |
| `posting_date` < Settings.`start_date` [Start Date] | not sent |
| `is_consolidated` (POS consolidation) | not sent |
| [Skip Internal Customers] and `is_internal_customer` / `is_internal_supplier` | not sent |
| [Skip B2C Invoices] (default on) and Customer.`customer_type` = Individual | not sent |
| Company default currency not AED | blocked at submit |

## 2. Document header

| Flick field | ERPNext source | Notes / conditions |
|---|---|---|
| `document_identifier` | `SI/PI.name` | Invoice number as is. |
| `issue_date` | `posting_date` | `YYYY-MM-DD`. |
| `issue_time` | `posting_time` | `HH:MM:SS+04:00`; offset from the site's system timezone. Omitted if it cannot be parsed. |
| `document_type` | derived | See table below. |
| `document_currency` | `currency` | |
| `currency_exchange_rate` | `conversion_rate` | Only when currency ≠ AED; rounded to 6 dp; ≤0 blocks submit. Sent as a number. |
| `due_date` | `due_date`, else `posting_date` | Only on non-credit documents with `payable_amount` > 0. |
| `note` | `remarks` [Remarks] | Dropped if empty or "No Remarks"; cut to 1000 chars. |
| `buyer_reference` | `einvoice_buyer_reference` [Buyer Reference] | |
| `accounting_cost` | `einvoice_buyer_accounting_cost` [Buyer Accounting Reference] | |
| `tax_point_date` | `einvoice_tax_point_date` [Tax Point Date] | Must be before `posting_date` (ibr-141-ae) else blocked. |
| `invoice_period.start_date` | `einvoice_period_start` [Period Start]; else `from_date` when Summary or Continuous | |
| `invoice_period.end_date` | `einvoice_period_end` [Period End]; else `to_date` when Summary or Continuous | End < start blocks (ibr-029). Summary without both blocks (ibr-138-ae). |
| `invoice_period.description_code` | constant `MTH` | Only when [Summary Invoice] ticked. |
| `order_reference.purchase_order_id` | `po_no` [Customer's Purchase Order] | Block sent only when `po_no` is set. |
| `order_reference.sales_order_id` | first `items[].sales_order` | Only beside a PO number (PINT AE rule). |
| `document_references[].document_identifier` | `return_against` | Only on credit notes, or when `is_debit_note`. |
| `document_references[].issue_date` | `posting_date` of the `return_against` invoice | |
| `document_references[]` (extra rows) | `einvoice_preceding_invoices` [Preceding Invoices] → `document_identifier` [Invoice Number], `issue_date` [Issue Date] | Always appended when filled (advance invoices, or originals from another system). |
| `credit_note_reason_code` | `einvoice_credit_note_reason` [Credit Note Reason] | Credit notes only; code part of `DL8.61.1.A`…`E`, `VD`. Required. Anything but `VD` also needs a `document_references` row (ibr-055-ae). |
| `other_references.despatch_document_reference` | `items[].delivery_note` | Unique values joined with ", ". |
| `other_references.receipt_document_reference` | `einvoice_receipt_reference` [Buyer's Goods Receipt] | |
| `other_references.tender_or_lot_reference` | `einvoice_tender_reference` [Tender or Lot] | |
| `other_references.contract_document_reference.id` | `einvoice_contract_reference` [Contract Reference] | |
| `other_references.contract_document_reference.contract_value` | `einvoice_contract_value` [Contract Value] | Text; needs the reference. |
| `other_references.customs_reference_number` | `einvoice_customs_reference` [Customs Declaration Number] | |
| `other_references.project_reference` | `project` [Project] | |
| `fz_beneficiary_id` | `einvoice_fz_beneficiary_id` [Free Zone Beneficiary TRN] | Only when [Free Trade Zone Supply]; 15 digits required. |
| `principal_id` | `einvoice_principal_id` [Principal TRN] | Only when [Disclosed Agent Billing]; 15 digits; must differ from Company.`tax_id` (ibr-176-ae). |
| `tax_totals` | **not sent** | Flick derives the VAT breakdown from lines and allowances/charges. |

`document_type` rules (`_Builder.document_type`). "Out of scope" = every line's category is E or O
(ibr-151-ae).

| Invoice | Normal | All lines E/O |
|---|---|---|
| SI, not return (incl. `is_debit_note`) | `380` | `480` |
| SI, `is_return` | `381` | `81` |
| PI (self-billed), not return | `389` | `480` |
| PI (self-billed), `is_return` | `261` | `81` |

A debit note (`is_debit_note`) is a `380`/`480` tax invoice that must reference the original
(`return_against` or a Preceding Invoices row), as PINT AE has no debit-note type.

## 3. `flags`

All nine keys are always sent (`codes.FLAG_KEYS`), default `false`.

| Flick field | Set to true when |
|---|---|
| `flags.is_selfbilled` | Document is a Purchase Invoice. |
| `flags.is_ftz` | `einvoice_is_ftz` [Free Trade Zone Supply]. Requires `fz_beneficiary_id`. |
| `flags.is_deemed` | `einvoice_is_deemed` [Deemed Supply]. Buyer `peppol_id` becomes `0235:9900000097`; payment means optional. |
| `flags.is_margin` | Any line in category `N`. **Always blocked at submit**: Flick's guide lists margin scheme as a known defect. |
| `flags.is_summary` | `einvoice_is_summary` [Summary Invoice]. Needs period start and end. |
| `flags.is_continuous` | `einvoice_is_continuous` [Continuous Supply]. |
| `flags.is_dab` | `einvoice_is_dab` [Disclosed Agent Billing]. Requires `principal_id`. |
| `flags.is_ecommerce` | `einvoice_is_ecommerce` [E-Commerce Supply]. Requires a delivery address. |
| `flags.is_export` | `einvoice_is_export` [Export]. Requires a delivery address outside the UAE. Auto-ticked on a new SI when Customer routing = "Export buyer not on Peppol" and the shipping (else billing) address country is not UAE (`invoice.default_export`); never unticked. |

## 4. Parties

### `issuing_party` (us)

Flick prefills this block from the onboarded entity profile; `peppol_id` and `legal_name` always
come from Flick's registration. We send only overrides. **Seller TRN, licence and address are not
sent.**

| Flick field | Source |
|---|---|
| `issuing_party.trade_name` | Settings.`seller_trade_name` [Trading Name] |
| `issuing_party.contact_name` | Settings.`seller_contact_name` [Contact Name] |
| `issuing_party.contact_telephone` | Settings.`seller_contact_phone` [Contact Phone] |
| `issuing_party.contact_email` | Settings.`seller_contact_email` [Contact Email] |

On a self-billed PI our company is the buyer issuing the document, so the same block applies.

### `receiving_party`

Self-billed swap: on a PI the receiving party is the **Supplier** (and Supplier address), on an SI
the **Customer**.

| Flick field | SI source | PI (self-billed) source | Notes |
|---|---|---|---|
| `receiving_party.legal_name` | `customer_name`, else Customer.`customer_name` | `supplier_name`, else Supplier.`supplier_name` | |
| `receiving_party.trade_name` | Customer.`einvoice_trade_name` [Trading Name] | not sent | |
| `receiving_party.peppol_id` | see routing below | Supplier.`einvoice_peppol_id` [Peppol ID] | Must match `^0235:1\d{9}$` unless a placeholder. |
| `receiving_party.identifiers.AE:VAT.value` | `tax_id` on invoice, else Customer.`tax_id` | `tax_id` on invoice, else Supplier.`tax_id` | 15 digits. Never sent as `AE:TIN`. Required for any AE (reverse charge) line (ibr-103-ae). |
| `receiving_party.identifiers.AE:TL.value` | Customer.`einvoice_trade_licence` [Trade Licence Number] | Supplier.`einvoice_trade_licence` | Required on `480`/`81` (ibr-136-ae) and on every self-billed doc (ibr-150-ae). |
| `receiving_party.identifiers.AE:TL.authority_name` | Customer.`einvoice_trade_licence_authority` [Licensing Authority] | Supplier, same field | Required with any licence (ibr-101-ae / ibr-172-ae). |
| `receiving_party.party_identifier` | Customer.`einvoice_party_identifier` [Buyer Identifier], else the trade licence number | not sent | |
| `receiving_party.contact_name` | Contact `first_name last_name` of `contact_person` | same | Only if `contact_person` is set. |
| `receiving_party.contact_telephone` | `contact_mobile`, else Contact.`mobile_no`, else `phone` | same | |
| `receiving_party.contact_email` | `contact_email`, else Contact.`email_id` | same | |
| `receiving_party.street_address` | `customer_address` → Address.`address_line1` | `supplier_address` → same | Required. |
| `receiving_party.additional_street_address` | Address.`address_line2` | same | |
| `receiving_party.city_address` | Address.`city` | same | Required. |
| `receiving_party.postal_zone` | Address.`pincode` | same | |
| `receiving_party.emirates_code` | Address.`emirate` → `AUH DXB SHJ AJM UAQ RAK FUJ` | same | Foreign address: Settings.`export_emirate` [Emirate Sent for Addresses Abroad] (ibr-152-ae). |
| `receiving_party.country_code` | Country.`code` of Address.`country`, upper-case | same | Non-AE blocked unless routing is a placeholder or [Export] is ticked. |
| `receiving_party.additional_address_lines` | not sent | not sent | Allowed key, never filled. |

Buyer `peppol_id` routing (SI), from Customer.`einvoice_routing` [E-Invoice Routing]:

| Condition | `peppol_id` sent |
|---|---|
| Invoice [Deemed Supply] ticked (SI or PI) | `0235:9900000097` |
| Routing "Export buyer not on Peppol" | `0235:9900000099` |
| Routing "Not subject to UAE e-invoicing" | `0235:9900000098` |
| Routing "Peppol ID" (default; always on PI) | Customer/Supplier.`einvoice_peppol_id`; missing or wrong format blocks |

With a placeholder `peppol_id`, no TRN, no identifier and not an export, submit is blocked (ibr-135-ae).

## 5. `delivery`

| Flick field | Source | Notes |
|---|---|---|
| `delivery.actual_delivery_date` | `einvoice_delivery_date` [Actual Delivery Date]; else latest of `items[].delivery_date` and linked Delivery Note `posting_date` | |
| `delivery.party_name` | `einvoice_delivery_party` [Received By] | |
| `delivery.location_id` | `einvoice_delivery_location_id` [Delivery Location ID] | |
| `delivery.location_id_scheme` | `einvoice_delivery_location_scheme` [Location ID Scheme] | |
| `delivery.delivery_terms.incoterms` | `incoterm` → Incoterm.`code` | Only `EXW FCA FOB CIF DAP DDP`; others block. |
| `delivery.address.*` | SI `shipping_address_name` / PI `shipping_address` → same address fields as §4 | With [Export] or [E-Commerce]: required, errors block. Otherwise sent only if complete, silently dropped if not. Export with an AE shipping country blocks. |

## 6. `invoice_lines[]` (one per `items` row)

| Flick field | Source | Notes |
|---|---|---|
| `invoice_lines[].id` | `items[].idx` | As string. |
| `invoice_lines[].name` | `item_name` | |
| `invoice_lines[].description` | `description`, HTML stripped, whitespace collapsed, ≤1000; else `item_name` | |
| `invoice_lines[].quantity` | `abs(qty)` | Zero blocks. |
| `invoice_lines[].uom` | `uom` → Code List in Settings.`uom_code_list` [UOM Code List] (ERPNext EDI Common Code "Applies To" UOM), else the list's default code | Must exist in PINT AE unit list (`unit_codes.json`); none or several codes block. |
| `invoice_lines[].unit_price` | line amount ÷ qty | Up to 6 dp. Line amount = `amount` in allowance style, else `net_amount`. |
| `invoice_lines[].gross_price` | `price_list_rate` | Only when > unit price and the invoice is not tax-inclusive and has no spread discount. |
| `invoice_lines[].price_discount` | `gross_price` − `unit_price` | Same condition. |
| `invoice_lines[].line_extension_amount` | `amount` (allowance style) or `net_amount` | 2 dp. |
| `invoice_lines[].tax_category` | Tax Treatment row → [Tax Category] | Lookup: line `item_tax_template` (type Item Tax Template) first, else invoice `taxes_and_charges` (Sales/Purchase Taxes and Charges Template). No row blocks. |
| `invoice_lines[].tax_percentage` | constant | `5` for S and AE; `0` for Z, E, O; not sent for N. ERPNext's effective rate must be 5 for S, 0 for Z/E/O/AE, else blocked. |
| `invoice_lines[].tax_exemption_reason_code` | Tax Treatment [Exemption Reason] (`DL8.46.1`–`DL8.46.4`) | Category E only; required. |
| `invoice_lines[].tax_exemption_reason` | Tax Treatment [Exemption Reason Text] | Category E only. |
| `invoice_lines[].reverse_charge_item_type` | Tax Treatment [Reverse-Charge Goods] (`DL8.48.*`) | Category AE only; required (ibr-166-ae). |
| `invoice_lines[].supplier_item_code` | SI: `item_code`. PI: Item Supplier.`supplier_part_no` for this supplier | |
| `invoice_lines[].buyers_item_code` | SI: `customer_item_code`. PI: `item_code` | |
| `invoice_lines[].standard_item_id` | first Item.`barcodes` row of type EAN, EAN-8/12/13, GTIN, JAN, UPC, UPC-A/E | Required on AE lines (ibr-174-ae). |
| `invoice_lines[].standard_item_id_scheme` | constant `0160` | With the GTIN. |
| `invoice_lines[].origin_country` | Item.`country_of_origin` → Country.`code` | |
| `invoice_lines[].hs_code` | Item.`customs_tariff_number` | |
| `invoice_lines[].sac_code` | Item.`einvoice_sac_code` [Service Accounting Code (SAC)] | |
| `invoice_lines[].commodity_code` | derived | `G` HS only, `S` SAC only, `B` both; not sent if neither. |
| `invoice_lines[].item_attributes` | Item.`attributes` (variants only) | `{attribute: value}`. |
| `invoice_lines[].batch_number` | `batch_no` | |
| `invoice_lines[].invoice_period_start_date` / `_end_date` | `service_start_date` / `service_end_date` | Only when both set. |
| `invoice_lines[].line_amount_aed` | `base_amount` (allowance style) or `base_net_amount` | Only when currency ≠ AED. |
| `invoice_lines[].tax_line_amount`, `tax_line_amount_aed`, `base_quantity`, line `allowance_charges` | not sent | |

## 7. `allowance_charges[]` (document level)

Allowance style = invoice has `discount_amount` ≠ 0, no tax row `included_in_print_rate`, and
Settings.`discount_style` [Invoice Discount] ≠ "Spread over lines". Otherwise the discount stays
inside `net_amount` per line and no allowance is sent.

| Source | `charge_indicator` | `reason_code` | `reason` | `amount` | `tax_category` / `tax_percentage` |
|---|---|---|---|---|---|
| Invoice discount, per VAT category (Σ `amount − net_amount` of lines), when positive | `false` | `95` | `Discount` | category sum | that category; `5`/`0`/none per §6 rule (E, O: not sent) |
| Same, when negative (raises total) | `true` | Settings.`negative_discount_reason_code` [Reason Code for Negative Discounts] (default `AJ`) | Settings.`negative_discount_reason` (default `Adjustment`) | absolute value | same |
| Tax row whose `account_head` is in Settings [Charge Accounts] | `true` | Charge Account [Reason Code] (must be in UNCL 7161 list) | [Reason], else tax row `description` | `tax_amount` | Charge Account [VAT Category] (S/Z/E/O) |

- A mapped charge row must be charge type "Actual"; any other non-VAT charge type blocks.
- A charge/allowance category that no line has blocks (Flick would have no breakdown row for it).
- `percentage` and `base_amount` are allowed keys but **not sent**.
- Self-billed PI: tax rows with category "Valuation" are skipped; "Deduct" rows block.

## 8. `invoice_totals`

All stated by us (Flick would otherwise compute them); our checks mirror Flick's ibr-co-10…16.

| Flick field | Value | Notes |
|---|---|---|
| `invoice_totals.line_extension_amount` | Σ line `line_extension_amount` | |
| `invoice_totals.allowance_total_amount` | Σ allowances | Only if any. |
| `invoice_totals.charge_total_amount` | Σ charges (incl. negative-discount charges) | Only if any. |
| `invoice_totals.tax_exclusive_amount` | lines − allowances + charges | |
| `invoice_totals.tax_amount` | 5% × (S lines − S allowances + S charges), half-up | Must be within 0.01 of ERPNext VAT (`total_taxes_and_charges` − charge rows), else blocked. For margin docs, ERPNext's figure. |
| `invoice_totals.tax_inclusive_amount` | tax_exclusive + tax_amount | Must match `grand_total` (tolerance 0.01, plus 0.01/line on 3-dp sites). |
| `invoice_totals.payable_amount` | `rounded_total` if set and rounding not disabled, else `grand_total` | |
| `invoice_totals.payable_rounding_amount` | payable − tax_inclusive | Only when non-zero. |
| `invoice_totals.total_tax_amount_in_aed` | `base_total_taxes_and_charges` − Σ charge rows' `base_tax_amount` | Only when currency ≠ AED. |
| `invoice_totals.total_amount_with_tax_in_aed` | `base_grand_total` | Only when currency ≠ AED. |
| `invoice_totals.prepaid_amount` | not sent | Advances are not mapped. |

## 9. Payment

### `payment_means[]`

| Case | Entries |
|---|---|
| SI/POS with `payments` rows of non-zero amount | One per row: [Payment Means by Mode of Payment] code for its `mode_of_payment`, else [Default Payment Means]; duplicates removed. |
| Otherwise (incl. all self-billed PIs) | One entry with [Default Payment Means]. None set: blocked, except on credit notes and deemed supplies (ibr-191-ae), where it's just omitted. |

| Flick field | Source |
|---|---|
| `payment_means[].type_code` | as above (`1 10 20 21 30 49 54 55 68`) |
| `payment_means[].payment_id` | invoice `name` |
| `payment_means[].payment_account_id` | Settings.`payment_account_id` [IBAN / Account Number]; code `30` only, required (ibr-192-ae) |
| `payment_means[].payment_account_name` | Settings.`payment_account_name` [Account Name]; code `30` only |
| `payment_means[].payment_service_provider_id` | Settings.`payment_service_provider_id` [Bank BIC]; code `30` only |
| `payment_means[].mandate.id` | Customer.`einvoice_mandate_id` [Direct Debit Mandate]; code `49`, SI only |
| `payment_means[].mandate.debited_account_id` | Customer.`einvoice_mandate_account` [Debited Account (IBAN)]; code `49`, SI only |

### `payment_terms[]` (not sent on credit notes)

| Flick field | Source |
|---|---|
| `payment_terms[].note` | `payment_schedule[].description`, else `payment_term` |
| `payment_terms[].amount` | `payment_schedule[].payment_amount` |
| `payment_terms[].installment_due_date` | `payment_schedule[].due_date` |
| `payment_terms[].payment_instruction_id` | not sent |

### `payee_party` (Settings only; name required if any field is set, ibr-017)

| Flick field | Source |
|---|---|
| `payee_party.name` | `payee_name` [Payee Name] |
| `payee_party.identifier.value` / `.scheme_id` | `payee_identifier` [Payee Identifier] / `payee_identifier_scheme` [Identifier Scheme] |
| `payee_party.legal_registration_id.value` | `payee_legal_registration_id` [Payee Legal Registration ID] |
| `payee_party.bank_creditor_id` | `payee_bank_creditor_id` [Payee Creditor ID] |

### `tax_representative` (Settings only; sent if any of name/TRN/address set, then all three required)

| Flick field | Source |
|---|---|
| `tax_representative.name` | `tax_rep_name` [Representative Name] |
| `tax_representative.vat_number` | `tax_rep_vat_number` [Representative TRN], 15 digits |
| `tax_representative.street_address` … `country_code` | `tax_rep_address` [Representative Address] → Address, as §4 (foreign allowed) |

## 10. Attachments: `additional_document_references[]`

From `einvoice_documents` [Supporting Documents] (child E Invoice Supporting Document). Rows without
[Document Number] block (Flick would drop them silently).

| Flick field | Source | When |
|---|---|---|
| `additional_document_references[].document_identifier` | [Document Number] | always |
| `additional_document_references[].document_description` | [Description] | always |
| `additional_document_references[].attachment_uri` | [URL] | Kind = Link (required) |
| `additional_document_references[].document_type_code` | `130` if [Invoiced Object] | Kind = Reference |
| `additional_document_references[].scheme_id` | [Identifier Scheme] | Kind = Reference |
| `additional_document_references[].embedded_document.mime_code` | guessed from file name | Kind = Attachment |
| `additional_document_references[].embedded_document.filename` | File.`file_name` | Kind = Attachment |
| `additional_document_references[].embedded_document.content` | base64 of file | ≤ 20 MB; only PDF, PNG, JPEG, CSV, XLSX, ODS, XML |

Back from Flick: after acceptance, Flick's PDF (Settings [Attach Flick's PDF to the Invoice], default
on) and UBL XML ([Attach the UBL XML to the Invoice]) are attached privately to the invoice as
`<number> e-invoice.pdf/.xml`; `files_attached` is set. Failed attaches are retried by the daily job.

## 11. Settings that change the mapping

| Settings field [label] | Effect |
|---|---|
| `tax_treatments` [Tax Treatments] (Template Type, Template, Tax Category, Exemption Reason, Exemption Reason Text, Reverse-Charge Goods) | Line `tax_category`, exemption code/text, reverse-charge type; drives `document_type` 480/81. |
| `charge_accounts` [Charge Accounts] (Account, Reason Code, Reason, VAT Category) | Tax rows on these accounts become document charges instead of VAT. |
| `discount_style` [Invoice Discount] | "Document-level allowance" (default) vs "Spread over lines". |
| `negative_discount_reason_code` / `negative_discount_reason` | Charge code/reason for a negative invoice discount. |
| `export_emirate` [Emirate Sent for Addresses Abroad] | `emirates_code` on any foreign address. |
| `uom_code_list` [UOM Code List] (default UN/ECE Recommendation 20) | UOM → unit code. |
| `payment_means_code` [Default Payment Means], `payment_modes` [Payment Means by Mode of Payment] | `payment_means[].type_code`. |
| `payment_account_id`, `payment_account_name`, `payment_service_provider_id` | Credit-transfer account details. |
| `seller_trade_name`, `seller_contact_*` | `issuing_party`. |
| `payee_*`, `tax_rep_*` | `payee_party`, `tax_representative`. |
| `start_date`, `skip_b2c`, `skip_internal_customers` | Scope (§1). |
| `submission_mode` [Submission Mode] Live/Batch, `batch_interval`, `use_bulk` | When/how sent (Live: queued job right after submit; Batch: every N minutes; bulk endpoint up to 50/request). |
| `validate_before_submit` | Also run Flick `/validate` at submit. |
| `attach_pdf`, `attach_xml` | Files pulled back after acceptance. |
| `status_updates` [Status Updates From] Events Feed/Webhook, `ack_events` | How statuses return. |
| `receive_invoices`, `auto_create_purchase_invoice`, `purchase_item`, `purchase_taxes_template` | Inbound (§13). |

## 12. Statuses

### Submission flow

Invoice submit → E Invoice Submission created (`Queued`, draft). It is submitted (docstatus 1)
only once Flick accepts. The invoice's `einvoice_status` [E-Invoice Status] and
`einvoice_submission` always mirror the submission's `status` (`service.mirror`).

| Flick answer to POST | Our `status` | Meaning | User action |
|---|---|---|---|
| (before send) | `Queued` | Waiting for the job | none |
| our own checks fail at send time | `Rejected` | Not sent; problems in [Validation Errors] | fix, Retry |
| HTTP 400 (schema/Schematron issues) | `Rejected` | Refused, nothing filed; issues as `path: message (code)` | fix, Retry |
| bulk `outcome: rejected` | `Rejected` | same | fix, Retry |
| 202, or 200 with `document_id` | `Processing` / `Completed` / `Failed` from tracks | Accepted; submission submitted, `document_id` stored | none |
| 409 with `document_id`, `is_duplicate`, bulk `duplicate` | adopted as accepted | Number already on Flick; compared on buyer `peppol_id`, type, date, currency, payable. Any difference → `Rejected` | if rejected, ask Flick support |
| 401 / 403 | `Error` | Credential refused; retry in 60 min; company's batch stops | fix credentials |
| 429 | `Error` | Rate limit; retry after `Retry-After` (default 60 s) | none |
| 500 with retention-rule message | lookup by number: adopt if found, else `Rejected` | Flick known defect: "already filed, do not retry" | renumber or ask Flick |
| no answer / 5xx / bulk `failed`/`not_processed` | `Error` | Unknown if it landed; retried (next POST adopts it if it did) | none unless it persists |
| invoice cancelled while still a draft | `Cancelled` | Never filed | none |

Retry backoff for `Error` (`BACKOFF_MINUTES`): 5, 15, 60, 180, 360, then every 360 min, by
attempt count. Due drafts are picked up by the every-minute job (`jobs.send_queued`).
Bulk unread copy / lookup failures retry in 15 min.

### Tracks after acceptance

Two Flick tracks are stored: `reporting_status` [Reporting] (FTA) and `exchange_status` [Delivery]
(Peppol), each with a `settled` flag from the single-document read.

| Flick value | Track | Combined `status` |
|---|---|---|
| `failed` on either track | either | `Failed` |
| reporting `reported`/`not_required` **and** exchange `delivered`/`received`/`not_required` | both | `Completed` |
| anything else (`processing`, one done, `cancelled`) | | `Processing` |

| Situation | `status` | Next action shown |
|---|---|---|
| reporting `reported`, exchange in progress | Processing | Reported to FTA, waiting for delivery |
| exchange `delivered`, reporting in progress | Processing | Delivered, waiting for FTA |
| a track `failed`, `settled: false` | Failed | Flick still retrying; do not reissue |
| both tracks `failed` and both settled | Failed | Number released: fix, then **Resubmit** (new submission, same number). If error `origin` is `core`/`region`, confirm with Flick first |
| reported, delivery failed for good | Failed | Do not reissue; sort out delivery, credit note if needed |
| delivered, reporting failed for good | Failed | Review with Flick support; do not reissue |
| error code `RECIPIENT_NOT_ON_NETWORK` | Failed | Correct the Peppol ID on the customer/supplier |

Where track updates come from:

| Source | Event → effect |
|---|---|
| Events feed (every 5 min, `jobs.sync_events`), cursor in Settings, acked if [Acknowledge Events] | `reporting.status_changed` → reporting; `exchange.status_changed` → exchange; `document.completed` → both; `document.failed` → full detail read. Other types ignored. |
| Webhook | `document.reporting.reported/failed`, `document.exchange.delivered/failed`, `document.completed`, `document.failed` mapped to the feed types above; `document.received` → inbound. |
| Detail read (`GET` document) | After any failure, after duplicates/non-Processing accepts, and daily for submissions still Processing/Failed 24 h after last change. Sets `settled` flags and [Error] text. |

Rules: a finished track (`reported`/`not_required`, `delivered`/`received`/`not_required`) never
moves back; events older than `last_event_at` are ignored; failures are only trusted from the
detail read.

Cancelling the ERPNext invoice is allowed only while the submission is a never-sent draft
(`Queued`, `Rejected`, `Error` with 0 attempts), or when Flick released the number. Otherwise:
issue a credit note.

## 13. Received invoices (inbound)

When Settings [Receive Invoices] is on, incoming `document.received` events (feed `incoming`
direction or webhook) fetch the document and create **E Invoice Received**. Flick's PDF and XML
are attached to it. A failed fetch keeps the feed cursor on that event for the next run.

### Flick → E Invoice Received

| Flick field | E Invoice Received field [label] | Notes |
|---|---|---|
| `document_id` | `document_id` [Flick Document ID] (also the record name) | |
| `document_identifier` (top level, else `document.document_identifier`) | `document_identifier` [Supplier's Invoice Number] | |
| `document.document_type`, else top-level `type` | `document_type` [Document Type] | |
| `document.issue_date` | `issue_date` [Issue Date] | |
| `counterparty.name`, else `document.issuing_party.legal_name` | `sender_name` [Sender Name] | Cut to 140 chars. |
| `counterparty.peppol_id`, else `issuing_party.peppol_id` | `sender_peppol_id` [Sender Peppol ID] | |
| `issuing_party.identifiers.AE:VAT.value`, else `counterparty.vat_number` | `sender_vat` [Sender TRN] | |
| (match) | `supplier` [Supplier] | Supplier with `einvoice_peppol_id` = sender Peppol ID, else `tax_id` = sender TRN. Re-matched when a Supplier's Peppol ID/TRN changes. |
| `document.document_currency` | `currency` | |
| `invoice_totals.payable_amount`, else `tax_inclusive_amount` | `total_amount` [Total] | |
| `invoice_totals.tax_amount` | `tax_amount` [VAT] | |
| whole `document` | `document_json` [Document] | |
| — | `status` New / Purchase Invoice Created / Ignored | |

### E Invoice Received → draft Purchase Invoice

Made by "Create Purchase Invoice", or automatically if [Create Draft Purchase Invoices] and a
supplier matched. Never submitted by the app.

| PI field | Source |
|---|---|
| `supplier` | record `supplier` (required) |
| `bill_no` / `bill_date` | `document_identifier` / `issue_date` |
| `posting_date` | today (`set_posting_time` = 1) |
| `currency` | `document_currency` |
| `conversion_rate` | `currency_exchange_rate`; keeps a linked Purchase Receipt's rate unless Buying Settings "Set Landed Cost Based on PI Rate" |
| `due_date` | `due_date` (not on returns); clears PO payment terms |
| `is_return` / `return_against` | type `381`/`81`/`261`; original = submitted PI of this supplier with `bill_no` = `document_references[].document_identifier` |
| source documents | Purchase Receipt from `other_references.receipt_document_reference`, or with `supplier_delivery_note` in `despatch_document_reference`; Purchase Order from `order_reference.purchase_order_id` or lines' `order_reference_id` (non-returns only) |
| `items[].item_code` | match: Item Supplier `supplier_part_no` = `supplier_item_code`, else Item Barcode = `standard_item_id`, else Item = `buyers_item_code`; else Settings [Item for Unmatched Lines] or app item `E-INVOICE-UNMATCHED` (blocks PI submit), else no item (expense account) |
| `items[].item_name` / `description` | `name` / `description` |
| `items[].qty` | `quantity` (negative on returns) |
| `items[].rate` | line amount ÷ qty (`reconcile.unit_rate`, incl. line allowances/charges and `base_quantity`) |
| `items[].uom` | `uom` code reverse-looked-up in [UOM Code List] |
| `items[].einvoice_supplier_item_code` [Supplier's Item Code] | `supplier_item_code` |
| `items[].einvoice_line_no` [E-Invoice Line] | line position |
| `items[].item_tax_template` | for non-`S` `tax_category`: first Item Tax Template row in Tax Treatments with that category |
| `taxes_and_charges` | Settings [Purchase Taxes for Standard-Rated Lines], else company default template, if any line is `S` and the source brought no taxes |
| `einvoice_received` [Received E-Invoice] | record name |
| totals | `reconcile.apply` adjusts the draft to the supplier's totals (rounding, invoice-level allowances/charges); differences listed in record `notes` |

On submitting such a PI, the app remembers supplier item codes in Item Supplier and fills the
supplier's Peppol ID if empty.

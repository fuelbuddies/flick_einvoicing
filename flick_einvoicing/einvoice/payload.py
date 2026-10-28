"""ERPNext invoice -> Flick UAE document.

One code path builds the payload and decides what is wrong with an invoice, so the check at
submit and the real send can never disagree. Problems are collected, not raised one at a
time, so the user fixes everything in one pass.

Amounts go as decimal strings (Flick accepts strings or numbers; strings keep the scale).
Credit notes carry positive amounts, though ERPNext stores returns as negatives. Lines use
ERPNext's net amounts, which already have the invoice discount spread over them and
inclusive tax taken out, so no document-level allowances are needed.
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import cstr, flt, getdate, strip_html

from flick_einvoicing.einvoice import codes


class InvoiceProblems(frappe.ValidationError):
	def __init__(self, problems: list[str]):
		self.problems = problems
		super().__init__("\n".join(problems))


def build_payload(invoice, settings) -> dict:
	builder = _Builder(invoice, settings)
	payload = builder.build()
	if builder.problems:
		raise InvoiceProblems(list(builder.problems))
	check_keys(payload)
	return payload


def invoice_problems(invoice, settings) -> list[str]:
	try:
		build_payload(invoice, settings)
	except InvoiceProblems as e:
		return e.problems
	return []


class _Builder:
	def __init__(self, invoice, settings):
		self.inv = invoice
		self.settings = settings
		self.problems: dict[str, None] = {}  # ordered, without repeats
		self.precision = invoice.precision("grand_total") or 2
		self.is_return = bool(invoice.get("is_return"))
		self.treatments = {(row.template_type, row.template): row for row in settings.tax_treatments}
		self._uom_codes: dict[str, str | None] = {}

	def problem(self, text: str):
		self.problems[text] = None

	def build(self) -> dict:
		inv = self.inv
		if inv.get("is_debit_note"):
			self.problem(
				"Debit notes can't be e-invoiced yet. Issue a new invoice for the additional amount instead."
			)

		company_currency = frappe.get_cached_value("Company", inv.company, "default_currency")
		if company_currency != "AED":
			self.problem(
				f"Company {inv.company} keeps its books in {company_currency}; UAE e-invoicing needs AED."
			)

		self.check_tax_rows()
		lines = [self.line(item) for item in inv.items]

		doc = {
			"document_identifier": inv.name,
			"issue_date": str(getdate(inv.posting_date)),
			"document_type": self.document_type(lines),
			"document_currency": inv.currency,
			"flags": {key: False for key in codes.FLAG_KEYS},
			"receiving_party": self.buyer(),
			"payment_means": self.payment_means(),
			"invoice_lines": [line for line, _category in lines],
			"invoice_totals": self.totals(lines),
		}

		if self.is_return:
			self.credit_note(doc)
		else:
			doc["due_date"] = str(getdate(inv.due_date or inv.posting_date))

		if inv.currency != "AED":
			rate = flt(inv.conversion_rate, 6)
			if rate <= 0:
				self.problem("The invoice has no exchange rate to AED.")
			doc["currency_exchange_rate"] = rate

		if inv.get("po_no"):
			doc["order_reference"] = {"purchase_order_id": cstr(inv.po_no)}

		return _drop_empty(doc)

	# Lines and tax

	def check_tax_rows(self):
		for tax in self.inv.taxes:
			if tax.charge_type != "On Net Total":
				self.problem(
					f"Tax row {tax.idx} ({tax.description or tax.account_head}) is charged '{tax.charge_type}'. "
					"Only 'On Net Total' VAT rows can be e-invoiced for now; add freight or other charges as item lines."
				)

	def line(self, item) -> tuple[dict, str | None]:
		label = f"Row {item.idx} ({item.item_code or item.item_name})"
		treatment = self.treatment_for(item, label)
		category = codes.code_of(treatment.tax_category) if treatment else None

		qty = abs(flt(item.qty))
		net = abs(flt(item.net_amount, self.precision))
		if not qty:
			self.problem(f"{label}: quantity is zero.")

		rate = self.line_rate(item)
		if category == "S" and flt(rate, 6) != codes.STANDARD_RATE:
			self.problem(
				f"{label} is mapped as standard rated, but ERPNext charges {rate:g}% on it. "
				"Standard-rated lines must carry exactly 5% VAT."
			)
		elif category in ("Z", "E", "O") and flt(rate, 6) != 0:
			self.problem(
				f"{label} is mapped as {treatment.tax_category}, but ERPNext charges {rate:g}% VAT on it."
			)
		if category == "S" and (item.get("is_zero_rated") or item.get("is_exempt")):
			self.problem(
				f"{label}: the item is flagged zero-rated or exempt, but its tax template is mapped as standard rated."
			)

		line = {
			"id": str(item.idx),
			"name": cstr(item.item_name),
			"description": _plain(item.description) or cstr(item.item_name),
			"quantity": _quantity(qty),
			"uom": self.uom_code(item.uom, label),
			"unit_price": _unit_price(net, qty) if qty else None,
			"line_extension_amount": self.money(net),
			"tax_category": category,
			"tax_percentage": str(codes.STANDARD_RATE) if category == "S" else "0",
			"supplier_item_code": cstr(item.item_code) or None,
		}
		if category == "E":
			line["tax_exemption_reason_code"] = codes.code_of(treatment.exemption_reason_code)
			line["tax_exemption_reason"] = cstr(treatment.exemption_reason_text) or None
			if not line["tax_exemption_reason_code"]:
				self.problem(
					f"Tax treatment for {treatment.template} is Exempt but has no exemption reason code."
				)
		return _drop_empty(line), category

	def treatment_for(self, item, label):
		if item.item_tax_template:
			# A line-level template overrides the invoice's rates, so never fall back past it.
			row = self.treatments.get(("Item Tax Template", item.item_tax_template))
			if not row:
				self.problem(
					f"{label}: Item Tax Template {item.item_tax_template} has no tax treatment in E Invoicing Settings."
				)
			return row
		template = self.inv.taxes_and_charges
		if template:
			row = self.treatments.get(("Sales Taxes and Charges Template", template))
			if not row:
				self.problem(
					f"Sales Taxes and Charges Template {template} has no tax treatment in E Invoicing Settings."
				)
			return row
		self.problem(
			f"{label}: neither the invoice nor the line has a tax template, so its VAT treatment is unknown."
		)
		return None

	def line_rate(self, item) -> float:
		"""The VAT rate ERPNext applied to this line, worked out the way ERPNext does."""
		try:
			item_rates = json.loads(item.item_tax_rate) if item.item_tax_rate else {}
		except (TypeError, ValueError):
			item_rates = {}
		rate = 0.0
		for tax in self.inv.taxes:
			if tax.charge_type != "On Net Total":
				continue
			rate += flt(item_rates[tax.account_head]) if tax.account_head in item_rates else flt(tax.rate)
		return rate

	def uom_code(self, uom: str, label: str) -> str | None:
		if uom in self._uom_codes:
			return self._uom_codes[uom]
		self._uom_codes[uom] = code = self._resolve_uom(uom, label)
		return code

	def _resolve_uom(self, uom: str, label: str) -> str | None:
		code_list = self.settings.uom_code_list
		if not code_list:
			self.problem("Set the UOM Code List in E Invoicing Settings.")
			return None
		try:
			from erpnext.edi.doctype.code_list.code_list import get_codes_for, get_default_code
		except ImportError:
			self.problem(
				"This ERPNext has no EDI code lists, which UOM codes are read from. Update ERPNext v15."
			)
			return None
		found = get_codes_for(code_list, "UOM", uom)
		if len(found) == 1:
			return found[0]
		if len(found) > 1:
			self.problem(f"UOM {uom} is linked to more than one code in {code_list}: {', '.join(found)}.")
			return None
		default = get_default_code(code_list)
		if default:
			return default
		self.problem(
			f"{label}: UOM {uom} has no code in Code List {code_list}. Open the matching Common Code "
			f"(for example H87 piece or C62 unit) and add UOM {uom} under Applies To."
		)
		return None

	def document_type(self, lines) -> str:
		categories = {category for _line, category in lines if category}
		only_exempt_or_out = bool(categories) and categories <= codes.EXEMPT_OR_OUT_OF_SCOPE
		# ibr-151-ae: a 380/381 made only of E and O lines must be a 480/81.
		if self.is_return:
			return codes.CREDIT_NOTE_OUT_OF_SCOPE if only_exempt_or_out else codes.CREDIT_NOTE
		return codes.INVOICE_OUT_OF_SCOPE if only_exempt_or_out else codes.INVOICE

	# Parties and payment

	def buyer(self) -> dict:
		inv = self.inv
		customer = frappe.get_cached_doc("Customer", inv.customer)
		peppol_id = cstr(customer.get("einvoice_peppol_id")).strip()
		if not peppol_id:
			self.problem(f"Customer {inv.customer} has no Peppol ID.")
		elif not re.match(codes.PEPPOL_ID_PATTERN, peppol_id):
			self.problem(f"Customer {inv.customer}: Peppol ID {peppol_id} should look like 0235:1234567890.")

		trn = cstr(inv.get("tax_id") or customer.tax_id).strip()
		if trn and not re.match(codes.TRN_PATTERN, trn):
			self.problem(f"Customer {inv.customer}: TRN {trn} should be 15 digits.")

		party = {
			"legal_name": cstr(inv.customer_name or customer.customer_name),
			"peppol_id": peppol_id,
			# AE:VAT only; Flick rejects a buyer AE:TIN in every case.
			"identifiers": {"AE:VAT": {"value": trn}} if trn else None,
		}

		if not inv.customer_address:
			self.problem("The invoice has no customer address; the buyer's address is required.")
			return _drop_empty(party)

		address = frappe.get_cached_doc("Address", inv.customer_address)
		for fieldname, label in (
			("address_line1", "Address Line 1"),
			("city", "City/Town"),
			("country", "Country"),
		):
			if not address.get(fieldname):
				self.problem(f"Customer address {address.name} has no {label}.")

		country_code = (
			cstr(frappe.get_cached_value("Country", address.country, "code")).upper()
			if address.country
			else ""
		)
		if country_code and country_code != "AE":
			self.problem(
				f"Customer address {address.name} is outside the UAE. Buyers abroad are not supported yet."
			)

		party.update(
			{
				"street_address": cstr(address.address_line1),
				"additional_street_address": cstr(address.address_line2) or None,
				"city_address": cstr(address.city),
				"postal_zone": cstr(address.pincode) or None,
				"emirates_code": self.emirate(address),
				"country_code": country_code or None,
			}
		)
		return _drop_empty(party)

	def emirate(self, address) -> str | None:
		if not frappe.get_meta("Address").has_field("emirate"):
			self.problem(
				"Address has no Emirate field. ERPNext adds it with its UAE regional setup for a UAE company."
			)
			return None
		name = address.get("emirate")
		if not name:
			self.problem(f"Customer address {address.name} has no Emirate.")
			return None
		code = codes.EMIRATE_CODES.get(name)
		if not code:
			self.problem(f"Customer address {address.name}: unknown emirate {name}.")
		return code

	def payment_means(self) -> list[dict] | None:
		code = codes.code_of(self.settings.payment_means_code)
		if not code:
			if not self.is_return:  # ibr-191-ae spares credit notes only
				self.problem("Set the default Payment Means in E Invoicing Settings.")
			return None
		entry = {
			"type_code": code,
			"payment_id": self.inv.name,
			"payment_account_id": cstr(self.settings.payment_account_id).strip() or None,
			"payment_account_name": cstr(self.settings.payment_account_name).strip() or None,
			"payment_service_provider_id": cstr(self.settings.payment_service_provider_id).strip() or None,
		}
		if code in codes.PAYMENT_MEANS_NEEDING_ACCOUNT and not entry["payment_account_id"]:
			self.problem("Payment means 30 (credit transfer) needs the IBAN in E Invoicing Settings.")
		return [_drop_empty(entry)]

	def credit_note(self, doc: dict):
		inv = self.inv
		reason = codes.code_of(inv.get("einvoice_credit_note_reason"))
		if not reason:
			self.problem("Choose a Credit Note Reason on the E-Invoicing tab.")
		doc["credit_note_reason_code"] = reason
		if inv.return_against:
			original_date = frappe.db.get_value(inv.doctype, inv.return_against, "posting_date")
			doc["document_references"] = [
				_drop_empty(
					{
						"document_identifier": inv.return_against,
						"issue_date": str(original_date) if original_date else None,
					}
				)
			]
		elif reason and reason != codes.REASON_WITHOUT_REFERENCE:
			# ibr-055-ae: only a volume-discount credit note may stand without the original.
			self.problem(
				"This credit note isn't linked to an original invoice (Return Against). Only reason VD may stand alone."
			)

	# Totals

	def money(self, value) -> str:
		return f"{abs(flt(value, self.precision)):.{self.precision}f}"

	def totals(self, lines) -> dict:
		"""Stated totals, so the e-invoice matches ERPNext to the fils.

		Flick enforces its arithmetic on stated totals, so the same rules run here first
		(ibr-co-10 to ibr-co-16, aligned-ibrp-s-09). A mismatch stops the submit, rather than
		coming back from Flick as a rejection after the invoice is booked.
		"""
		inv = self.inv
		p = self.precision
		dec = lambda value: Decimal(self.money(value))  # noqa: E731

		net_total = dec(inv.net_total)
		tax_total = dec(inv.total_taxes_and_charges)
		grand_total = dec(inv.grand_total)
		rounded = flt(inv.get("rounded_total")) and not inv.get("disable_rounded_total")
		payable = dec(inv.rounded_total) if rounded else grand_total

		line_sum = sum(
			(Decimal(line["line_extension_amount"]) for line, _c in lines if "line_extension_amount" in line),
			Decimal(0),
		)
		if line_sum != net_total:
			self.problem(f"Line amounts add up to {line_sum} but the invoice net total is {net_total}.")
		if net_total + tax_total != grand_total:
			self.problem(
				f"Net total {net_total} plus VAT {tax_total} does not equal the grand total {grand_total}."
			)

		standard = sum((Decimal(line["line_extension_amount"]) for line, c in lines if c == "S"), Decimal(0))
		expected_tax = Decimal(f"{flt(flt(standard) * codes.STANDARD_RATE / 100, p):.{p}f}")
		if expected_tax != tax_total:
			self.problem(
				f"VAT on the invoice is {tax_total}, but 5% of the standard-rated lines ({standard}) is {expected_tax}. "
				"Check that only VAT is charged in the taxes table and that the rows are mapped correctly."
			)

		totals = {
			"line_extension_amount": self.money(net_total),
			"tax_exclusive_amount": self.money(net_total),
			"tax_amount": self.money(tax_total),
			"tax_inclusive_amount": self.money(grand_total),
			"payable_amount": self.money(payable),
		}
		if payable != grand_total:
			totals["payable_rounding_amount"] = f"{payable - grand_total:.{p}f}"
		if inv.currency != "AED":
			# Required on foreign-currency documents and never derived by Flick.
			totals["total_tax_amount_in_aed"] = self.money(inv.base_total_taxes_and_charges)
			totals["total_amount_with_tax_in_aed"] = self.money(inv.base_grand_total)
		return totals


# Formatting


def _quantity(qty: float) -> str:
	text = f"{Decimal(str(qty)).quantize(Decimal('0.000001'), ROUND_HALF_UP):f}"
	return text.rstrip("0").rstrip(".") if "." in text else text


def _unit_price(net: float, qty: float) -> str:
	"""Net amount over quantity, to 6 decimals, so quantity x price lands on the line amount
	(ibr-147-ae accepts 3 x 33.333333 against 100.00 on pre-production)."""
	value = (Decimal(str(net)) / Decimal(str(qty))).quantize(Decimal("0.000001"), ROUND_HALF_UP)
	whole, _, fraction = f"{value:f}".partition(".")
	return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def _plain(html: str | None) -> str:
	return re.sub(r"\s+", " ", strip_html(cstr(html))).strip()[:1000]


def _drop_empty(data: dict) -> dict:
	return {key: value for key, value in data.items() if value not in (None, "", [], {})}


# Payload keys

# Flick drops unknown keys without a word, so a typo surfaces as an unrelated Schematron error.
# Every key the builder may send is listed here; anything else is a bug caught before sending.
_ANY = None
ALLOWED_KEYS = {
	"document_identifier": _ANY,
	"issue_date": _ANY,
	"due_date": _ANY,
	"document_type": _ANY,
	"document_currency": _ANY,
	"currency_exchange_rate": _ANY,
	"credit_note_reason_code": _ANY,
	"flags": dict.fromkeys(codes.FLAG_KEYS, _ANY),
	"order_reference": {"purchase_order_id": _ANY},
	"document_references": [{"document_identifier": _ANY, "issue_date": _ANY}],
	"receiving_party": {
		"legal_name": _ANY,
		"peppol_id": _ANY,
		"identifiers": {"AE:VAT": {"value": _ANY}},
		"street_address": _ANY,
		"additional_street_address": _ANY,
		"city_address": _ANY,
		"postal_zone": _ANY,
		"emirates_code": _ANY,
		"country_code": _ANY,
	},
	"payment_means": [
		{
			"type_code": _ANY,
			"payment_id": _ANY,
			"payment_account_id": _ANY,
			"payment_account_name": _ANY,
			"payment_service_provider_id": _ANY,
		}
	],
	"invoice_lines": [
		{
			"id": _ANY,
			"name": _ANY,
			"description": _ANY,
			"quantity": _ANY,
			"uom": _ANY,
			"unit_price": _ANY,
			"line_extension_amount": _ANY,
			"tax_category": _ANY,
			"tax_percentage": _ANY,
			"tax_exemption_reason_code": _ANY,
			"tax_exemption_reason": _ANY,
			"supplier_item_code": _ANY,
		}
	],
	"invoice_totals": dict.fromkeys(
		(
			"line_extension_amount",
			"tax_exclusive_amount",
			"tax_amount",
			"tax_inclusive_amount",
			"payable_rounding_amount",
			"payable_amount",
			"total_tax_amount_in_aed",
			"total_amount_with_tax_in_aed",
		),
		_ANY,
	),
}


def check_keys(payload: dict, allowed: dict = ALLOWED_KEYS, path: str = ""):
	for key, value in payload.items():
		where = f"{path}{key}"
		if key not in allowed:
			raise KeyError(f"Payload key {where!r} is not a known Flick key")
		shape = allowed[key]
		if isinstance(shape, dict) and isinstance(value, dict):
			check_keys(value, shape, where + ".")
		elif isinstance(shape, list) and isinstance(value, list):
			for index, entry in enumerate(value):
				check_keys(entry, shape[0], f"{where}.{index}.")

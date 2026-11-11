"""ERPNext invoice -> Flick UAE document.

One code path builds the payload and decides what is wrong with an invoice, so the check at
submit and the real send can never disagree. Problems are collected, not raised one at a
time, so the user fixes everything in one pass.

Covers every case in Flick's UAE guide: tax invoices and credit notes in and out of scope,
debit notes (a new tax invoice referencing the original, PINT-AE has no debit note), self-
billed invoices and credit notes from purchase invoices, the eight transaction-type flags,
reverse charge, the predefined recipients of PINT-AE §1.5.3, foreign currency, allowances and
charges, delivery, references, attachments, payment means and instalments, payee and tax
representative.

Amounts go as decimal strings (Flick accepts strings or numbers; strings keep the scale).
Credit notes carry positive amounts, though ERPNext stores returns as negatives.
"""

import base64
import json
import mimetypes
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import cstr, flt, get_datetime, get_system_timezone, getdate, strip_html

from flick_einvoicing.compat import file_bytes
from flick_einvoicing.einvoice import code_lists, codes

SALES_TEMPLATE = "Sales Taxes and Charges Template"
PURCHASE_TEMPLATE = "Purchase Taxes and Charges Template"
ITEM_TEMPLATE = "Item Tax Template"
VAT_CHARGE_TYPES = ("On Net Total", "On Previous Row Total", "On Previous Row Amount")
NO_REMARKS = ("", "No Remarks")
# An original that ERPNext doesn't hold, such as one issued in the previous system.
ADD_PRECEDING = "add its number and date under Preceding Invoices on the E-Invoicing tab"


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


def D(value) -> Decimal:
	return Decimal(str(value))


class _Line:
	def __init__(self, payload: dict, category: str | None, amount: Decimal, net: Decimal):
		self.payload = payload
		self.category = category
		self.amount = amount  # what the line states: before the document discount when it is an allowance
		self.net = net  # after the document discount, as ERPNext taxes it


class _Builder:
	def __init__(self, invoice, settings):
		self.inv = invoice
		self.settings = settings
		self.problems: dict[str, None] = {}  # ordered, without repeats
		# ERPNext's precision may be 3 decimals; an e-invoice's money has 2 (ibr-091, ibr-121
		# to ibr-125, found on pre-production), rounded half-up the way Flick derives VAT.
		self.site_p = invoice.precision("grand_total") or 2
		self.p = E_INVOICE_PLACES
		self.self_billed = invoice.doctype == "Purchase Invoice"
		self.is_return = bool(invoice.get("is_return"))
		self.is_debit_note = bool(invoice.get("is_debit_note"))
		self.treatments = {(row.template_type, row.template): row for row in settings.tax_treatments}
		self.charge_accounts = {row.account: row for row in settings.get("charge_accounts") or []}
		self.inclusive = any(tax.get("included_in_print_rate") for tax in invoice.taxes)
		self.discounted = flt(invoice.get("discount_amount")) != 0
		self.allowance_style = (
			self.discounted and not self.inclusive and settings.get("discount_style") != "Spread over lines"
		)
		self._uom_codes: dict[str, str | None] = {}
		self._party = None

	def problem(self, text: str):
		self.problems[text] = None

	def quietly(self, build):
		"""Build an optional block; drop it, and whatever it complained about, if incomplete."""
		before = dict(self.problems)
		value = build()
		if len(self.problems) != len(before):
			self.problems = before
			return None
		return value

	def money(self, value) -> str:
		return f"{_fils(abs(flt(value, self.site_p))):.{self.p}f}"

	def dec(self, value) -> Decimal:
		return D(self.money(value))

	# The document

	def build(self) -> dict:
		inv = self.inv
		company_currency = frappe.get_cached_value("Company", inv.company, "default_currency")
		if company_currency != "AED":
			self.problem(
				f"Company {inv.company} keeps its books in {company_currency}; UAE e-invoicing needs AED."
			)

		self.vat_rows, self.charge_rows = self.classify_tax_rows()
		lines = [self.line(item) for item in inv.items]
		categories = {line.category for line in lines if line.category}
		document_type = self.document_type(categories)
		is_credit_note = document_type in codes.CREDIT_NOTE_TYPES

		allowances, discount_charges = self.document_discount(lines)
		charges = self.charges() + discount_charges
		totals = self.totals(lines, allowances, charges, discount_charges)

		doc = {
			"document_identifier": inv.name,
			"issue_date": str(getdate(inv.posting_date)),
			"issue_time": self.issue_time(),
			"document_type": document_type,
			"document_currency": inv.currency,
			"flags": self.flags(categories),
			"receiving_party": self.receiving_party(categories),
			"issuing_party": self.issuing_party(),
			"invoice_lines": [line.payload for line in lines],
			"allowance_charges": allowances + charges,
			"invoice_totals": totals,
			"note": None
			if cstr(inv.get("remarks")).strip() in NO_REMARKS
			else cstr(inv.remarks).strip()[:1000],
			"buyer_reference": cstr(inv.get("einvoice_buyer_reference")) or None,
			"accounting_cost": cstr(inv.get("einvoice_buyer_accounting_cost")) or None,
			"tax_point_date": self.tax_point_date(),
			"order_reference": self.order_reference(),
			"document_references": self.document_references(is_credit_note),
			"other_references": self.other_references(),
			"additional_document_references": self.supporting_documents(),
			"delivery": self.delivery(),
			"invoice_period": self.invoice_period(),
			"fz_beneficiary_id": self.flag_party(
				"einvoice_is_ftz", "einvoice_fz_beneficiary_id", "Free Zone Beneficiary TRN"
			),
			"principal_id": self.principal(),
			"payment_means": self.payment_means(is_credit_note),
			"payment_terms": None if is_credit_note else self.payment_terms(),
			"payee_party": self.payee_party(),
			"tax_representative": self.tax_representative(),
		}

		self.check_registration(doc, document_type)
		self.check_breakdown_categories(categories, allowances + charges)

		if is_credit_note:
			reason = codes.code_of(inv.get("einvoice_credit_note_reason"))
			if not reason:
				self.problem("Choose a Credit Note Reason on the E-Invoicing tab.")
			doc["credit_note_reason_code"] = reason
			if reason != codes.REASON_WITHOUT_REFERENCE and not doc["document_references"]:
				# ibr-055-ae: only a volume-discount credit note may stand without the original.
				# Checked with or without a reason, so both problems show at once.
				self.problem(
					"This credit note doesn't name the invoice it corrects. Make it from the original invoice "
					"(Create > Return / Credit Note there), which fills Return Against; for an invoice from "
					f"another system, {ADD_PRECEDING}. Only reason VD may stand alone."
				)
		elif D(totals["payable_amount"]) > 0:
			doc["due_date"] = str(getdate(inv.get("due_date") or inv.posting_date))  # ibr-127-ae

		if inv.currency != "AED":
			rate = flt(inv.conversion_rate, 6)
			if rate <= 0:
				self.problem("The invoice has no exchange rate to AED.")
			doc["currency_exchange_rate"] = rate

		return _drop_empty(doc)

	def document_type(self, categories: set[str]) -> str:
		out_of_scope = bool(categories) and categories <= codes.EXEMPT_OR_OUT_OF_SCOPE
		# ibr-151-ae: a 380/381 made only of E and O lines is a 480/81; the same pairing holds for
		# self-billed documents (389 or 480, 261 or 81).
		if self.self_billed:
			if self.is_return:
				return codes.CREDIT_NOTE_OUT_OF_SCOPE if out_of_scope else codes.SELF_BILLED_CREDIT_NOTE
			return codes.INVOICE_OUT_OF_SCOPE if out_of_scope else codes.SELF_BILLED_INVOICE
		if self.is_return:
			return codes.CREDIT_NOTE_OUT_OF_SCOPE if out_of_scope else codes.CREDIT_NOTE
		# A debit note is a tax invoice for the additional amount: PINT-AE has no debit note.
		return codes.INVOICE_OUT_OF_SCOPE if out_of_scope else codes.INVOICE

	def check_registration(self, doc: dict, document_type: str):
		"""Two legal registration ids Flick requires, found on pre-production: the buyer's on an
		out-of-scope document (ibr-136-ae), and on a self-billed one the supplier's, who is the
		seller there (ibr-150-ae). Both are the party's trade licence (AE:TL)."""
		has_licence = bool(((doc.get("receiving_party") or {}).get("identifiers") or {}).get("AE:TL"))
		if has_licence:
			return
		party = "Supplier" if self.self_billed else "Customer"
		name = self.inv.supplier if self.self_billed else self.inv.customer
		if self.self_billed:
			self.problem(
				f"Self-billed invoices need the supplier's trade licence: set it, with the licensing authority, on {party} {name} (ibr-150-ae)."
			)
		elif document_type in (codes.INVOICE_OUT_OF_SCOPE, codes.CREDIT_NOTE_OUT_OF_SCOPE):
			self.problem(
				f"Out-of-scope documents ({document_type}) need the buyer's trade licence: set it, with the licensing authority, on {party} {name} (ibr-136-ae)."
			)

	def check_breakdown_categories(self, line_categories: set[str], entries: list[dict]):
		"""Flick derives the VAT breakdown from the lines, so a charge or allowance taxed in a
		category no line has gets no breakdown row and is refused (aligned-ibrp-o-01 and kin)."""
		for entry in entries:
			category = entry.get("tax_category")
			if category and category not in line_categories:
				kind = "charge" if entry.get("charge_indicator") else "discount"
				self.problem(
					f"A {kind} taxed {category} needs at least one invoice line taxed {category}. "
					"Map the charge account to a category the lines use, or add it as an item line."
				)

	def flags(self, categories: set[str]) -> dict:
		inv = self.inv
		flags = dict.fromkeys(codes.FLAG_KEYS, False)
		flags["is_selfbilled"] = self.self_billed
		for key in (
			"is_ftz",
			"is_deemed",
			"is_summary",
			"is_continuous",
			"is_dab",
			"is_ecommerce",
			"is_export",
		):
			flags[key] = bool(inv.get(f"einvoice_{key}"))
		if codes.MARGIN in categories:
			flags["is_margin"] = True
			# Flick's guide: "The margin scheme cannot be filed ... There is currently no accepted
			# combination." Stop it here rather than let Flick refuse it after submit.
			self.problem(
				"Flick cannot file profit-margin-scheme invoices yet. Flick's developer guide (UAE, Known "
				"defects): \"The margin scheme cannot be filed ... There is currently no accepted combination.\" "
				"Use another VAT treatment for these lines, or wait for Flick's fix."
			)
			if categories != {codes.MARGIN}:
				self.problem(
					"A profit-margin-scheme invoice must have every line in the margin category (ibr-116-ae)."
				)
		return flags

	def issue_time(self) -> str | None:
		posting_time = self.inv.get("posting_time")
		if posting_time in (None, ""):
			return None
		if isinstance(posting_time, timedelta):
			seconds = int(posting_time.total_seconds())
			clock = f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
		else:
			clock = cstr(posting_time).split(".")[0]
			if clock.count(":") == 1:
				clock += ":00"
		try:
			from zoneinfo import ZoneInfo

			moment = datetime.combine(
				getdate(self.inv.posting_date), datetime.strptime(clock, "%H:%M:%S").time()
			)
			offset = moment.replace(tzinfo=ZoneInfo(get_system_timezone())).strftime("%z")
		except Exception:
			return None
		return f"{clock}{offset[:3]}:{offset[3:]}"

	def tax_point_date(self) -> str | None:
		value = self.inv.get("einvoice_tax_point_date")
		if not value:
			return None
		if getdate(value) >= getdate(self.inv.posting_date):
			self.problem("Tax Point Date must fall before the posting date (ibr-141-ae).")
		return str(getdate(value))

	# Tax rows

	def classify_tax_rows(self):
		vat_rows, charge_rows = [], []
		for tax in self.inv.taxes:
			label = f"Tax row {tax.idx} ({tax.description or tax.account_head})"
			if self.self_billed:
				if tax.get("category") == "Valuation":
					continue  # changes stock valuation only, never the invoice
				if tax.get("add_deduct_tax") == "Deduct":
					self.problem(f"{label} deducts tax; deductions can't be e-invoiced.")
					continue
			if tax.account_head in self.charge_accounts:
				if tax.charge_type != "Actual":
					self.problem(f"{label} is a mapped charge account, so it must be charged 'Actual'.")
				charge_rows.append(tax)
			elif tax.charge_type in VAT_CHARGE_TYPES:
				vat_rows.append(tax)
			else:
				self.problem(
					f"{label} is charged '{tax.charge_type}'. Map its account as a document-level charge in "
					"E Invoicing Settings, or charge VAT on the net total."
				)
		return vat_rows, charge_rows

	@staticmethod
	def item_rates(item) -> dict:
		"""The rates the line's Item Tax Template sets, by account."""
		try:
			return json.loads(item.item_tax_rate) if item.item_tax_rate else {}
		except (TypeError, ValueError):
			return {}

	def line_rate(self, item) -> float:
		"""The VAT rate ERPNext applied to this line, worked out the way ERPNext does."""
		item_rates = self.item_rates(item)
		rate = 0.0
		for tax in self.vat_rows:
			rate += flt(item_rates[tax.account_head]) if tax.account_head in item_rates else flt(tax.rate)
		return rate

	def treatment_for(self, item, label):
		if item.get("item_tax_template"):
			# A line-level template overrides the invoice's rates, so never fall back past it.
			row = self.treatments.get((ITEM_TEMPLATE, item.item_tax_template))
			if not row:
				self.problem(
					f"{label}: Item Tax Template {item.item_tax_template} has no tax treatment in E Invoicing Settings."
				)
			return row
		template = self.inv.get("taxes_and_charges")
		if template:
			template_type = PURCHASE_TEMPLATE if self.self_billed else SALES_TEMPLATE
			row = self.treatments.get((template_type, template))
			if not row:
				self.problem(f"{template_type} {template} has no tax treatment in E Invoicing Settings.")
			return row
		self.problem(
			f"{label}: neither the invoice nor the line has a tax template, so its VAT treatment is unknown."
		)
		return None

	# Lines

	def line(self, item) -> _Line:
		label = f"Row {item.idx} ({item.item_code or item.item_name})"
		treatment = self.treatment_for(item, label)
		category = codes.code_of(treatment.tax_category) if treatment else None

		qty = abs(flt(item.qty))
		if not qty:
			self.problem(f"{label}: quantity is zero.")
		net = self.dec(item.net_amount)
		amount = self.dec(item.amount) if self.allowance_style else net

		rate = self.line_rate(item)
		self.check_rate(label, category, rate, treatment, item)

		line = {
			"id": str(item.idx),
			"name": cstr(item.item_name),
			"description": _plain(item.description) or cstr(item.item_name),
			"quantity": _quantity(qty),
			"uom": self.uom_code(item.uom, label),
			"unit_price": _price(amount / D(qty)) if qty else None,
			"line_extension_amount": f"{amount:.{self.p}f}",
			"tax_category": category,
			"tax_percentage": "0"
			if category in ("Z", "E", "O")
			else str(codes.STANDARD_RATE)
			if category in ("S", "AE")
			else None,
		}
		self.price_discount(line, item, amount, qty)
		self.item_details(line, item, category, treatment, label)
		if self.inv.currency != "AED":
			line["line_amount_aed"] = self.money(
				item.base_amount if self.allowance_style else item.base_net_amount
			)
		return _Line(_drop_empty(line), category, amount, net)

	def check_rate(self, label, category, rate, treatment, item):
		if category == "S" and flt(rate, 6) != codes.STANDARD_RATE:
			self.problem(
				f"{label} is mapped as standard rated, but ERPNext charges {rate:g}% on it. "
				"Standard-rated lines must carry exactly 5% VAT."
			)
		elif category in ("Z", "E", "O", "AE") and flt(rate, 6) != 0:
			why = " (under reverse charge the buyer accounts for the VAT)" if category == "AE" else ""
			# ERPNext's own UAE Zero and Exempted templates name accounts of their own, which leaves the
			# invoice's 5% row charging the line.
			# Read the template itself: from ERPNext v16 the line's item_tax_rate also carries the
			# invoice's own rates for accounts the template leaves out.
			listed = (
				{d.tax_type for d in frappe.get_cached_doc("Item Tax Template", item.item_tax_template).taxes}
				if item.get("item_tax_template")
				else set()
			)
			uncovered = [tax.account_head for tax in self.vat_rows if flt(tax.rate) and tax.account_head not in listed]
			fix = (
				f" Its Item Tax Template {item.item_tax_template} does not list {', '.join(uncovered)}, the "
				"account the invoice charges VAT to: add it to the template at 0%."
				if item.get("item_tax_template") and uncovered
				else ""
			)
			self.problem(
				f"{label} is mapped as {treatment.tax_category}, but ERPNext charges {rate:g}% VAT on it{why}.{fix}"
			)
		if category == "S" and (item.get("is_zero_rated") or item.get("is_exempt")):
			self.problem(
				f"{label}: the item is flagged zero-rated or exempt, but its tax template is mapped as standard rated."
			)

	def price_discount(self, line: dict, item, amount: Decimal, qty: float):
		"""Show an item discount as the price discount, when the stated price is the item's rate."""
		if not qty or self.inclusive or (self.discounted and not self.allowance_style):
			return  # the stated price already folds in more than the item discount
		gross = D(item.get("price_list_rate") or 0)
		unit = D(line["unit_price"])
		if gross > unit:
			gross_text = _price(gross)
			line["gross_price"] = gross_text
			line["price_discount"] = _price(D(gross_text) - unit)  # gross - discount must equal the price

	def item_details(self, line: dict, item, category, treatment, label):
		item_doc = frappe.get_cached_doc("Item", item.item_code) if item.get("item_code") else None

		if self.self_billed:
			line["buyers_item_code"] = cstr(item.item_code) or None
			line["supplier_item_code"] = self.supplier_part_no(item) or None
		else:
			line["supplier_item_code"] = cstr(item.item_code) or None
			line["buyers_item_code"] = cstr(item.get("customer_item_code")) or None

		gtin = self.gtin(item_doc, item)
		if gtin:
			line["standard_item_id"], line["standard_item_id_scheme"] = gtin, codes.GTIN_SCHEME

		if category == "E":
			line["tax_exemption_reason_code"] = codes.code_of(treatment.exemption_reason_code)
			line["tax_exemption_reason"] = cstr(treatment.exemption_reason_text) or None
			if not line["tax_exemption_reason_code"]:
				self.problem(
					f"Tax treatment for {treatment.template} is Exempt but has no exemption reason code."
				)
		elif category == "AE":
			line["reverse_charge_item_type"] = codes.code_of(treatment.reverse_charge_item_type)
			if not line["reverse_charge_item_type"]:
				self.problem(
					f"Tax treatment for {treatment.template} is reverse charge but has no goods type (ibr-166-ae)."
				)
			if not gtin:
				self.problem(f"{label}: reverse-charge lines need the item's GTIN barcode (ibr-174-ae).")

		if item_doc:
			if item_doc.get("country_of_origin"):
				line["origin_country"] = (
					cstr(frappe.get_cached_value("Country", item_doc.country_of_origin, "code")).upper()
					or None
				)
			hs_code = cstr(item_doc.get("customs_tariff_number"))
			sac_code = cstr(item_doc.get("einvoice_sac_code"))
			if hs_code or sac_code:
				# G needs the HS code, S the SAC, B both (ibr-184/185/186-ae), so send what is set.
				line["commodity_code"] = (
					codes.COMMODITY_BOTH
					if hs_code and sac_code
					else codes.COMMODITY_GOODS
					if hs_code
					else codes.COMMODITY_SERVICES
				)
				line["hs_code"] = hs_code or None
				line["sac_code"] = sac_code or None
			if item_doc.get("variant_of") and item_doc.get("attributes"):
				line["item_attributes"] = {
					row.attribute: cstr(row.attribute_value)
					for row in item_doc.attributes
					if row.attribute_value
				}

		line["batch_number"] = cstr(item.get("batch_no")) or None
		start, end = item.get("service_start_date"), item.get("service_end_date")
		if start and end:
			line["invoice_period_start_date"], line["invoice_period_end_date"] = (
				str(getdate(start)),
				str(getdate(end)),
			)

	def gtin(self, item_doc, item) -> str | None:
		if not item_doc:
			return None
		for barcode in item_doc.get("barcodes") or []:
			if barcode.get("barcode_type") in codes.GTIN_BARCODE_TYPES and barcode.barcode:
				return cstr(barcode.barcode).strip()
		return None

	def supplier_part_no(self, item) -> str | None:
		return frappe.db.get_value(
			"Item Supplier", {"parent": item.item_code, "supplier": self.inv.supplier}, "supplier_part_no"
		)

	def uom_code(self, uom: str, label: str) -> str | None:
		if uom not in self._uom_codes:
			self._uom_codes[uom] = self._resolve_uom(uom, label)
		return self._uom_codes[uom]

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
		if len(found) > 1:
			self.problem(f"UOM {uom} is linked to more than one code in {code_list}: {', '.join(found)}.")
			return None
		code = found[0] if found else get_default_code(code_list)
		if not code:
			self.problem(
				f"{label}: UOM {uom} has no code in Code List {code_list}. Open the matching Common Code "
				f"(for example H87 piece or C62 one) and add UOM {uom} under Applies To."
			)
			return None
		if code not in code_lists.codes(code_lists.UNITS):
			self.problem(
				f"{label}: UOM {uom} stands for {code} in {code_list}, but PINT-AE has no unit code {code}."
			)
			return None
		return code

	# Allowances, charges and totals

	def document_discount(self, lines: list[_Line]) -> tuple[list[dict], list[dict]]:
		"""The invoice discount per VAT category: allowances, and charges where it is negative.

		A negative discount raises the total (to round it up, say). PINT-AE has no negative
		allowance, so it goes as a charge, under the reason code the client chose in settings."""
		if not self.allowance_style:
			return [], []
		by_category: dict[str, Decimal] = {}
		for line in lines:
			if line.category:
				by_category[line.category] = by_category.get(line.category, D(0)) + (line.amount - line.net)
		allowances, charges = [], []
		for category, amount in by_category.items():
			if amount > 0:
				allowances.append(
					_drop_empty(
						{
							"charge_indicator": False,
							"reason_code": codes.DISCOUNT_REASON_CODE,
							"reason": "Discount",
							"amount": f"{amount:.{self.p}f}",
							"tax_category": category,
							"tax_percentage": self.category_rate(category),
						}
					)
				)
			elif amount < 0:
				charges.append(self.discount_charge(category, -amount))
		return allowances, charges

	def discount_charge(self, category: str, amount: Decimal) -> dict:
		reason_code = cstr(self.settings.get("negative_discount_reason_code")).strip()
		if not reason_code:
			self.problem(
				"The invoice discount is negative, so it raises the total, and the e-invoice states that as a "
				"charge. Set Reason Code for Negative Discounts in E Invoicing Settings."
			)
		else:
			self.check_charge_reason(reason_code, "Reason Code for Negative Discounts")
		return _drop_empty(
			{
				"charge_indicator": True,
				"reason_code": reason_code or None,
				"reason": cstr(self.settings.get("negative_discount_reason")).strip() or None,
				"amount": f"{amount:.{self.p}f}",
				"tax_category": category,
				"tax_percentage": self.category_rate(category),
			}
		)

	def charges(self) -> list[dict]:
		entries = []
		for tax in self.charge_rows:
			mapping = self.charge_accounts[tax.account_head]
			category = codes.code_of(mapping.tax_category)
			reason_code = cstr(mapping.reason_code).strip()
			self.check_charge_reason(reason_code, f"The reason code for charge account {tax.account_head}")
			entries.append(
				_drop_empty(
					{
						"charge_indicator": True,
						"reason_code": reason_code,
						"reason": cstr(mapping.reason or tax.description) or None,
						"amount": self.money(tax.tax_amount),
						"tax_category": category,
						"tax_percentage": self.category_rate(category),
					}
				)
			)
		return entries

	def check_charge_reason(self, reason_code: str, label: str) -> None:
		"""Settings refuse other codes, but a code the specification later drops stays saved."""
		if reason_code not in code_lists.codes(code_lists.CHARGE_REASONS):
			self.problem(
				f"{label} in E Invoicing Settings is {reason_code}, which is not a charge reason code "
				"in PINT-AE's UNCL 7161 list. Pick one from the list there."
			)

	def category_rate(self, category: str | None) -> str | None:
		if category in ("S", "AE"):
			return str(codes.STANDARD_RATE)
		if category == "Z":
			return "0"
		return None

	def totals(
		self, lines: list[_Line], allowances: list[dict], charges: list[dict], discount_charges: list[dict]
	) -> dict:
		"""Stated totals, so the e-invoice matches ERPNext to the fils.

		Flick enforces its arithmetic on stated totals, so the same rules run here first
		(ibr-co-10 to ibr-co-16, aligned-ibrp-s-08/09). A mismatch stops the submit, rather than
		coming back from Flick as a rejection after the invoice is booked.
		"""
		inv = self.inv
		p = self.p
		# On a site keeping 3 decimals, rounding each amount to fils moves sums by up to a fil a
		# line; on a 2-decimal site everything must agree exactly. VAT is allowed one fil either
		# way, since ERPNext may round half a fil to even where Flick rounds it up.
		per_line = D(0) if self.site_p <= p else FIL * max(len(lines), 1)
		line_total = sum((line.amount for line in lines), D(0))
		allowance_total = sum((D(a["amount"]) for a in allowances), D(0))
		charge_total = sum((D(c["amount"]) for c in charges), D(0))
		# A negative invoice discount is part of ERPNext's net total; other charges are tax rows.
		discount_charge_total = sum((D(c["amount"]) for c in discount_charges), D(0))

		net_total = self.dec(inv.net_total)
		taxes_total = self.dec(inv.total_taxes_and_charges)
		books_vat = taxes_total - (charge_total - discount_charge_total)
		grand_total = self.dec(inv.grand_total)
		rounded = flt(inv.get("rounded_total")) and not inv.get("disable_rounded_total")
		payable = self.dec(inv.rounded_total) if rounded else grand_total

		after_discount = line_total - allowance_total + discount_charge_total
		if abs(after_discount - net_total) > per_line:
			self.problem(
				f"Line amounts after the invoice discount come to {after_discount}, but the net total is {net_total}."
			)
		tax_exclusive = line_total - allowance_total + charge_total

		vat = books_vat
		categories = {line.category for line in lines}
		if codes.MARGIN not in categories:
			# VAT is 5% of the standard-rated base: lines, less their discount, plus charges taxed
			# S. State it as Flick derives it, or ibr-co-14 refuses the document.
			base = sum((line.amount for line in lines if line.category == "S"), D(0))
			base -= sum((D(a["amount"]) for a in allowances if a.get("tax_category") == "S"), D(0))
			base += sum((D(c["amount"]) for c in charges if c.get("tax_category") == "S"), D(0))
			vat = _fils(base * codes.STANDARD_RATE / 100)
			if abs(vat - books_vat) > FIL:
				self.problem(
					f"VAT on the invoice is {books_vat}, but 5% of the standard-rated amount ({base}) is {vat}. "
					"Check the taxes table and that each template and charge is mapped correctly."
				)
		tax_inclusive = tax_exclusive + vat
		if abs(tax_inclusive - grand_total) > per_line + FIL:
			self.problem(f"Net {tax_exclusive} plus VAT {vat} does not equal the grand total {grand_total}.")

		totals = {
			"line_extension_amount": f"{line_total:.{p}f}",
			"allowance_total_amount": f"{allowance_total:.{p}f}" if allowances else None,
			"charge_total_amount": f"{charge_total:.{p}f}" if charges else None,
			"tax_exclusive_amount": f"{tax_exclusive:.{p}f}",
			"tax_amount": f"{vat:.{p}f}",
			"tax_inclusive_amount": f"{tax_inclusive:.{p}f}",
			# What the buyer owes is ERPNext's figure; any fils between it and the e-invoice's own
			# arithmetic are stated as rounding (ibr-co-16).
			"payable_amount": f"{payable:.{p}f}",
		}
		if payable != tax_inclusive:
			totals["payable_rounding_amount"] = f"{payable - tax_inclusive:.{p}f}"
		if inv.currency != "AED":
			# Required on foreign-currency documents and never derived by Flick.
			base_charges = sum((abs(flt(t.base_tax_amount)) for t in self.charge_rows), 0)
			totals["total_tax_amount_in_aed"] = self.money(
				abs(flt(inv.base_total_taxes_and_charges)) - base_charges
			)
			totals["total_amount_with_tax_in_aed"] = self.money(inv.base_grand_total)
		return _drop_empty(totals)

	# Parties

	def receiving_party(self, categories: set[str]) -> dict:
		inv = self.inv
		if self.self_billed:
			doctype, name = "Supplier", inv.supplier
			party = frappe.get_cached_doc("Supplier", name)
			legal_name = inv.get("supplier_name") or party.supplier_name
			address_name = inv.get("supplier_address")
			routing = codes.ROUTING_PEPPOL
		else:
			doctype, name = "Customer", inv.customer
			party = frappe.get_cached_doc("Customer", name)
			legal_name = cstr(party.get("einvoice_legal_name")).strip() or inv.get("customer_name") or party.customer_name
			address_name = inv.get("customer_address")
			routing = party.get("einvoice_routing") or codes.ROUTING_PEPPOL

		if inv.get("einvoice_is_deemed"):
			peppol_id = codes.DEEMED_SUPPLY_ENDPOINT  # reported only, not exchanged
		elif routing in codes.ROUTING_ENDPOINTS:
			peppol_id = codes.ROUTING_ENDPOINTS[routing]
		else:
			peppol_id = cstr(party.get("einvoice_peppol_id")).strip()
			if not peppol_id:
				self.problem(f"{doctype} {name} has no Peppol ID.")
			elif not re.match(codes.PEPPOL_ID_PATTERN, peppol_id):
				self.problem(
					f"{doctype} {name}: Peppol ID {peppol_id} should be 0235: followed by the 10-digit Peppol "
					"number starting with 1, e.g. 0235:1234567890 (not the 15-digit TRN). Flick's guide: "
					"receiving_party.peppol_id, size 10 digits (1XXXXXXXXX)."
				)

		trn = cstr(inv.get("tax_id") or party.get("tax_id")).strip()
		if trn and not re.match(codes.TRN_PATTERN, trn):
			self.problem(f"{doctype} {name}: TRN {trn} should be 15 digits.")
		if "AE" in categories and not trn:
			self.problem(f"Reverse charge needs the buyer's TRN on {doctype} {name} (ibr-103-ae).")

		identifiers = {}
		if trn:
			identifiers["AE:VAT"] = {"value": trn}  # never AE:TIN: Flick rejects it for the buyer
		licence = cstr(party.get("einvoice_trade_licence")).strip()
		if licence:
			identifiers["AE:TL"] = {
				"value": licence,
				"authority_name": cstr(party.get("einvoice_trade_licence_authority")) or None,
			}
			if not identifiers["AE:TL"]["authority_name"]:
				# Flick's guide: authority_name is required with any AE:TL, "ibr-172-ae seller, ibr-101-ae buyer";
				# on a self-billed document this party is the supplier, the seller.
				rule = "ibr-172-ae" if self.self_billed else "ibr-101-ae"
				self.problem(f"{doctype} {name}: a trade licence needs its licensing authority ({rule}).")

		# The buyer's own identifier (Flick's party_identifier, IBT-046): what names a buyer with no
		# TRN and no Peppol ID of its own. Left empty, its trade licence number stands in.
		party_identifier = ""
		if doctype == "Customer":
			party_identifier = cstr(party.get("einvoice_party_identifier")).strip() or licence
		if (
			peppol_id
			and not re.match(codes.PEPPOL_ID_PATTERN, peppol_id)
			and not inv.get("einvoice_is_export")
			and not trn
			and not party_identifier
		):
			# ibr-135-ae: a buyer on a placeholder address (abroad, not subject, deemed) has no Peppol
			# ID to name them; outside an export Flick needs the buyer's TRN or identifier.
			self.problem(
				f"{doctype} {name} has no Peppol address of its own, no TRN and no identifier, so Flick cannot "
				"identify the buyer (ibr-135-ae). Set the buyer's TRN, or its trade licence (or Buyer "
				"Identifier) on the customer. For a buyer outside the UAE, ticking Export also works."
			)

		block = {
			"legal_name": cstr(legal_name),
			# Flick's guide: "Trading name the buyer operates under, when it differs from the legal name."
			"trade_name": (cstr(party.get("einvoice_trade_name")).strip() or None) if doctype == "Customer" else None,
			"peppol_id": peppol_id,
			"party_identifier": party_identifier or None,
			"identifiers": _drop_empty(identifiers),
		}
		block.update(self.contact())

		foreign_allowed = routing in codes.ROUTING_ENDPOINTS or inv.get("einvoice_is_export")
		if not address_name:
			self.problem(
				f"The invoice has no {doctype.lower()} address; the {'supplier' if self.self_billed else 'buyer'}'s address is required."
			)
			return _drop_empty(block)
		block.update(self.address(address_name, foreign_allowed, f"{doctype} address {address_name}"))
		return _drop_empty(block)

	def contact(self) -> dict:
		inv = self.inv
		if not inv.get("contact_person"):
			return {}
		contact = frappe.get_cached_doc("Contact", inv.contact_person)
		name = " ".join(filter(None, (contact.first_name, contact.last_name))) or contact.name
		return {
			"contact_name": cstr(name) or None,
			"contact_telephone": cstr(inv.get("contact_mobile") or contact.mobile_no or contact.phone)
			or None,
			"contact_email": cstr(inv.get("contact_email") or contact.email_id) or None,
		}

	def address(self, address_name: str, foreign_allowed, label: str) -> dict:
		address = frappe.get_cached_doc("Address", address_name)
		for fieldname, field_label in (
			("address_line1", "Address Line 1"),
			("city", "City/Town"),
			("country", "Country"),
		):
			if not address.get(fieldname):
				self.problem(f"{label} has no {field_label}.")
		country = (
			cstr(frappe.get_cached_value("Country", address.country, "code")).upper()
			if address.country
			else ""
		)
		if country and country != "AE" and not foreign_allowed:
			self.problem(
				f"{label} is outside the UAE. Set the customer's E-Invoice Routing for a buyer abroad, or mark the invoice as an export."
			)
		return {
			"street_address": cstr(address.address_line1),
			"additional_street_address": cstr(address.address_line2) or None,
			"city_address": cstr(address.city),
			"postal_zone": cstr(address.pincode) or None,
			"emirates_code": self.emirate(address, country, label),
			"country_code": country or None,
		}

	def emirate(self, address, country: str, label: str) -> str | None:
		if country and country != "AE":
			# ibr-152-ae wants a country subdivision even on a foreign address.
			code = codes.EMIRATE_CODES.get(self.settings.get("export_emirate"))
			if not code:
				self.problem(
					f"{label} is abroad: set the Emirate Sent for Addresses Abroad in E Invoicing Settings."
				)
			return code
		if not frappe.get_meta("Address").has_field("emirate"):
			self.problem(
				"Address has no Emirate field. ERPNext adds it with its UAE regional setup for a UAE company."
			)
			return None
		name = address.get("emirate")
		if not name:
			self.problem(f"{label} has no Emirate.")
			return None
		code = codes.EMIRATE_CODES.get(name)
		if not code:
			self.problem(f"{label}: unknown emirate {name}.")
		return code

	def issuing_party(self) -> dict | None:
		s = self.settings
		return _drop_empty(
			{
				"trade_name": cstr(s.get("seller_trade_name")) or None,
				"contact_name": cstr(s.get("seller_contact_name")) or None,
				"contact_telephone": cstr(s.get("seller_contact_phone")) or None,
				"contact_email": cstr(s.get("seller_contact_email")) or None,
			}
		)

	def flag_party(self, flag: str, field: str, label: str) -> str | None:
		if not self.inv.get(flag):
			return None
		value = cstr(self.inv.get(field)).strip()
		if not value:
			self.problem(f"Fill in the {label} on the E-Invoicing tab.")
		elif not re.match(codes.TRN_PATTERN, value):
			self.problem(f"{label} {value} should be a 15-digit TRN.")
		return value or None

	def principal(self) -> str | None:
		principal = self.flag_party("einvoice_is_dab", "einvoice_principal_id", "Principal TRN")
		if (
			principal
			and principal == cstr(frappe.get_cached_value("Company", self.inv.company, "tax_id")).strip()
		):
			self.problem(
				"The principal's TRN must differ from your own (ibr-176-ae): an agent can't bill for itself."
			)
		return principal

	def payee_party(self) -> dict | None:
		s = self.settings
		block = _drop_empty(
			{
				"name": cstr(s.get("payee_name")) or None,
				"identifier": _drop_empty(
					{
						"value": cstr(s.get("payee_identifier")) or None,
						"scheme_id": cstr(s.get("payee_identifier_scheme")) or None,
					}
				),
				"legal_registration_id": _drop_empty(
					{"value": cstr(s.get("payee_legal_registration_id")) or None}
				),
				"bank_creditor_id": cstr(s.get("payee_bank_creditor_id")) or None,
			}
		)
		if block and not block.get("name"):
			self.problem("Payee details in E Invoicing Settings need the payee's name (ibr-017).")
		return block or None

	def tax_representative(self) -> dict | None:
		s = self.settings
		if not (s.get("tax_rep_name") or s.get("tax_rep_vat_number") or s.get("tax_rep_address")):
			return None
		block = {"name": cstr(s.get("tax_rep_name")), "vat_number": cstr(s.get("tax_rep_vat_number"))}
		if not block["name"] or not re.match(codes.TRN_PATTERN, block["vat_number"]):
			self.problem("A tax representative needs a name and a 15-digit TRN in E Invoicing Settings.")
		if not s.get("tax_rep_address"):
			self.problem("A tax representative needs an address in E Invoicing Settings.")
		else:
			block.update(
				self.address(s.tax_rep_address, True, f"Tax representative address {s.tax_rep_address}")
			)
		return _drop_empty(block)

	# References and supporting documents

	def order_reference(self) -> dict | None:
		# PINT-AE: "an invoice instance cannot reference a sales order, without providing the
		# corresponding purchase order reference" (Flick's schema check refuses it), so the sales
		# order goes only beside the buyer's PO number.
		purchase_order = cstr(self.inv.get("po_no")).strip()
		if not purchase_order:
			return None
		sales_orders = _unique(item.get("sales_order") for item in self.inv.items)
		return _drop_empty(
			{
				"purchase_order_id": purchase_order,
				"sales_order_id": sales_orders[0] if sales_orders else None,
			}
		)

	def document_references(self, is_credit_note: bool) -> list[dict]:
		inv = self.inv
		references = []
		if inv.get("return_against") and (is_credit_note or self.is_debit_note):
			original_date = frappe.db.get_value(inv.doctype, inv.return_against, "posting_date")
			references.append(
				{
					"document_identifier": inv.return_against,
					"issue_date": str(original_date) if original_date else None,
				}
			)
		elif self.is_debit_note and not any(
			cstr(row.document_identifier).strip() for row in inv.get("einvoice_preceding_invoices") or []
		):
			self.problem(
				"A debit note is sent as a tax invoice referencing the original: set Return Against, or for "
				f"an invoice from another system, {ADD_PRECEDING}."
			)
		for row in inv.get("einvoice_preceding_invoices") or []:
			references.append(
				{
					"document_identifier": cstr(row.document_identifier).strip(),
					"issue_date": str(row.issue_date) if row.issue_date else None,
				}
			)
		return [_drop_empty(r) for r in references if r.get("document_identifier")]

	def other_references(self) -> dict | None:
		inv = self.inv
		delivery_notes = _unique(item.get("delivery_note") for item in inv.items)
		contract = _drop_empty(
			{
				"id": cstr(inv.get("einvoice_contract_reference")) or None,
				"contract_value": cstr(inv.get("einvoice_contract_value")) or None,
			}
		)
		if contract.get("contract_value") and not contract.get("id"):
			self.problem("A contract value needs the contract reference with it.")
		return _drop_empty(
			{
				"despatch_document_reference": ", ".join(delivery_notes) or None,
				"receipt_document_reference": cstr(inv.get("einvoice_receipt_reference")) or None,
				"tender_or_lot_reference": cstr(inv.get("einvoice_tender_reference")) or None,
				"contract_document_reference": contract,
				"customs_reference_number": cstr(inv.get("einvoice_customs_reference")) or None,
				"project_reference": cstr(inv.get("project")) or None,
			}
		)

	def supporting_documents(self) -> list[dict]:
		entries = []
		for row in self.inv.get("einvoice_documents") or []:
			entry = {
				"document_identifier": cstr(row.document_identifier).strip(),
				"document_description": cstr(row.document_description) or None,
			}
			if not entry["document_identifier"]:
				# Flick drops an entry without one in silence, attachment and all.
				self.problem(f"Supporting document row {row.idx} needs a document number.")
				continue
			if row.kind == "Link":
				entry["attachment_uri"] = cstr(row.attachment_uri).strip() or None
				if not entry["attachment_uri"]:
					self.problem(f"Supporting document row {row.idx} needs its URL.")
			elif row.kind == "Reference":
				entry["document_type_code"] = codes.INVOICED_OBJECT if row.invoiced_object else None
				entry["scheme_id"] = cstr(row.scheme_id) or None
			else:
				entry["embedded_document"] = self.embedded_file(row)
			entries.append(_drop_empty(entry))
		return entries

	def embedded_file(self, row) -> dict | None:
		if not row.file:
			self.problem(f"Supporting document row {row.idx} needs its file.")
			return None
		file_name = frappe.db.get_value("File", {"file_url": row.file}, "name")
		if not file_name:
			self.problem(f"Supporting document row {row.idx}: file {row.file} was not found.")
			return None
		file = frappe.get_doc("File", file_name)
		content = file_bytes(file)
		if len(content) > codes.ATTACHMENT_LIMIT_BYTES:
			self.problem(f"Supporting document row {row.idx}: {file.file_name} is over Flick's 20 MB limit.")
			return None
		mime = mimetypes.guess_type(file.file_name or "")[0]
		if mime not in codes.ATTACHMENT_TYPES:
			self.problem(
				f"Supporting document row {row.idx}: {file.file_name} is not an accepted file type. The UAE "
				"e-invoice specification (PINT AE, Media Types code list) allows PDF, PNG, JPG, CSV, XLSX, ODS "
				"and XML."
			)
			return None
		return {"mime_code": mime, "filename": file.file_name, "content": base64.b64encode(content).decode()}

	# Delivery and period

	def delivery(self) -> dict | None:
		inv = self.inv
		flagged = inv.get("einvoice_is_ecommerce") or inv.get("einvoice_is_export")
		# Self-billed: the purchase invoice's own Shipping Address (where the goods come to). Flick's guide
		# pairs is_export and is_ecommerce with a delivery address and does not exempt self-billing.
		shipping = inv.get("shipping_address") if self.self_billed else inv.get("shipping_address_name")
		block = {
			"actual_delivery_date": self.delivery_date(),
			"party_name": cstr(inv.get("einvoice_delivery_party")) or None,
			"location_id": cstr(inv.get("einvoice_delivery_location_id")) or None,
			"location_id_scheme": cstr(inv.get("einvoice_delivery_location_scheme")) or None,
			"delivery_terms": self.incoterms(),
		}
		if shipping and flagged:
			block["address"] = _drop_empty(self.address(shipping, True, f"Shipping address {shipping}"))
		elif shipping:
			# Optional here: send it when it is complete, never block the submit over it.
			block["address"] = self.quietly(lambda: _drop_empty(self.address(shipping, True, "")))
		elif flagged:
			# ibr-142-ae / ibr-152-ae: street, city, emirate and country, all four.
			self.problem("E-commerce and export invoices need a Shipping Address (the delivery address).")
		if inv.get("einvoice_is_export") and block.get("address", {}).get("country_code") == "AE":
			# Flick does not enforce the destination half of the rule; we do.
			self.problem("The invoice is marked as an export, but its shipping address is in the UAE.")
		return _drop_empty(block)

	def delivery_date(self) -> str | None:
		inv = self.inv
		if inv.get("einvoice_delivery_date"):
			return str(getdate(inv.einvoice_delivery_date))
		dates = [getdate(item.delivery_date) for item in inv.items if item.get("delivery_date")]
		notes = _unique(item.get("delivery_note") for item in inv.items)
		if notes:
			dates += [
				getdate(d)
				for d in frappe.get_all("Delivery Note", {"name": ("in", notes)}, pluck="posting_date")
				if d
			]
		return str(max(dates)) if dates else None

	def incoterms(self) -> dict | None:
		incoterm = self.inv.get("incoterm")
		if not incoterm:
			return None
		code = cstr(frappe.get_cached_value("Incoterm", incoterm, "code") or incoterm).upper()
		if code not in codes.INCOTERMS:
			self.problem(
				f"Incoterm {incoterm} can't go on a UAE e-invoice; Flick accepts {', '.join(codes.INCOTERMS)}."
			)
			return None
		return {"incoterms": code}

	def invoice_period(self) -> dict | None:
		inv = self.inv
		summary, continuous = inv.get("einvoice_is_summary"), inv.get("einvoice_is_continuous")
		start = inv.get("einvoice_period_start") or (inv.get("from_date") if summary or continuous else None)
		end = inv.get("einvoice_period_end") or (inv.get("to_date") if summary or continuous else None)
		if summary and not (start and end):
			self.problem("A summary invoice needs its Period Start and Period End (ibr-138-ae).")
		if not (start or end):
			return None
		if start and end and getdate(end) < getdate(start):
			self.problem("Period End falls before Period Start (ibr-029).")
		return _drop_empty(
			{
				"start_date": str(getdate(start)) if start else None,
				"end_date": str(getdate(end)) if end else None,
				# The only frequency code PINT-AE lists; a summary invoice covers one calendar month.
				"description_code": "MTH" if summary else None,
			}
		)

	# Payment

	def payment_means(self, is_credit_note: bool) -> list[dict] | None:
		inv = self.inv
		paid_rows = (
			[row for row in inv.get("payments") or [] if flt(row.amount)] if not self.self_billed else []
		)
		modes = {
			row.mode_of_payment: codes.code_of(row.payment_means_code)
			for row in self.settings.get("payment_modes") or []
		}
		default = codes.code_of(self.settings.payment_means_code)

		if paid_rows:
			entries = []
			for row in paid_rows:
				code = modes.get(row.mode_of_payment) or default
				if not code:
					self.problem(
						f"Mode of payment {row.mode_of_payment} has no payment means in E Invoicing Settings."
					)
					continue
				entries.append(self.means_entry(code))
			return _unique_dicts(entries)

		if not default:
			if not (is_credit_note or inv.get("einvoice_is_deemed")):  # ibr-191-ae
				self.problem("Set the default Payment Means in E Invoicing Settings.")
			return None
		return [self.means_entry(default)]

	def means_entry(self, code: str) -> dict:
		s = self.settings
		entry = {"type_code": code, "payment_id": self.inv.name}
		if code in codes.PAYMENT_MEANS_NEEDING_ACCOUNT:
			entry["payment_account_id"] = cstr(s.payment_account_id).strip() or None
			entry["payment_account_name"] = cstr(s.payment_account_name).strip() or None
			entry["payment_service_provider_id"] = cstr(s.get("payment_service_provider_id")).strip() or None
			if not entry["payment_account_id"]:
				self.problem(
					"Payment means 30 (credit transfer) needs the IBAN in E Invoicing Settings (ibr-192-ae)."
				)
		elif code == codes.DIRECT_DEBIT and not self.self_billed:
			customer = frappe.get_cached_doc("Customer", self.inv.customer)
			entry["mandate"] = _drop_empty(
				{
					"id": cstr(customer.get("einvoice_mandate_id")) or None,
					"debited_account_id": cstr(customer.get("einvoice_mandate_account")) or None,
				}
			)
		return _drop_empty(entry)

	def payment_terms(self) -> list[dict] | None:
		schedule = self.inv.get("payment_schedule") or []
		if not schedule:
			return None
		return [
			_drop_empty(
				{
					"note": cstr(row.get("description") or row.get("payment_term")) or None,
					"amount": self.money(row.payment_amount),
					"installment_due_date": str(getdate(row.due_date)) if row.due_date else None,
				}
			)
			for row in schedule
		]


# Formatting


E_INVOICE_PLACES = 2
FIL = Decimal("0.01")


def _fils(value) -> Decimal:
	"""Round half-up to fils: what Flick does when it derives VAT (probed on pre-production)."""
	return D(value).quantize(FIL, ROUND_HALF_UP)


def _quantity(qty: float) -> str:
	text = f"{D(qty).quantize(Decimal('0.000001'), ROUND_HALF_UP):f}"
	return text.rstrip("0").rstrip(".") if "." in text else text


def _price(value: Decimal) -> str:
	"""To 6 decimals, so quantity x price lands on the line amount
	(ibr-147-ae accepts 3 x 33.333333 against 100.00 on pre-production)."""
	value = value.quantize(Decimal("0.000001"), ROUND_HALF_UP)
	whole, _, fraction = f"{value:f}".partition(".")
	return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def _plain(html: str | None) -> str:
	return re.sub(r"\s+", " ", strip_html(cstr(html))).strip()[:1000]


def _unique(values) -> list[str]:
	seen = []
	for value in values:
		if value and value not in seen:
			seen.append(value)
	return seen


def _unique_dicts(entries: list[dict]) -> list[dict]:
	seen, out = set(), []
	for entry in entries:
		key = json.dumps(entry, sort_keys=True)
		if key not in seen:
			seen.add(key)
			out.append(entry)
	return out


def _drop_empty(data: dict | None) -> dict | None:
	if not data:
		return data
	return {key: value for key, value in data.items() if value not in (None, "", [], {})}


# Payload keys

# Flick drops unknown keys without a word, so a typo surfaces as an unrelated Schematron error.
# Every key the builder may send is listed here; anything else is a bug caught before sending.
_ANY = None
_ADDRESS = dict.fromkeys(
	(
		"street_address",
		"additional_street_address",
		"additional_address_lines",
		"city_address",
		"postal_zone",
		"emirates_code",
		"country_code",
	),
	_ANY,
)
_CONTACT = dict.fromkeys(("contact_name", "contact_telephone", "contact_email"), _ANY)
_ALLOWANCE = dict.fromkeys(
	(
		"charge_indicator",
		"reason_code",
		"reason",
		"percentage",
		"amount",
		"base_amount",
		"tax_category",
		"tax_percentage",
	),
	_ANY,
)
ALLOWED_KEYS = {
	**dict.fromkeys(
		(
			"document_identifier",
			"issue_date",
			"issue_time",
			"due_date",
			"document_type",
			"note",
			"tax_point_date",
			"document_currency",
			"currency_exchange_rate",
			"accounting_cost",
			"buyer_reference",
			"credit_note_reason_code",
			"fz_beneficiary_id",
			"principal_id",
		),
		_ANY,
	),
	"flags": dict.fromkeys(codes.FLAG_KEYS, _ANY),
	"invoice_period": dict.fromkeys(("start_date", "end_date", "description_code"), _ANY),
	"order_reference": dict.fromkeys(("purchase_order_id", "sales_order_id"), _ANY),
	"document_references": [dict.fromkeys(("document_identifier", "issue_date"), _ANY)],
	"other_references": {
		**dict.fromkeys(
			(
				"despatch_document_reference",
				"receipt_document_reference",
				"tender_or_lot_reference",
				"customs_reference_number",
				"project_reference",
			),
			_ANY,
		),
		"contract_document_reference": dict.fromkeys(("id", "contract_value"), _ANY),
	},
	"additional_document_references": [
		{
			**dict.fromkeys(
				(
					"document_identifier",
					"document_type_code",
					"scheme_id",
					"document_description",
					"attachment_uri",
				),
				_ANY,
			),
			"embedded_document": dict.fromkeys(("mime_code", "filename", "content"), _ANY),
		}
	],
	"issuing_party": {"trade_name": _ANY, **_CONTACT},
	"receiving_party": {
		"legal_name": _ANY,
		"trade_name": _ANY,
		"peppol_id": _ANY,
		"party_identifier": _ANY,
		"identifiers": {"AE:VAT": {"value": _ANY}, "AE:TL": {"value": _ANY, "authority_name": _ANY}},
		**_ADDRESS,
		**_CONTACT,
	},
	"payee_party": {
		"name": _ANY,
		"identifier": dict.fromkeys(("value", "scheme_id"), _ANY),
		"legal_registration_id": {"value": _ANY},
		"bank_creditor_id": _ANY,
	},
	"tax_representative": {"name": _ANY, "vat_number": _ANY, **_ADDRESS},
	"delivery": {
		**dict.fromkeys(("actual_delivery_date", "location_id", "location_id_scheme", "party_name"), _ANY),
		"address": _ADDRESS,
		"delivery_terms": {"incoterms": _ANY},
	},
	"payment_means": [
		{
			**dict.fromkeys(
				(
					"type_code",
					"payment_id",
					"payment_account_id",
					"payment_account_name",
					"payment_service_provider_id",
				),
				_ANY,
			),
			"mandate": dict.fromkeys(("id", "debited_account_id"), _ANY),
		}
	],
	"payment_terms": [
		dict.fromkeys(("payment_instruction_id", "note", "amount", "installment_due_date"), _ANY)
	],
	"allowance_charges": [_ALLOWANCE],
	"invoice_lines": [
		{
			**dict.fromkeys(
				(
					"id",
					"name",
					"description",
					"quantity",
					"uom",
					"unit_price",
					"gross_price",
					"price_discount",
					"line_extension_amount",
					"invoice_period_start_date",
					"invoice_period_end_date",
					"buyers_item_code",
					"supplier_item_code",
					"standard_item_id",
					"standard_item_id_scheme",
					"origin_country",
					"commodity_code",
					"hs_code",
					"sac_code",
					"reverse_charge_item_type",
					"batch_number",
					"tax_category",
					"tax_percentage",
					"tax_exemption_reason_code",
					"tax_exemption_reason",
					"line_amount_aed",
				),
				_ANY,
			),
			# A free map of attribute name to value.
			"item_attributes": "*",
		}
	],
	"invoice_totals": dict.fromkeys(
		(
			"line_extension_amount",
			"allowance_total_amount",
			"charge_total_amount",
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
		if shape == "*":
			continue
		if isinstance(shape, dict) and isinstance(value, dict):
			check_keys(value, shape, where + ".")
		elif isinstance(shape, list) and isinstance(value, list):
			for index, entry in enumerate(value):
				check_keys(entry, shape[0], f"{where}.{index}.")

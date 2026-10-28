"""Invoices suppliers send this participant over Peppol.

Each one lands as an E Invoice Received record, with Flick's PDF and UBL attached. A draft
Purchase Invoice can then be made from it; what can't be matched (supplier, items, UOMs) is
listed on the record for whoever reviews the draft. Nothing here submits anything.
"""

import json

import frappe
from frappe.utils import cstr, flt, getdate, today

from flick_einvoicing.einvoice import codes
from flick_einvoicing.providers import get_client
from flick_einvoicing.providers.flick import INCOMING

RECEIVED = "E Invoice Received"
RETURN_TYPES = {codes.CREDIT_NOTE, codes.CREDIT_NOTE_OUT_OF_SCOPE, codes.SELF_BILLED_CREDIT_NOTE}


def receive(document_id: str | None, settings) -> str | None:
	"""Store one incoming document. Safe to call again for the same document."""
	if not document_id:
		return None
	if frappe.db.exists(RECEIVED, document_id):
		return document_id
	client = get_client(settings)
	response = client.get_document(document_id)
	if not response.ok or not isinstance(response.data, dict):
		return None
	data = response.data
	if data.get("direction") not in (INCOMING, None):
		return None

	document = data.get("document") or {}
	counterparty = data.get("counterparty") or {}
	seller = document.get("issuing_party") or {}
	sender_vat = cstr(((seller.get("identifiers") or {}).get("AE:VAT") or {}).get("value")) or cstr(
		counterparty.get("vat_number")
	)
	sender_peppol = cstr(counterparty.get("peppol_id") or seller.get("peppol_id"))
	totals = document.get("invoice_totals") or {}

	record = frappe.get_doc(
		{
			"doctype": RECEIVED,
			"document_id": document_id,
			"company": settings.company,
			"status": "New",
			"document_identifier": data.get("document_identifier") or document.get("document_identifier"),
			"document_type": document.get("document_type") or data.get("type"),
			"issue_date": document.get("issue_date"),
			"sender_name": counterparty.get("name") or seller.get("legal_name"),
			"sender_peppol_id": sender_peppol,
			"sender_vat": sender_vat,
			"supplier": match_supplier(sender_peppol, sender_vat),
			"currency": document.get("document_currency"),
			"total_amount": flt(totals.get("payable_amount") or totals.get("tax_inclusive_amount")),
			"tax_amount": flt(totals.get("tax_amount")),
			"document_json": json.dumps(document, indent=1),
		}
	)
	record.insert(ignore_permissions=True)

	for kind in ("pdf", "xml"):
		file = client.get_file(document_id, kind)
		if file.ok and file.content is not None:
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": f"{record.document_identifier or document_id}.{kind}",
					"attached_to_doctype": RECEIVED,
					"attached_to_name": record.name,
					"is_private": 1,
					"content": file.content,
				}
			).insert(ignore_permissions=True)

	if settings.get("auto_create_purchase_invoice") and record.supplier:
		create_purchase_invoice(record.name)
	return record.name


def match_supplier(peppol_id: str | None, vat: str | None) -> str | None:
	if peppol_id:
		name = frappe.db.get_value("Supplier", {"einvoice_peppol_id": peppol_id}, "name")
		if name:
			return name
	if vat:
		return frappe.db.get_value("Supplier", {"tax_id": vat}, "name")
	return None


def create_purchase_invoice(name: str) -> str:
	"""Draft a Purchase Invoice from a received document. The draft is for review: it is never
	submitted here."""
	record = frappe.get_doc(RECEIVED, name)
	if record.purchase_invoice:
		return record.purchase_invoice
	if not record.supplier:
		frappe.throw(f"Set the Supplier on {name} first: no supplier matched the sender.")
	settings = frappe.get_doc("E Invoicing Settings", record.company)
	document = json.loads(record.document_json or "{}")
	notes = []

	is_return = cstr(document.get("document_type")) in RETURN_TYPES
	sign = -1 if is_return else 1
	pi = frappe.new_doc("Purchase Invoice")
	pi.company = record.company
	pi.supplier = record.supplier
	pi.bill_no = record.document_identifier
	pi.bill_date = record.issue_date
	pi.posting_date = today()
	pi.set_posting_time = 1
	pi.currency = document.get("document_currency") or record.currency
	if pi.currency != "AED" and document.get("currency_exchange_rate"):
		pi.conversion_rate = flt(document["currency_exchange_rate"])
	if document.get("due_date") and not is_return:
		pi.due_date = getdate(document["due_date"])
	pi.einvoice_received = record.name
	if pi.meta.has_field("cost_center") and not pi.get("cost_center"):
		# A draft for review; some sites won't even number one without it.
		pi.cost_center = frappe.get_cached_value("Company", record.company, "cost_center")
	pi.is_return = 1 if is_return else 0
	if is_return:
		original = _original_purchase_invoice(record.supplier, document)
		if original:
			pi.return_against = original
		else:
			notes.append(
				"The original purchase invoice this credit note refers to was not found; set Return Against."
			)

	item_templates = _item_templates_by_category(settings)
	for line in document.get("invoice_lines") or []:
		item_code, how = _match_item(line, record.supplier, settings)
		if not item_code:
			notes.append(f"Line {line.get('id')}: no item matched and no Item for Unmatched Lines is set.")
			continue
		if how:
			notes.append(f"Line {line.get('id')} ({line.get('name')}): {how}.")
		row = {
			"item_code": item_code,
			"item_name": cstr(line.get("name"))[:140] or None,
			"description": cstr(line.get("description") or line.get("name")) or None,
			"qty": sign * flt(line.get("quantity")),
			"rate": flt(line.get("unit_price")),
		}
		uom = _uom_for(line.get("uom"), settings)
		if uom:
			row["uom"] = uom
		category = line.get("tax_category")
		if category and category != "S":
			template = item_templates.get(category)
			if template:
				row["item_tax_template"] = template
			else:
				notes.append(
					f"Line {line.get('id')}: VAT category {category} has no Item Tax Template mapped."
				)
		pi.append("items", row)

	if not pi.items:
		frappe.throw(
			"None of the lines could be matched to an item. Set an Item for Unmatched Lines in E Invoicing Settings."
		)

	if settings.get("purchase_taxes_template") and any(
		(l.get("tax_category") == "S") for l in document.get("invoice_lines") or []
	):
		pi.taxes_and_charges = settings.purchase_taxes_template
		pi.set_taxes()

	pi.flags.ignore_mandatory = True  # a draft for review; ERPNext asks again on submit
	pi.insert(ignore_permissions=True)

	totals = document.get("invoice_totals") or {}
	expected = flt(totals.get("payable_amount") or totals.get("tax_inclusive_amount"))
	if expected and abs(abs(flt(pi.grand_total)) - expected) > 0.01:
		notes.append(
			f"The draft totals {abs(flt(pi.grand_total))}, the supplier's invoice {expected}. Check rates and taxes."
		)

	record.purchase_invoice = pi.name
	record.status = "Purchase Invoice Created"
	record.notes = "\n".join(notes) or None
	record.flags.ignore_permissions = True
	record.save()
	return pi.name


def _original_purchase_invoice(supplier: str, document: dict) -> str | None:
	for reference in document.get("document_references") or []:
		bill_no = reference.get("document_identifier")
		if bill_no:
			name = frappe.db.get_value(
				"Purchase Invoice", {"supplier": supplier, "bill_no": bill_no, "docstatus": 1}, "name"
			)
			if name:
				return name
	return None


def _match_item(line: dict, supplier: str, settings) -> tuple[str | None, str | None]:
	"""Find our item for a received line: the supplier's code, then a GTIN, then our own code
	if the supplier sent it. Falls back to the configured catch-all item."""
	supplier_code = cstr(line.get("supplier_item_code"))
	if supplier_code:
		name = frappe.db.get_value(
			"Item Supplier", {"supplier": supplier, "supplier_part_no": supplier_code}, "parent"
		)
		if name:
			return name, None
	gtin = cstr(line.get("standard_item_id"))
	if gtin:
		name = frappe.db.get_value("Item Barcode", {"barcode": gtin}, "parent")
		if name:
			return name, None
	buyer_code = cstr(line.get("buyers_item_code"))
	if buyer_code and frappe.db.exists("Item", buyer_code):
		return buyer_code, None
	fallback = settings.get("purchase_item")
	if fallback:
		return fallback, f"booked to {fallback}, no item matched"
	return None, None


def _uom_for(code: str | None, settings) -> str | None:
	if not code or not settings.get("uom_code_list"):
		return None
	try:
		from erpnext.edi.doctype.code_list.code_list import get_docnames_for
	except ImportError:
		return None
	names = get_docnames_for(settings.uom_code_list, "UOM", code)
	return names[0] if names else None


def _item_templates_by_category(settings) -> dict[str, str]:
	templates = {}
	for row in settings.tax_treatments:
		if row.template_type == "Item Tax Template":
			templates.setdefault(codes.code_of(row.tax_category), row.template)
	return templates

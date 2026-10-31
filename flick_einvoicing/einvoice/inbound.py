"""Invoices suppliers send this participant over Peppol.

Each one lands as an E Invoice Received record, with Flick's PDF and UBL attached. A draft
Purchase Invoice can then be made from it, from our Purchase Receipts or Purchase Order when the
supplier quotes them. A line no item matches goes on a placeholder item (the company's Item for
Unmatched Lines, else the app's own), and the invoice can't be submitted until someone picks our
item for it; with no placeholder at all, it stays a line without an item, booked as an expense,
where the site allows that. The draft is then brought to the supplier's figures (see reconcile).
What can't be matched (supplier, items, UOMs, prices) is listed on the record for whoever reviews
the draft. Nothing here submits anything. Submitting the reviewed invoice remembers which of our
items each supplier code turned out to be.
"""

import json

import frappe
from frappe.utils import cint, cstr, flt, getdate, now, today
from frappe.utils.html_utils import sanitize_html

from flick_einvoicing.einvoice import codes, reconcile
from flick_einvoicing.providers import get_client
from flick_einvoicing.providers.flick import INCOMING

RECEIVED = "E Invoice Received"
# The app's own item for lines no item of ours matches; install.ensure_placeholder_item makes it.
PLACEHOLDER_CODE = "E-INVOICE-UNMATCHED"
PLACEHOLDER_KEY = "flick_einvoicing_unmatched_item"
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
			"sender_name": _fit(counterparty.get("name") or seller.get("legal_name")),
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


def match_waiting(doc, method=None):
	"""Supplier on_update: invoices that came in before their sender was a supplier here wait
	without one. They get this supplier once its Peppol ID or TRN matches the sender's."""
	if doc.has_value_changed("einvoice_peppol_id") or doc.has_value_changed("tax_id"):
		link_waiting(doc.get("einvoice_peppol_id"), doc.get("tax_id"))


def link_waiting(peppol_id: str | None, vat: str | None) -> int:
	"""Match the received invoices still waiting for a supplier from this Peppol ID or TRN."""
	senders = {
		field: value
		for field, value in (("sender_peppol_id", cstr(peppol_id).strip()), ("sender_vat", cstr(vat).strip()))
		if value
	}
	if not senders:
		return 0
	linked = 0
	for record in frappe.get_all(
		RECEIVED,
		filters={"status": "New", "supplier": ("is", "not set")},
		or_filters=senders,
		fields=["name", "sender_peppol_id", "sender_vat"],
	):
		supplier = match_supplier(record.sender_peppol_id, record.sender_vat)
		if supplier:
			frappe.db.set_value(RECEIVED, record.name, "supplier", supplier)
			linked += 1
	return linked


def create_purchase_invoice(name: str) -> str:
	"""Draft a Purchase Invoice from a received document. When the supplier quotes our receipt,
	their delivery note or our purchase order, the draft is made from those, so they show as
	billed. The draft is for review: it is never submitted here."""
	record = frappe.get_doc(RECEIVED, name)
	if record.purchase_invoice:
		return record.purchase_invoice
	if not record.supplier:
		# The supplier may have been given the sender's Peppol ID or TRN since it came in.
		supplier = match_supplier(record.sender_peppol_id, record.sender_vat)
		if supplier:
			record.db_set("supplier", supplier)
	if not record.supplier:
		frappe.throw(
			f"No supplier matches the sender of {name} (Peppol ID {record.sender_peppol_id or 'none'}, "
			f"TRN {record.sender_vat or 'none'}). Use Create Supplier on it, or set its Supplier by hand.",
			title="Supplier not found",
		)
	settings = frappe.get_doc("E Invoicing Settings", record.company)
	document = json.loads(record.document_json or "{}")
	notes = []

	is_return = cstr(document.get("document_type")) in RETURN_TYPES
	sign = -1 if is_return else 1
	currency = document.get("document_currency") or record.currency
	pi = None if is_return else _from_receipts_and_orders(record, document, currency, notes)
	mapped = pi is not None
	ordered = list(pi.items) if mapped else []  # our receipt and order lines; each is billed once
	if mapped:
		pi.set("items", [])
	else:
		pi = frappe.new_doc("Purchase Invoice")
	pi.company = record.company
	pi.supplier = record.supplier
	pi.bill_no = record.document_identifier
	pi.bill_date = record.issue_date
	pi.posting_date = today()
	pi.set_posting_time = 1
	pi.currency = currency
	if document.get("due_date") and not is_return:
		pi.due_date = getdate(document["due_date"])
		if mapped:
			# Our order's payment terms came with it; the supplier's due date stands instead.
			pi.payment_terms_template = None
			pi.set("payment_schedule", [])
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

	lines = document.get("invoice_lines") or []
	if not lines:
		frappe.throw(f"{name} has no invoice lines.")
	item_templates = _item_templates_by_category(settings)
	same_rate = cint(frappe.db.get_single_value("Buying Settings", "maintain_same_rate"))
	quoted_order = cstr((document.get("order_reference") or {}).get("purchase_order_id")).strip()
	placeholder = settings.get("purchase_item") or placeholder_item()
	expense_account = frappe.get_cached_value("Company", record.company, "default_expense_account")
	for number, line in enumerate(lines, 1):
		label = f"Line {line.get('id')}"
		item_code = _match_item(line, record.supplier)
		row = _claim(ordered, line, item_code, quoted_order)
		if row:
			_bill(row, line, same_rate, settings, notes)
			pi.append("items", row)
		else:
			if mapped:
				notes.append(f"{label} ({line.get('name')}) is on none of the receipts or orders it quotes.")
			if not item_code and placeholder:
				notes.append(f"{label} ({line.get('name')}): no item matched; on {placeholder} until our item is chosen.")
			elif not item_code:
				notes.append(
					f"{label} ({line.get('name')}): no item matched; kept without an item, booked to "
					f"{expense_account or 'no account yet'}. Link our item if it is one."
				)
			row = pi.append(
				"items",
				{
					"item_code": item_code or placeholder,
					"item_name": _fit(line.get("name") or line.get("description")) or label,
					"description": cstr(line.get("description") or line.get("name")) or None,
					"qty": sign * flt(line.get("quantity")),
					"rate": reconcile.unit_rate(line),
				},
			)
			uom = _uom_for(line.get("uom"), settings)
			if uom:
				row.uom = uom
			if not row.item_code:
				# ERPNext books a line without an item as an expense: it needs a unit and an account.
				row.uom = row.get("uom") or ("Nos" if frappe.db.exists("UOM", "Nos") else None)
				row.stock_uom = row.uom
				row.conversion_factor = 1
				row.expense_account = expense_account
				row.cost_center = pi.get("cost_center")
		row.einvoice_supplier_item_code = cstr(line.get("supplier_item_code")) or None
		row.einvoice_line_no = number
		category = line.get("tax_category")
		if category and category != "S" and not row.get("item_tax_template"):
			template = item_templates.get(category)
			if template:
				row.item_tax_template = template
			else:
				notes.append(f"{label}: VAT category {category} has no Item Tax Template mapped.")

	_set_exchange_rate(pi, document, notes)

	# A receipt or order brings its own taxes; otherwise the configured template does, else the
	# company's default, as on a purchase invoice made by hand.
	template = settings.get("purchase_taxes_template") or frappe.db.get_value(
		"Purchase Taxes and Charges Template", {"company": record.company, "is_default": 1, "disabled": 0}, "name"
	)
	if not pi.get("taxes") and template and any((l.get("tax_category") == "S") for l in lines):
		pi.taxes_and_charges = template
		pi.set_taxes()

	_check_payable_currency(pi)
	pi.flags.ignore_mandatory = True  # a draft for review; ERPNext asks again on submit
	try:
		pi.insert(ignore_permissions=True)
	except Exception:
		if len(pi.items) < len(lines):
			_refuse_lost_lines(len(lines) - len(pi.items))
		raise
	if len(pi.items) < len(lines):
		_refuse_lost_lines(len(lines) - len(pi.items))

	# Rates are now as this site holds them: bring the totals to the supplier's.
	if mapped and (flt(pi.discount_amount) or flt(pi.additional_discount_percentage)):
		ours = (
			f"{flt(pi.additional_discount_percentage):g}%"
			if flt(pi.additional_discount_percentage)
			else f"{flt(pi.discount_amount):,.2f}"
		)
		notes.append(
			f"Our receipt or order gives an invoice discount of {ours}; the draft takes the supplier's "
			"invoice-level allowances instead. Check them against the order."
		)
	result = reconcile.apply(pi, document)
	pi.flags.ignore_mandatory = True
	pi.save(ignore_permissions=True)
	notes += reconcile.explain(pi, result)
	notes += reconcile.differences(pi, document, lines=False)

	record.purchase_invoice = pi.name
	record.status = "Purchase Invoice Created"
	record.notes = "\n".join(notes) or None
	record.flags.ignore_permissions = True
	record.save()
	return pi.name


def _set_exchange_rate(pi, document: dict, notes: list):
	"""The supplier's exchange rate, at which the e-invoice states its AED amounts. ERPNext refuses
	to bill stock received at another rate unless Buying Settings adjust the landed cost to the
	invoice, so a draft billing such a receipt keeps the receipt's rate, and says so."""
	rate = flt(document.get("currency_exchange_rate"))
	if not rate or pi.currency == frappe.get_cached_value("Company", pi.company, "default_currency"):
		return
	receipts = {row.purchase_receipt for row in pi.items if row.get("purchase_receipt")}
	if receipts and not cint(
		frappe.db.get_single_value("Buying Settings", "set_landed_cost_based_on_purchase_invoice_rate")
	):
		held = [
			receipt
			for receipt in frappe.get_all(
				"Purchase Receipt",
				filters={"name": ("in", list(receipts)), "currency": pi.currency},
				fields=["name", "conversion_rate"],
			)
			if flt(receipt.conversion_rate) and flt(receipt.conversion_rate) != rate
		]
		if held:
			pi.conversion_rate = flt(held[0].conversion_rate)
			notes.append(
				f"The supplier's exchange rate is {rate:g}; Purchase Receipt {held[0].name} is at "
				f"{pi.conversion_rate:g}, which the draft keeps: ERPNext bills received stock at the receipt's "
				"rate unless Buying Settings > Set Landed Cost Based on Purchase Invoice Rate is on. The AED "
				"amounts then differ from the e-invoice's."
			)
			return
	pi.conversion_rate = rate


def _check_payable_currency(pi):
	"""ERPNext books an invoice in another currency only to a payable account in that currency,
	unless Accounts Settings allow it. Say what to set up, rather than ERPNext's own message."""
	from erpnext.accounts.party import get_party_account, get_party_gle_currency

	if pi.currency == frappe.get_cached_value("Company", pi.company, "default_currency") or cint(
		frappe.db.get_single_value("Accounts Settings", "allow_multi_currency_invoices_against_single_party_account")
	):
		return
	if get_party_gle_currency("Supplier", pi.supplier, pi.company):
		return  # booked before: ERPNext's message names the currency it was booked in
	account = pi.get("credit_to") or get_party_account("Supplier", pi.supplier, pi.company)
	currency = frappe.get_cached_value("Account", account, "account_currency") if account else None
	if currency and currency != pi.currency:
		frappe.throw(
			f"{pi.supplier} bills in {pi.currency}, but its payable account {account} is in {currency}. "
			f"Add a {pi.currency} payable account for {pi.company} under Accounts on the supplier, "
			"then create the draft again.",
			title=f"No {pi.currency} payable account",
		)


def _fit(text, length: int = 140) -> str:
	"""Supplier text cut to a Data field's length as Frappe will store it: a value that looks like
	HTML is escaped on save, which can take it past the field's length."""
	text = cstr(text)[:length]
	while (over := len(sanitize_html(text)) - length) > 0:
		text = text[: -max(1, over // 5)]
	return text


def placeholder_item() -> str | None:
	"""The app's own item for unmatched lines, made on install and migrate."""
	name = frappe.db.get_default(PLACEHOLDER_KEY)
	return name if name and frappe.db.exists("Item", name) else None


def _placeholders(company: str) -> set[str]:
	return {frappe.db.get_value("E Invoicing Settings", company, "purchase_item"), placeholder_item()} - {None}


def _refuse_lost_lines(lost: int):
	# A site hook dropped lines without an item code as the draft saved (a blank-row clean-up).
	# Refuse the draft rather than leave the supplier's lines out of it.
	frappe.throw(
		f"{lost} of the supplier's lines have no item of ours, and this site removes purchase invoice "
		"lines without an item code as they are saved. Set an Item for Unmatched Lines in E Invoicing "
		"Settings, or give the supplier's codes our items (Supplier Items on the item)."
	)


def _from_receipts_and_orders(record, document, currency: str, notes: list):
	"""A draft made from the receipts and orders the supplier quotes, as Get Items From does in
	the desk, or None when they quote none of ours that still waits to be billed."""
	from erpnext.buying.doctype.purchase_order.purchase_order import (
		make_purchase_invoice as from_order,
	)
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		make_purchase_invoice as from_receipt,
	)

	pi, used = None, []
	for doctype, name in _purchase_sources(record, document):
		if frappe.db.get_value(doctype, name, "currency") != currency:
			notes.append(f"{doctype} {name} is not in {currency}, the invoice's currency; not used.")
			continue
		make = from_receipt if doctype == "Purchase Receipt" else from_order
		try:
			pi = make(name, target_doc=pi)
		except frappe.ValidationError as e:
			notes.append(f"{doctype} {name} could not be used: {cstr(e)}")
			continue
		used.append(f"{doctype} {name}")
	if not used:
		return None
	notes.insert(0, f"Made from {', '.join(used)}.")
	return pi


def _purchase_sources(record, document) -> list[tuple[str, str]]:
	"""Our Purchase Receipts and Purchase Orders this invoice bills, from what the supplier
	quotes: our receipt number, their delivery note number, our order number. Receipts come
	first; an order is used itself only while nothing has been received against it."""
	waiting = {
		"supplier": record.supplier,
		"company": record.company,
		"docstatus": 1,
		"per_billed": ("<", 100),
		"status": ("not in", ("Closed", "On Hold")),
	}
	other = document.get("other_references") or {}
	receipts = []
	receipt = cstr(other.get("receipt_document_reference")).strip()
	if receipt:
		receipts += frappe.get_all("Purchase Receipt", filters={"name": receipt, **waiting}, pluck="name")
	for note in cstr(other.get("despatch_document_reference")).split(","):
		if note.strip():
			receipts += frappe.get_all(
				"Purchase Receipt",
				filters={"supplier_delivery_note": note.strip(), **waiting},
				pluck="name",
				order_by="posting_date, name",
			)

	quoted = [cstr((document.get("order_reference") or {}).get("purchase_order_id")).strip()]
	quoted += [cstr(line.get("order_reference_id")).strip() for line in document.get("invoice_lines") or []]
	quoted = list(dict.fromkeys(q for q in quoted if q))
	found = set(frappe.get_all("Purchase Order", filters={"name": ("in", quoted), **waiting}, pluck="name")) if quoted else set()
	orders = [order for order in quoted if order in found]
	received = set()
	if orders:
		rows = frappe.get_all(
			"Purchase Receipt Item",
			filters={"purchase_order": ("in", orders), "docstatus": 1},
			fields=["parent", "purchase_order"],
			distinct=True,
		)
		received = {row.purchase_order for row in rows}
		against = sorted({row.parent for row in rows})
		if against:
			receipts += frappe.get_all(
				"Purchase Receipt", filters={"name": ("in", against), **waiting}, pluck="name", order_by="posting_date, name"
			)
	return [("Purchase Receipt", name) for name in dict.fromkeys(receipts)] + [
		("Purchase Order", order) for order in orders if order not in received
	]


def _claim(ordered: list, line: dict, item_code: str | None, quoted_order: str):
	"""Take the receipt or order line an invoice line bills, so none is billed twice: the order
	line the supplier names, else the first one for the same item."""
	order = cstr(line.get("order_reference_id")).strip() or quoted_order
	named = _order_line(cstr(line.get("order_line_reference")), {row.get("purchase_order") for row in ordered} - {None})
	found = None
	if named:
		in_order, number = named[0] or order, named[1]
		found = next(
			(
				row
				for row in ordered
				if row.get("po_detail")
				and (not in_order or row.purchase_order == in_order)
				and frappe.db.get_value("Purchase Order Item", row.po_detail, "idx") == number
			),
			None,
		)
	if not found and item_code:
		found = next(
			(
				row
				for row in ordered
				if row.item_code == item_code and (not order or row.get("purchase_order") in (order, None))
			),
			None,
		)
	if found:
		ordered.remove(found)
	return found


def _order_line(reference: str, orders: set[str]) -> tuple[str | None, int] | None:
	"""The line number on one of our orders that the supplier wrote: "3", or the order number
	followed by it, like "PUR-ORD-2026-00012-3". None when it is neither."""
	reference = reference.strip()
	if reference.isdigit():
		return None, int(reference)
	for order in orders:
		if reference.startswith(order):
			rest = reference[len(order) :].strip(" -/#:.")
			if rest.isdigit():
				return order, int(rest)
	return None


def _bill(row, line: dict, same_rate: int, settings, notes: list):
	"""Bill one of our receipt or order lines for what the supplier invoices on it, never for
	more than waits to be billed there: ERPNext refuses to save that, so the reviewer decides."""
	label = f"Line {line.get('id')}"
	source = row.get("purchase_receipt") or row.get("purchase_order")
	qty, rate = flt(line.get("quantity")), reconcile.unit_rate(line)
	number = reconcile.written
	if qty > flt(row.qty):
		notes.append(
			f"{label}: bills {number(qty)}, but {number(row.qty)} wait to be billed on {source}; the draft bills {number(row.qty)}."
		)
	else:
		row.qty = qty
	uom = _uom_for(line.get("uom"), settings)
	if uom and uom != row.uom:
		notes.append(f"{label}: the supplier bills in {uom}, {source} is in {row.uom}; check the quantity.")
	precision = row.precision("rate")
	if flt(rate, precision) == flt(row.rate, precision):
		return  # the same price, as far as this site holds prices
	theirs, ours = number(rate), number(row.rate)
	if same_rate:
		notes.append(f"{label}: the supplier charges {theirs}, {source} {ours}; kept ours, as Buying Settings keep the same rate.")
	elif rate > flt(row.rate):
		notes.append(f"{label}: the supplier charges {theirs}, more than the {ours} on {source}; kept ours.")
	else:
		notes.append(f"{label}: the supplier charges {theirs}, less than the {ours} on {source}; billed theirs.")
		row.update({"rate": rate, "price_list_rate": rate, "discount_percentage": 0, "discount_amount": 0})


def check_unmatched_lines(doc, method=None):
	"""No purchase invoice is submitted while a line is on a placeholder: it only holds a received
	line until someone picks our item. The app's own placeholder is held back on every purchase
	invoice; a company's chosen one, on those made from received e-invoices."""
	held = {placeholder_item()} - {None}
	if doc.get("einvoice_received"):
		held = _placeholders(doc.company)
	waiting = [row for row in doc.items if row.item_code in held]
	if waiting:
		rows = "".join(
			f"<li>Row {row.idx}: {frappe.utils.escape_html(row.item_name or row.description or '')}</li>"
			for row in waiting
		)
		frappe.throw(
			f"Choose our item for these lines before submitting; {waiting[0].item_code} only holds them until then:<ul>{rows}</ul>",
			title="Lines still to match",
		)


def remember_item_codes(doc, method=None):
	"""On submitting a purchase invoice made from a received e-invoice, remember which of our
	items each supplier code turned out to be, so the next invoice matches by itself. The
	reviewer's choice replaces an earlier one. A supplier without a Peppol ID gets the sender's.
	Never stops the submit."""
	if not doc.get("einvoice_received"):
		return
	held = _placeholders(doc.company)
	learned = []
	frappe.db.savepoint("flick_remember")
	try:
		change = _remember_sender(doc)
	except Exception:
		frappe.db.rollback(save_point="flick_remember")
		frappe.log_error(f"E-invoice: remembering the sender's Peppol ID for {doc.supplier}")
		change = None
	if change:
		learned.append(change)
	for row in doc.items:
		code = cstr(row.get("einvoice_supplier_item_code")).strip()
		if not code or not row.item_code or row.item_code in held:
			continue
		frappe.db.savepoint("flick_remember")
		try:
			change = _remember(doc.supplier, code, row.item_code)
		except Exception:
			frappe.db.rollback(save_point="flick_remember")
			frappe.log_error(f"E-invoice: remembering {doc.supplier}'s code {code} as {row.item_code}")
			continue
		if change:
			learned.append(change)
	if learned and frappe.db.exists(RECEIVED, doc.einvoice_received):
		frappe.get_doc(RECEIVED, doc.einvoice_received).add_comment(
			"Info", "Remembered for the next invoice: " + "; ".join(learned)
		)


def _remember_sender(doc) -> str | None:
	"""The sender's Peppol ID, on a supplier that has none and was matched by TRN or by hand, so
	its next invoices match by themselves; those already waiting get the supplier now. Never one
	another supplier has. Returns what changed."""
	sender = cstr(frappe.db.get_value(RECEIVED, doc.einvoice_received, "sender_peppol_id")).strip()
	if not sender or frappe.db.get_value("Supplier", doc.supplier, "einvoice_peppol_id"):
		return None
	if frappe.db.exists("Supplier", {"einvoice_peppol_id": sender, "name": ("!=", doc.supplier)}):
		return None
	frappe.db.set_value("Supplier", doc.supplier, "einvoice_peppol_id", sender)
	waiting = link_waiting(sender, None)
	also = f" ({waiting} more of its invoices now have this supplier)" if waiting else ""
	return f"{doc.supplier}'s Peppol ID is {sender}{also}"


def _remember(supplier: str, code: str, item_code: str) -> str | None:
	"""Point the supplier's code at our item, in the item's Supplier Items. Returns what changed."""
	rows = frappe.get_all(
		"Item Supplier",
		filters={"parenttype": "Item", "supplier": supplier, "supplier_part_no": code},
		fields=["name", "parent"],
	)
	if any(row.parent == item_code for row in rows):
		return None
	for row in rows:
		frappe.db.delete("Item Supplier", {"name": row.name})
		frappe.clear_document_cache("Item", row.parent)
	last = frappe.get_all(
		"Item Supplier", filters={"parenttype": "Item", "parent": item_code}, pluck="idx", order_by="idx desc", limit=1
	)
	stamp = now()
	frappe.get_doc(
		{
			"doctype": "Item Supplier",
			"parenttype": "Item",
			"parentfield": "supplier_items",
			"parent": item_code,
			"idx": (last[0] if last else 0) + 1,
			"supplier": supplier,
			"supplier_part_no": code,
			"owner": frappe.session.user,
			"modified_by": frappe.session.user,
			"creation": stamp,
			"modified": stamp,
		}
	).db_insert()
	frappe.clear_document_cache("Item", item_code)
	was = f" (was {', '.join(row.parent for row in rows)})" if rows else ""
	return f"{code} is {item_code}{was}"


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


def _match_item(line: dict, supplier: str) -> str | None:
	"""Find our item for a received line: the supplier's code, then a GTIN, then our own code
	if the supplier sent it. Never by name: theirs and ours rarely agree."""
	supplier_code = cstr(line.get("supplier_item_code"))
	if supplier_code:
		name = frappe.db.get_value(
			"Item Supplier", {"supplier": supplier, "supplier_part_no": supplier_code}, "parent"
		)
		if name:
			return name
	gtin = cstr(line.get("standard_item_id"))
	if gtin:
		name = frappe.db.get_value("Item Barcode", {"barcode": gtin}, "parent")
		if name:
			return name
	buyer_code = cstr(line.get("buyers_item_code"))
	if buyer_code and frappe.db.exists("Item", buyer_code):
		return buyer_code
	return None


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

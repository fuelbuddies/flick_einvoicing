"""Fixtures and a fake Flick for the flow tests. Everything is created inside the test
transaction and rolled back by FrappeTestCase."""

import json
import re
from types import SimpleNamespace

import frappe
from frappe.utils import add_days, today

from flick_einvoicing.providers.flick import Response

PARTICIPANT = "0190a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
BUYER_PEPPOL = "0235:1066001239"
BUYER_TRN = "100000123400003"
SUPPLIER_PEPPOL = "0235:1020260015"
SUPPLIER_TRN = "102026001500003"
DOC_ID = "01a18392-5f0a-7853-a3b6-1459869ff5e4"
GTIN = "4006381333931"
PREFIX = "_Test EInv"


def uae_company():
	companies = frappe.get_all(
		"Company",
		filters={"country": "United Arab Emirates", "default_currency": "AED"},
		fields=[
			"name",
			"abbr",
			"default_income_account",
			"default_expense_account",
			"cost_center",
			"default_receivable_account",
			"default_payable_account",
			"tax_id",
		],
		order_by="creation",
	)
	if frappe.db.exists("DocType", "Trading Branch"):
		# techountant_trading stamps the user's default branch on every invoice; use a company
		# with a branch of its own so the invoice can carry a branch that fits.
		with_branch = [c for c in companies if frappe.db.exists("Trading Branch", {"company": c.name})]
		companies = with_branch or companies
	return companies[0] if companies else None


def _branch_of(company, doctype="Sales Invoice"):
	if not frappe.get_meta(doctype).has_field("tt_branch"):
		return {}
	branch = frappe.db.get_value("Trading Branch", {"company": company.name}, "name")
	return {"tt_branch": branch} if branch else {}


def vat_account(company):
	account = frappe.db.get_value(
		"Account", {"company": company.name, "account_type": "Tax", "is_group": 0, "disabled": 0}, "name"
	)
	if account:
		return account
	parent = frappe.db.get_value(
		"Account", {"company": company.name, "account_name": "Duties and Taxes", "is_group": 1}, "name"
	)
	return _account(company, f"{PREFIX} VAT", parent, "Tax")


def _account(company, name, parent, account_type):
	existing = frappe.db.get_value("Account", {"company": company.name, "account_name": name}, "name")
	if existing:
		return existing
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"parent_account": parent,
				"company": company.name,
				"account_type": account_type,
			}
		)
		.insert()
		.name
	)


def make_fixtures(company) -> SimpleNamespace:
	vat = vat_account(company)

	vat5 = frappe.get_doc(
		{
			"doctype": "Sales Taxes and Charges Template",
			"title": f"{PREFIX} VAT 5%",
			"company": company.name,
			"taxes": [
				{"charge_type": "On Net Total", "account_head": vat, "rate": 5, "description": "VAT 5%"}
			],
		}
	).insert()
	vat0 = frappe.get_doc(
		{
			"doctype": "Sales Taxes and Charges Template",
			"title": f"{PREFIX} No VAT",
			"company": company.name,
			"taxes": [
				{"charge_type": "On Net Total", "account_head": vat, "rate": 0, "description": "VAT 0%"}
			],
		}
	).insert()
	purchase_vat = frappe.get_doc(
		{
			"doctype": "Purchase Taxes and Charges Template",
			"title": f"{PREFIX} Purchase VAT",
			"company": company.name,
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": vat,
					"rate": 5,
					"description": "VAT 5%",
					"category": "Total",
					"add_deduct_tax": "Add",
				}
			],
		}
	).insert()

	def item_tax_template(title):
		return (
			frappe.get_doc(
				{
					"doctype": "Item Tax Template",
					"title": f"{PREFIX} {title}",
					"company": company.name,
					"taxes": [{"tax_type": vat, "tax_rate": 0}],
				}
			)
			.insert()
			.name
		)

	zero = item_tax_template("Zero")
	exempt = item_tax_template("Exempt")
	reverse = item_tax_template("Reverse")

	income_group = frappe.db.get_value(
		"Account",
		{"company": company.name, "root_type": "Income", "is_group": 1, "parent_account": ("is", "set")},
		"name",
	)
	freight = _account(company, f"{PREFIX} Freight", income_group, "Chargeable")
	receivable_group = frappe.db.get_value("Account", company.default_receivable_account, "parent_account")
	usd_debtors = frappe.db.get_value(
		"Account", {"company": company.name, "account_name": f"{PREFIX} Debtors USD"}, "name"
	) or (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": f"{PREFIX} Debtors USD",
				"parent_account": receivable_group,
				"company": company.name,
				"account_type": "Receivable",
				"account_currency": "USD",
			}
		)
		.insert()
		.name
	)

	customer = _customer(f"{PREFIX} Buyer", BUYER_PEPPOL, BUYER_TRN, "Company")
	frappe.db.set_value(
		"Customer",
		customer,
		{"einvoice_trade_licence": "1066001239", "einvoice_trade_licence_authority": "Dubai DED"},
	)
	address = _address("Customer", customer, "Ras Al Khaimah")
	shipping_abroad = _address(
		"Customer", customer, None, country="United Kingdom", city="London", title=f"{customer} London"
	)
	walk_in = _customer(f"{PREFIX} Walk In", None, None, "Individual")
	walk_in_address = _address("Customer", walk_in, "Dubai")
	no_peppol = _customer(f"{PREFIX} No Peppol", None, BUYER_TRN, "Company")
	no_peppol_address = _address("Customer", no_peppol, "Dubai")
	overseas = _customer(f"{PREFIX} Overseas", None, None, "Company")
	frappe.db.set_value("Customer", overseas, "einvoice_routing", "Export buyer not on Peppol")
	overseas_address = _address("Customer", overseas, None, country="United Kingdom", city="London")

	supplier = frappe.db.get_value("Supplier", {"supplier_name": f"{PREFIX} Supplier"}, "name")
	if not supplier:
		supplier = (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": f"{PREFIX} Supplier",
					"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
					or "All Supplier Groups",
					"tax_id": SUPPLIER_TRN,
					"einvoice_peppol_id": SUPPLIER_PEPPOL,
					"einvoice_self_billing": 1,
					"einvoice_trade_licence": "1020260015",
					"einvoice_trade_licence_authority": "Fujairah Municipality",
				}
			)
			.insert()
			.name
		)
	supplier_address = _address("Supplier", supplier, "Fujairah")

	item = _item(f"{PREFIX} Item", "Probe widget")
	if not frappe.db.exists("Customs Tariff Number", "851713"):
		frappe.get_doc(
			{"doctype": "Customs Tariff Number", "tariff_number": "851713", "description": "Phones"}
		).insert()
	phone = _item(
		f"{PREFIX} Phone",
		"Smart phone",
		barcodes=[{"barcode": GTIN, "barcode_type": "EAN"}],
		customs_tariff_number="851713",
		country_of_origin="China",
		einvoice_sac_code="998313",
		supplier_items=[{"supplier": supplier, "supplier_part_no": "SUP-PHONE"}],
	)

	_link_uom("Nos", "H87")
	cash = "_Test EInv Cash"
	if not frappe.db.exists("Mode of Payment", cash):
		frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": cash, "type": "Cash"}).insert()

	settings_name = company.name
	if frappe.db.exists("E Invoicing Settings", settings_name):
		frappe.delete_doc("E Invoicing Settings", settings_name, force=True)
	frappe.get_doc(
		{
			"doctype": "E Invoicing Settings",
			"company": company.name,
			"enabled": 1,
			"environment": "Pre-Production",
			"provider": "Flick",
			"base_url": "https://flick.invalid",
			"api_key": "flick_ae_test_key_never_real",
			"participant_id": PARTICIPANT,
			"submission_mode": "Batch",
			"start_date": "2020-01-01",
			"skip_b2c": 1,
			"discount_style": "Document-level allowance",
			"export_emirate": "Dubai",
			"attach_pdf": 0,
			"payment_means_code": "30 - Credit transfer",
			"payment_account_id": "AE070331234567890123456",
			"payment_account_name": "Test Seller LLC",
			"payment_modes": [{"mode_of_payment": cash, "payment_means_code": "10 - Cash"}],
			"charge_accounts": [
				{
					"account": freight,
					"reason_code": "FC",
					"reason": "Freight",
					"tax_category": "S - Standard rated (5%)",
				}
			],
			"uom_code_list": "UN/ECE Recommendation 20",
			"ack_events": 0,
			"receive_invoices": 1,
			"purchase_item": item,
			"purchase_taxes_template": purchase_vat.name,
			"tax_treatments": [
				{
					"template_type": "Sales Taxes and Charges Template",
					"template": vat5.name,
					"tax_category": "S - Standard rated (5%)",
				},
				{
					"template_type": "Purchase Taxes and Charges Template",
					"template": purchase_vat.name,
					"tax_category": "S - Standard rated (5%)",
				},
				{
					"template_type": "Sales Taxes and Charges Template",
					"template": vat0.name,
					"tax_category": "Z - Zero rated",
				},
				{"template_type": "Item Tax Template", "template": zero, "tax_category": "Z - Zero rated"},
				{
					"template_type": "Item Tax Template",
					"template": exempt,
					"tax_category": "E - Exempt",
					"exemption_reason_code": "DL8.46.1 - Certain financial services",
				},
				{
					"template_type": "Item Tax Template",
					"template": reverse,
					"tax_category": "AE - Reverse charge",
					"reverse_charge_item_type": "DL8.48.8.2 - Electronic devices",
				},
			],
		}
	).insert()

	return SimpleNamespace(
		company=company,
		vat=vat,
		vat5=vat5.name,
		vat0=vat0.name,
		purchase_vat=purchase_vat.name,
		zero=zero,
		exempt=exempt,
		reverse=reverse,
		freight=freight,
		usd_debtors=usd_debtors,
		customer=customer,
		address=address,
		shipping_abroad=shipping_abroad,
		walk_in=walk_in,
		walk_in_address=walk_in_address,
		no_peppol=no_peppol,
		no_peppol_address=no_peppol_address,
		overseas=overseas,
		overseas_address=overseas_address,
		supplier=supplier,
		supplier_address=supplier_address,
		cash=cash,
		item=item,
		phone=phone,
	)


def _customer(name, peppol, trn, customer_type):
	if frappe.db.exists("Customer", name):
		frappe.db.set_value(
			"Customer", name, {"einvoice_peppol_id": peppol, "tax_id": trn, "customer_type": customer_type}
		)
		return name
	return (
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_type": customer_type,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
				or "All Customer Groups",
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
				"tax_id": trn,
				"einvoice_peppol_id": peppol,
			}
		)
		.insert()
		.name
	)


def _address(
	link_doctype, link_name, emirate, country="United Arab Emirates", city="Ras Al Khaimah", title=None
):
	return (
		frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": title or link_name,
				"address_type": "Billing",
				"address_line1": "68 East Milton Street",
				"city": city,
				"country": country,
				"emirate": emirate,
				"links": [{"link_doctype": link_doctype, "link_name": link_name}],
			}
		)
		.insert()
		.name
	)


def _item(code, name, **extra):
	"""The item's real name: sites that name items by series ignore the item code."""
	existing = frappe.db.get_value("Item", {"item_code": code}, "name")
	if existing:
		return existing
	return (
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": name,
				"description": "<p>Throwaway <b>test</b> item</p>",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				**extra,
			}
		)
		.insert()
		.name
	)


def _link_uom(uom, code):
	name = frappe.db.get_value("Common Code", {"code_list": "UN/ECE Recommendation 20", "common_code": code})
	doc = frappe.get_doc("Common Code", name)
	if not any(link.link_doctype == "UOM" and link.link_name == uom for link in doc.applies_to):
		# The same UOM may already point at another code on this site; unlink it there first.
		for other in frappe.get_all(
			"Dynamic Link",
			filters={"parenttype": "Common Code", "link_doctype": "UOM", "link_name": uom},
			fields=["name", "parent"],
		):
			frappe.delete_doc("Dynamic Link", other.name, force=True)
		doc.append("applies_to", {"link_doctype": "UOM", "link_name": uom})
		doc.save()


def line(fx, qty, rate, template=None, item=None, **extra):
	company = fx.company
	return {
		"item_code": item or fx.item,
		"qty": qty,
		"rate": rate,
		"uom": "Nos",
		"conversion_factor": 1,
		"income_account": company.default_income_account,
		"cost_center": company.cost_center,
		"item_tax_template": template,
		"item_tax_rate": json.dumps({fx.vat: 0}) if template else None,
		**extra,
	}


def make_invoice(
	fx,
	lines,
	customer=None,
	address=None,
	discount=None,
	submit=False,
	taxes=None,
	discount_on="Net Total",
	**values,
):
	"""lines: (qty, rate, item_tax_template or None) tuples, or ready row dicts.

	An invoice whose every line carries an item tax template gets the no-VAT invoice template:
	some sites refuse a 5% template on an invoice whose VAT comes to nothing."""
	company = fx.company
	rows = [row if isinstance(row, dict) else line(fx, *row) for row in lines]
	no_vat = taxes is None and all(row.get("item_tax_template") for row in rows)
	template = fx.vat0 if no_vat else fx.vat5
	if taxes is None:
		taxes = [
			{
				"charge_type": "On Net Total",
				"account_head": fx.vat,
				"rate": 0 if no_vat else 5,
				"description": "VAT",
			}
		]
	si = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": company.name,
			"customer": customer or fx.customer,
			"customer_address": address or fx.address,
			"posting_date": today(),
			"due_date": add_days(today(), 30),
			"set_posting_time": 1,
			"currency": "AED",
			"conversion_rate": 1,
			"debit_to": company.default_receivable_account,
			"cost_center": company.cost_center,  # some sites number invoices by cost centre
			"taxes_and_charges": template,
			"taxes": taxes,
			"items": rows,
			**_branch_of(company),
			**values,
		}
	)
	if discount:
		si.apply_discount_on = discount_on
		si.discount_amount = discount
	si.insert()
	if submit:
		si.submit()
	return si


def make_purchase_invoice(fx, lines, submit=False, **values):
	"""lines: (qty, rate, item or None)."""
	company = fx.company
	pi = frappe.get_doc(
		{
			"doctype": "Purchase Invoice",
			"company": company.name,
			"supplier": fx.supplier,
			"supplier_address": fx.supplier_address,
			"posting_date": today(),
			"due_date": add_days(today(), 30),
			"set_posting_time": 1,
			"currency": "AED",
			"conversion_rate": 1,
			"credit_to": company.default_payable_account,
			"cost_center": company.cost_center,
			"taxes_and_charges": fx.purchase_vat,
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": fx.vat,
					"rate": 5,
					"description": "VAT 5%",
					"category": "Total",
					"add_deduct_tax": "Add",
				}
			],
			"items": [
				{
					"item_code": item or fx.item,
					"qty": qty,
					"rate": rate,
					"uom": "Nos",
					"conversion_factor": 1,
					"expense_account": company.default_expense_account,
					"cost_center": company.cost_center,
				}
				for qty, rate, item in lines
			],
			**_branch_of(company, "Purchase Invoice"),
			**values,
		}
	)
	pi.insert()
	if submit:
		pi.submit()
	return pi


# A fake Flick


def response(status, body=None, **kw):
	kw.setdefault("text", json.dumps(body) if body is not None else "")
	return Response(status=status, body=body, **kw)


def accepted(document_id=DOC_ID, identifier="x", **statuses):
	data = {
		"document_id": document_id,
		"document_identifier": identifier,
		"status": "processing",
		"reporting_status": "processing",
		"exchange_status": "processing",
		"is_duplicate": False,
	}
	data.update(statuses)
	return response(
		202, {"status": "success", "message": "Document accepted for Peppol network delivery.", "data": data}
	)


def detail(reporting, exchange, errors=None, document_id=DOC_ID, **extra):
	data = {
		"id": document_id,
		"status": "failed" if "failed" in (reporting["status"], exchange["status"]) else "processing",
		"reporting": reporting,
		"exchange": exchange,
		"errors": errors or [],
		"document": {"not": "stored"},
	}
	data.update(extra)
	return response(200, {"status": "success", "data": data})


def pdf_bytes() -> bytes:
	"""A real, blank one-page PDF: Frappe parses PDFs when they are saved as files."""
	from io import BytesIO

	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=72, height=72)
	buffer = BytesIO()
	writer.write(buffer)
	return buffer.getvalue()


def binary(content: bytes, content_type="application/pdf"):
	return Response(status=200, content=content, content_type=content_type)


class FakeFlick:
	"""Answers by (method, path pattern); records every call made."""

	def __init__(self):
		self.routes: list[tuple[str, str, list]] = []
		self.calls: list[tuple[str, str, dict | None]] = []

	def on(self, method, pattern, *responses):
		self.routes.append((method, pattern, list(responses)))
		return self

	def __call__(self, method, url, body, credential=None, binary=False):
		self.calls.append((method, url, body))
		for route_method, pattern, responses in self.routes:
			if route_method == method and re.search(pattern, url):
				return responses.pop(0) if len(responses) > 1 else responses[0]
		raise AssertionError(f"Unexpected Flick call: {method} {url}")

	def called(self, method, pattern) -> int:
		return sum(1 for m, url, _ in self.calls if m == method and re.search(pattern, url))

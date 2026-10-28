"""Fixtures and a fake Flick for the flow tests. Everything is created inside the test
transaction and rolled back by FrappeTestCase."""

import json
import re
from types import SimpleNamespace

import frappe
from frappe.utils import add_days, today

from flick_einvoicing.providers.flick import Response

PARTICIPANT = "019fc2bd-5623-7e33-aeca-a95afb39541a"
BUYER_PEPPOL = "0235:1066001239"
BUYER_TRN = "100000123400003"
DOC_ID = "01a18392-5f0a-7853-a3b6-1459869ff5e4"
PREFIX = "_Test EInv"


def uae_company():
	companies = frappe.get_all(
		"Company",
		filters={"country": "United Arab Emirates", "default_currency": "AED"},
		fields=["name", "abbr", "default_income_account", "cost_center", "default_receivable_account"],
		order_by="creation",
	)
	if frappe.db.exists("DocType", "Trading Branch"):
		# techountant_trading stamps the user's default branch on every invoice; use a company
		# with a branch of its own so the invoice can carry a branch that fits.
		with_branch = [c for c in companies if frappe.db.exists("Trading Branch", {"company": c.name})]
		companies = with_branch or companies
	return companies[0] if companies else None


def _branch_of(company):
	if not frappe.get_meta("Sales Invoice").has_field("tt_branch"):
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
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": f"{PREFIX} VAT",
				"parent_account": parent,
				"company": company.name,
				"account_type": "Tax",
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

	customer = _customer(f"{PREFIX} Buyer", BUYER_PEPPOL, BUYER_TRN, "Company")
	address = _address(customer, "Ras Al Khaimah")
	walk_in = _customer(f"{PREFIX} Walk In", None, None, "Individual")
	walk_in_address = _address(walk_in, "Dubai")
	no_peppol = _customer(f"{PREFIX} No Peppol", None, BUYER_TRN, "Company")
	no_peppol_address = _address(no_peppol, "Dubai")

	item = f"{PREFIX} Item"
	if not frappe.db.exists("Item", item):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item,
				"item_name": "Probe widget",
				"description": "<p>Throwaway <b>test</b> item</p>",
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()

	_link_uom("Nos", "H87")

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
			"payment_means_code": "30 - Credit transfer",
			"payment_account_id": "AE070331234567890123456",
			"payment_account_name": "Test Seller LLC",
			"uom_code_list": "UN/ECE Recommendation 20",
			"ack_events": 0,
			"tax_treatments": [
				{
					"template_type": "Sales Taxes and Charges Template",
					"template": vat5.name,
					"tax_category": "S - Standard rated (5%)",
				},
				{"template_type": "Item Tax Template", "template": zero, "tax_category": "Z - Zero rated"},
				{
					"template_type": "Item Tax Template",
					"template": exempt,
					"tax_category": "E - Exempt",
					"exemption_reason_code": "DL8.46.1 - Certain financial services",
				},
			],
		}
	).insert()

	return SimpleNamespace(
		company=company,
		vat=vat,
		vat5=vat5.name,
		zero=zero,
		exempt=exempt,
		customer=customer,
		address=address,
		walk_in=walk_in,
		walk_in_address=walk_in_address,
		no_peppol=no_peppol,
		no_peppol_address=no_peppol_address,
		item=item,
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


def _address(customer, emirate):
	return (
		frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": customer,
				"address_type": "Billing",
				"address_line1": "68 East Milton Street",
				"city": "Ras Al Khaimah",
				"country": "United Arab Emirates",
				"emirate": emirate,
				"links": [{"link_doctype": "Customer", "link_name": customer}],
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


def make_invoice(fx, lines, customer=None, address=None, discount=None, submit=False, **values):
	"""lines: (qty, rate, item_tax_template or None)"""
	company = fx.company
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
			"taxes_and_charges": fx.vat5,
			"taxes": [
				{"charge_type": "On Net Total", "account_head": fx.vat, "rate": 5, "description": "VAT 5%"}
			],
			"items": [
				{
					"item_code": fx.item,
					"qty": qty,
					"rate": rate,
					"uom": "Nos",
					"conversion_factor": 1,
					"income_account": company.default_income_account,
					"cost_center": company.cost_center,
					"item_tax_template": template,
					"item_tax_rate": json.dumps({fx.vat: 0}) if template else None,
				}
				for qty, rate, template in lines
			],
			**_branch_of(company),
			**values,
		}
	)
	if discount:
		si.apply_discount_on = "Net Total"
		si.discount_amount = discount
	si.insert()
	if submit:
		si.submit()
	return si


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


def detail(reporting, exchange, errors=None, document_id=DOC_ID):
	return response(
		200,
		{
			"status": "success",
			"data": {
				"id": document_id,
				"status": "failed" if "failed" in (reporting["status"], exchange["status"]) else "processing",
				"reporting": reporting,
				"exchange": exchange,
				"errors": errors or [],
				"document": {"not": "stored"},
			},
		},
	)


class FakeFlick:
	"""Answers by (method, path pattern); records every call made."""

	def __init__(self):
		self.routes: list[tuple[str, str, list]] = []
		self.calls: list[tuple[str, str, dict | None]] = []

	def on(self, method, pattern, *responses):
		self.routes.append((method, pattern, list(responses)))
		return self

	def __call__(self, method, url, body):
		self.calls.append((method, url, body))
		for route_method, pattern, responses in self.routes:
			if route_method == method and re.search(pattern, url):
				return responses.pop(0) if len(responses) > 1 else responses[0]
		raise AssertionError(f"Unexpected Flick call: {method} {url}")

	def called(self, method, pattern) -> int:
		return sum(1 for m, url, _ in self.calls if m == method and re.search(pattern, url))

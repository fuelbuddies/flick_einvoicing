import base64
import json
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import make_debit_note
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, today

from flick_einvoicing.api import charge_reasons, dashboard, einvoicing_enabled, invoice_banner
from flick_einvoicing.einvoice import code_lists, inbound, service, setup
from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.payload import build_payload, check_keys, invoice_problems
from flick_einvoicing.einvoice.scope import settings_for
from flick_einvoicing.install import DASHBOARD_BLOCK, DASHBOARD_ROLES, ensure_dashboard, ensure_placeholder_item
from flick_einvoicing.providers.flick import FlickClient
from flick_einvoicing.tests.utils import (
	BUYER_PEPPOL,
	BUYER_TRN,
	DOC_ID,
	GTIN,
	PREFIX,
	SUPPLIER_PEPPOL,
	FakeFlick,
	_branch_of,
	_item,
	accepted,
	binary,
	detail,
	line,
	make_fixtures,
	make_invoice,
	make_purchase_invoice,
	pdf_bytes,
	response,
	uae_company,
)

SETTLED_OK = {"status": "reported", "settled": True, "ok": True}
DELIVERED = {"status": "delivered", "settled": True, "ok": True}
RUNNING = {"status": "processing", "settled": False}
DETAIL = rf"/documents/{DOC_ID}$"


class FlowTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		company = uae_company()
		if not company:
			raise cls.skipTest(cls, "needs a company in the United Arab Emirates keeping books in AED")
		cls.fx = make_fixtures(company)

	def settings(self, **changes):
		if changes:
			frappe.db.set_value("E Invoicing Settings", self.fx.company.name, changes)
		frappe.clear_document_cache("E Invoicing Settings", self.fx.company.name)
		return settings_for(self.fx.company.name)

	def flick(self, fake):
		return patch.object(FlickClient, "_http", side_effect=fake)

	def payload(self, invoice):
		return build_payload(invoice, self.settings())

	def problems(self, invoice):
		return invoice_problems(invoice, self.settings())

	def submitted_invoice(self, lines=((1, 1000, None),), **kw):
		si = make_invoice(self.fx, lines, submit=True, **kw)
		sub = service.active_submission("Sales Invoice", si.name)
		self.assertIsNotNone(sub, "a submission is created when the invoice is submitted")
		return si, sub


class TestDocuments(FlowTestCase):
	"""Invoices, credit notes and debit notes, as the guide's examples have them."""

	def test_minimal_invoice(self):
		si = make_invoice(self.fx, [(10, 100, None)])
		payload = self.payload(si)
		self.assertEqual(payload["document_identifier"], si.name)
		self.assertEqual(payload["document_type"], "380")
		self.assertEqual(payload["document_currency"], "AED")
		self.assertEqual(payload["due_date"], str(si.due_date))
		self.assertEqual(len(payload["flags"]), 9)
		self.assertFalse(any(payload["flags"].values()))
		self.assertRegex(payload["issue_time"], r"^\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

		line_ = payload["invoice_lines"][0]
		self.assertEqual(
			(line_["quantity"], line_["unit_price"], line_["line_extension_amount"], line_["uom"]),
			("10", "100.00", "1000.00", "H87"),
		)
		self.assertEqual((line_["tax_category"], line_["tax_percentage"]), ("S", "5"))
		self.assertEqual(line_["description"], "Throwaway test item")
		self.assertEqual(line_["supplier_item_code"], self.fx.item)

		totals = payload["invoice_totals"]
		self.assertEqual(
			(totals["tax_exclusive_amount"], totals["tax_amount"], totals["payable_amount"]),
			("1000.00", "50.00", "1050.00"),
		)
		buyer = payload["receiving_party"]
		self.assertEqual(buyer["peppol_id"], BUYER_PEPPOL)
		self.assertEqual(
			buyer["identifiers"],
			{"AE:VAT": {"value": BUYER_TRN}, "AE:TL": {"value": "1066001239", "authority_name": "Dubai DED"}},
		)
		self.assertEqual((buyer["emirates_code"], buyer["country_code"]), ("RAK", "AE"))
		means = payload["payment_means"][0]
		self.assertEqual((means["type_code"], means["payment_account_id"]), ("30", "AE070331234567890123456"))
		self.assertEqual(payload["payment_terms"][0]["amount"], "1050.00")
		check_keys(payload)

	def test_discount_as_document_level_allowance(self):
		si = make_invoice(self.fx, [(3, 40, None)], discount=20)
		payload = self.payload(si)
		line_ = payload["invoice_lines"][0]
		self.assertEqual((line_["unit_price"], line_["line_extension_amount"]), ("40.00", "120.00"))
		self.assertEqual(
			payload["allowance_charges"][0],
			{
				"charge_indicator": False,
				"reason_code": "95",
				"reason": "Discount",
				"amount": "20.00",
				"tax_category": "S",
				"tax_percentage": "5",
			},
		)
		totals = payload["invoice_totals"]
		self.assertEqual(
			(
				totals["line_extension_amount"],
				totals["allowance_total_amount"],
				totals["tax_exclusive_amount"],
			),
			("120.00", "20.00", "100.00"),
		)
		self.assertEqual((totals["tax_amount"], totals["payable_amount"]), ("5.00", "105.00"))

	def test_discount_spread_over_lines(self):
		self.settings(discount_style="Spread over lines")
		try:
			si = make_invoice(self.fx, [(3, 40, None)], discount=20)
			payload = self.payload(si)
		finally:
			self.settings(discount_style="Document-level allowance")
		line_ = payload["invoice_lines"][0]
		self.assertEqual((line_["unit_price"], line_["line_extension_amount"]), ("33.333333", "100.00"))
		self.assertNotIn("allowance_charges", payload)

	def test_negative_discount_as_document_level_charge(self):
		# FC stands in for the charge reason the client picks; any UNCL 7161 code serves here.
		self.settings(negative_discount_reason_code="FC", negative_discount_reason="Rounding")
		try:
			si = make_invoice(self.fx, [(3, 40, None)], discount=-20)
			payload = self.payload(si)
		finally:
			self.settings(negative_discount_reason_code=None, negative_discount_reason=None)
		self.assertEqual(payload["invoice_lines"][0]["line_extension_amount"], "120.00")
		self.assertEqual(
			payload["allowance_charges"],
			[
				{
					"charge_indicator": True,
					"reason_code": "FC",
					"reason": "Rounding",
					"amount": "20.00",
					"tax_category": "S",
					"tax_percentage": "5",
				}
			],
		)
		totals = payload["invoice_totals"]
		self.assertEqual(
			(
				totals["charge_total_amount"],
				totals["tax_exclusive_amount"],
				totals["tax_amount"],
				totals["payable_amount"],
			),
			("20.00", "140.00", "7.00", "147.00"),
		)
		self.assertNotIn("allowance_total_amount", totals)

	def test_negative_discount_on_grand_total_keeps_the_books_vat(self):
		"""A total rounded up through the discount: the e-invoice's VAT is the books' VAT."""
		self.settings(negative_discount_reason_code="FC")
		try:
			si = make_invoice(self.fx, [(1, 837.5, None)], discount=-0.62, discount_on="Grand Total")
			payload = self.payload(si)
		finally:
			self.settings(negative_discount_reason_code=None)
		totals = payload["invoice_totals"]
		self.assertEqual(totals["tax_amount"], f"{si.total_taxes_and_charges:.2f}")
		self.assertEqual(totals["tax_exclusive_amount"], f"{si.net_total:.2f}")
		self.assertEqual(payload["allowance_charges"][0]["amount"], f"{si.net_total - 837.5:.2f}")

	def test_negative_discount_needs_a_reason_code(self):
		si = make_invoice(self.fx, [(3, 40, None)], discount=-20)
		self.assertTrue(any("Reason Code for Negative Discounts" in p for p in self.problems(si)))

	def test_item_discount_as_price_discount(self):
		row = line(self.fx, 2, 40, price_list_rate=50, discount_percentage=20)
		si = make_invoice(self.fx, [row])
		line_ = self.payload(si)["invoice_lines"][0]
		self.assertEqual(
			(line_["gross_price"], line_["price_discount"], line_["unit_price"]), ("50.00", "10.00", "40.00")
		)

	def test_mixed_categories(self):
		si = make_invoice(self.fx, [(1, 100, None), (1, 50, self.fx.zero), (1, 20, self.fx.exempt)])
		payload = self.payload(si)
		lines = {row["id"]: row for row in payload["invoice_lines"]}
		self.assertEqual([lines[i]["tax_category"] for i in ("1", "2", "3")], ["S", "Z", "E"])
		self.assertEqual(lines["3"]["tax_exemption_reason_code"], "DL8.46.1")
		self.assertEqual(payload["document_type"], "380")
		self.assertEqual(payload["invoice_totals"]["payable_amount"], "175.00")

	def test_only_exempt_lines_make_a_480(self):
		si = make_invoice(self.fx, [(2, 30, self.fx.exempt)])
		self.assertEqual(self.payload(si)["document_type"], "480")

	def test_credit_note(self):
		si = make_invoice(self.fx, [(3, 100, None)], submit=True)
		cn = make_sales_return(si.name)
		cn.einvoice_credit_note_reason = "DL8.61.1.D - Goods or services returned"
		cn.items[0].qty = -1
		cn.insert()
		payload = self.payload(cn)
		self.assertEqual(
			(payload["document_type"], payload["credit_note_reason_code"]), ("381", "DL8.61.1.D")
		)
		self.assertEqual(payload["document_references"][0]["document_identifier"], si.name)
		self.assertNotIn("due_date", payload)
		self.assertNotIn("payment_terms", payload)
		self.assertEqual(payload["invoice_lines"][0]["quantity"], "1")
		self.assertEqual(payload["invoice_totals"]["payable_amount"], "105.00")

	def test_credit_note_needs_a_reason(self):
		si = make_invoice(self.fx, [(1, 100, None)], submit=True)
		cn = make_sales_return(si.name)
		cn.insert()
		self.assertTrue(any("Credit Note Reason" in p for p in self.problems(cn)))

	def test_credit_note_lists_a_missing_reason_and_original_together(self):
		cn = make_invoice(self.fx, [(-1, 100, None)], is_return=1)
		problems = self.problems(cn)
		self.assertTrue(any("Credit Note Reason" in p for p in problems))
		self.assertTrue(any("doesn't name the invoice it corrects" in p for p in problems))

	def test_credit_note_for_an_invoice_from_another_system(self):
		issued = add_days(today(), -400)
		cn = make_invoice(
			self.fx,
			[(-1, 100, None)],
			is_return=1,
			einvoice_credit_note_reason="DL8.61.1.D - Goods or services returned",
			einvoice_preceding_invoices=[{"document_identifier": "OLD-ERP-1001", "issue_date": issued}],
		)
		payload = self.payload(cn)
		self.assertEqual(payload["document_type"], "381")
		self.assertEqual(
			payload["document_references"], [{"document_identifier": "OLD-ERP-1001", "issue_date": issued}]
		)

	def test_debit_note_is_a_tax_invoice_referencing_the_original(self):
		si = make_invoice(self.fx, [(1, 100, None)], submit=True)
		dn = make_invoice(self.fx, [(1, 20, None)], is_debit_note=1, return_against=si.name)
		payload = self.payload(dn)
		self.assertEqual(payload["document_type"], "380")
		self.assertEqual(payload["document_references"][0]["document_identifier"], si.name)

	def test_debit_note_for_an_invoice_from_another_system(self):
		dn = make_invoice(
			self.fx,
			[(1, 20, None)],
			is_debit_note=1,
			einvoice_preceding_invoices=[
				{"document_identifier": "OLD-ERP-1001", "issue_date": add_days(today(), -400)}
			],
		)
		payload = self.payload(dn)
		self.assertEqual(payload["document_type"], "380")
		self.assertEqual(payload["document_references"][0]["document_identifier"], "OLD-ERP-1001")

	def test_debit_note_needs_its_original(self):
		dn = make_invoice(self.fx, [(1, 20, None)], is_debit_note=1)
		self.assertTrue(any("referencing the original" in p for p in self.problems(dn)))

	def test_unit_codes_are_the_specifications(self):
		official = code_lists.codes(code_lists.UNITS)
		self.assertGreater(len(official), 2000)
		self.assertEqual(official["H87"][0], "piece")
		self.assertIn("XBX", official)  # package codes, from Recommendation 21
		loaded = frappe.get_all(
			"Common Code", filters={"code_list": "UN/ECE Recommendation 20"}, pluck="common_code"
		)
		self.assertFalse(set(official) - set(loaded), "install and migrate load every code")

	def test_charge_reasons_are_the_specifications(self):
		reasons = code_lists.codes(code_lists.CHARGE_REASONS)
		self.assertGreater(len(reasons), 100)
		self.assertEqual(reasons["FC"][0], "Freight service")
		offered = {row["value"]: row["label"] for row in charge_reasons()}
		self.assertEqual(offered["FC"], "FC - Freight service")

	def test_settings_refuse_a_charge_reason_outside_the_list(self):
		settings = frappe.get_doc("E Invoicing Settings", self.fx.company.name)
		settings.negative_discount_reason_code = "FC - Freight service"
		settings.validate_charge_reasons()
		self.assertEqual(settings.negative_discount_reason_code, "FC")
		settings.charge_accounts[0].reason_code = "QQQ"
		with self.assertRaises(frappe.ValidationError) as caught:
			settings.validate_charge_reasons()
		self.assertIn("Charge row 1: reason code QQQ", str(caught.exception))

	def test_charge_reason_dropped_from_the_list_is_refused(self):
		# Saved before a refresh of the list dropped it: settings keep it, the invoice check refuses it.
		self.settings(negative_discount_reason_code="QQQ")
		try:
			si = make_invoice(self.fx, [(3, 40, None)], discount=-20)
			problems = self.problems(si)
		finally:
			self.settings(negative_discount_reason_code=None)
		self.assertTrue(any("is QQQ, which is not a charge reason code" in p for p in problems))

	def test_unit_code_outside_the_specification_is_refused(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		with patch("erpnext.edi.doctype.code_list.code_list.get_codes_for", return_value=("PCS",)):
			problems = self.problems(si)
		self.assertTrue(any("PINT-AE has no unit code PCS" in p for p in problems))

	def test_form_marks_the_template_required_where_einvoicing_is_on(self):
		self.assertTrue(einvoicing_enabled(self.fx.company.name))
		self.assertFalse(einvoicing_enabled("_Test EInv No Such Company"))

	def test_problems_are_collected_and_block_the_submit(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], customer=self.fx.no_peppol, address=self.fx.no_peppol_address
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			si.submit()
		self.assertIn("has no Peppol ID", str(caught.exception))

	def test_unmapped_line_template_never_falls_back(self):
		other = frappe.get_doc(
			{
				"doctype": "Item Tax Template",
				"title": "_Test EInv Unmapped",
				"company": self.fx.company.name,
				"taxes": [{"tax_type": self.fx.vat, "tax_rate": 0}],
			}
		).insert()
		si = make_invoice(self.fx, [(1, 100, None), (1, 100, other.name)])
		self.assertTrue(any("has no tax treatment" in p for p in self.problems(si)))

	def test_b2c_gets_no_submission(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], customer=self.fx.walk_in, address=self.fx.walk_in_address, submit=True
		)
		self.assertIsNone(service.active_submission("Sales Invoice", si.name))

	def test_unknown_payload_keys_are_caught(self):
		with self.assertRaises(KeyError):
			check_keys({"tax_totals": [{"tax_percentage": "5"}]})


class TestTransactionTypes(FlowTestCase):
	"""The eight transaction-type flags and the predefined recipients."""

	def test_free_trade_zone(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], einvoice_is_ftz=1, einvoice_fz_beneficiary_id="189098765401003"
		)
		payload = self.payload(si)
		self.assertTrue(payload["flags"]["is_ftz"])
		self.assertEqual(payload["fz_beneficiary_id"], "189098765401003")

	def test_free_trade_zone_needs_the_beneficiary(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		si.einvoice_is_ftz = 1
		self.assertTrue(any("Free Zone Beneficiary TRN" in p for p in self.problems(si)))

	def test_disclosed_agent_billing(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], einvoice_is_dab=1, einvoice_principal_id="155667745601003"
		)
		payload = self.payload(si)
		self.assertTrue(payload["flags"]["is_dab"])
		self.assertEqual(payload["principal_id"], "155667745601003")

	def test_agent_cannot_bill_for_itself(self):
		own = frappe.db.get_value("Company", self.fx.company.name, "tax_id")
		if not own:
			self.skipTest("company has no TRN")
		si = make_invoice(self.fx, [(1, 100, None)])
		si.einvoice_is_dab, si.einvoice_principal_id = 1, own
		self.assertTrue(any("ibr-176-ae" in p for p in self.problems(si)))

	def test_summary_invoice_needs_its_period(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		si.einvoice_is_summary = 1
		self.assertTrue(any("Period Start" in p for p in self.problems(si)))
		si.einvoice_period_start, si.einvoice_period_end = add_days(today(), -30), today()
		payload = self.payload(si)
		self.assertTrue(payload["flags"]["is_summary"])
		self.assertEqual(payload["invoice_period"]["description_code"], "MTH")
		self.assertEqual(payload["invoice_period"]["end_date"], str(today()))

	def test_continuous_supply(self):
		si = make_invoice(self.fx, [(1, 100, None)], einvoice_is_continuous=1)
		self.assertTrue(self.payload(si)["flags"]["is_continuous"])

	def test_deemed_supply_goes_to_the_predefined_recipient(self):
		si = make_invoice(self.fx, [(1, 100, None)], einvoice_is_deemed=1)
		payload = self.payload(si)
		self.assertTrue(payload["flags"]["is_deemed"])
		self.assertEqual(payload["receiving_party"]["peppol_id"], "0235:9900000097")

	def test_ecommerce_needs_a_delivery_address(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		si.einvoice_is_ecommerce, si.shipping_address_name = 1, None
		self.assertTrue(any("Shipping Address" in p for p in self.problems(si)))
		si.shipping_address_name = self.fx.address
		delivery = self.payload(si)["delivery"]["address"]
		self.assertEqual((delivery["emirates_code"], delivery["country_code"]), ("RAK", "AE"))

	def test_export_to_a_buyer_not_on_peppol(self):
		si = make_invoice(
			self.fx,
			[(1, 100, self.fx.zero)],
			customer=self.fx.overseas,
			address=self.fx.overseas_address,
			einvoice_is_export=1,
			shipping_address_name=self.fx.overseas_address,
		)
		payload = self.payload(si)
		buyer = payload["receiving_party"]
		self.assertEqual(buyer["peppol_id"], "0235:9900000099")
		self.assertEqual((buyer["country_code"], buyer["emirates_code"]), ("GB", "DXB"))
		self.assertTrue(payload["flags"]["is_export"])
		self.assertEqual(payload["delivery"]["address"]["country_code"], "GB")

	def test_export_to_a_uae_address_is_refused(self):
		si = make_invoice(
			self.fx, [(1, 100, self.fx.zero)], einvoice_is_export=1, shipping_address_name=self.fx.address
		)
		self.assertTrue(any("shipping address is in the UAE" in p for p in self.problems(si)))

	def test_buyer_abroad_needs_routing(self):
		si = make_invoice(self.fx, [(1, 100, None)], address=self.fx.shipping_abroad)
		self.assertTrue(any("outside the UAE" in p for p in self.problems(si)))


class TestTaxTreatment(FlowTestCase):
	def test_reverse_charge(self):
		si = make_invoice(self.fx, [line(self.fx, 1, 5000, self.fx.reverse, item=self.fx.phone)])
		payload = self.payload(si)
		line_ = payload["invoice_lines"][0]
		self.assertEqual((line_["tax_category"], line_["tax_percentage"]), ("AE", "5"))
		self.assertEqual((line_["standard_item_id"], line_["standard_item_id_scheme"]), (GTIN, "0160"))
		self.assertEqual(line_["reverse_charge_item_type"], "DL8.48.8.2")
		totals = payload["invoice_totals"]
		self.assertEqual((totals["tax_amount"], totals["payable_amount"]), ("0.00", "5000.00"))

	def test_reverse_charge_needs_a_gtin(self):
		si = make_invoice(self.fx, [(1, 5000, self.fx.reverse)])
		self.assertTrue(any("GTIN" in p for p in self.problems(si)))

	def test_goods_and_services_classification(self):
		si = make_invoice(self.fx, [line(self.fx, 1, 100, item=self.fx.phone)])
		line_ = self.payload(si)["invoice_lines"][0]
		self.assertEqual(
			(line_["commodity_code"], line_["hs_code"], line_["sac_code"]), ("B", "851713", "998313")
		)
		self.assertEqual(line_["origin_country"], "CN")

	def test_foreign_currency(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], currency="USD", conversion_rate=3.6725, debit_to=self.fx.usd_debtors
		)
		payload = self.payload(si)
		self.assertEqual(payload["currency_exchange_rate"], 3.6725)
		totals = payload["invoice_totals"]
		self.assertEqual((totals["tax_amount"], totals["total_tax_amount_in_aed"]), ("5.00", "18.36"))
		self.assertEqual(totals["total_amount_with_tax_in_aed"], "385.61")
		self.assertEqual(payload["invoice_lines"][0]["line_amount_aed"], "367.25")

	def test_document_level_charge(self):
		# Freight with VAT on it: the VAT row is charged on the previous row's total.
		taxes = [
			{
				"charge_type": "Actual",
				"account_head": self.fx.freight,
				"tax_amount": 50,
				"description": "Freight",
			},
			{
				"charge_type": "On Previous Row Total",
				"row_id": 1,
				"account_head": self.fx.vat,
				"rate": 5,
				"description": "VAT 5%",
			},
		]
		si = make_invoice(self.fx, [(1, 100, None)], taxes=taxes)
		payload = self.payload(si)
		charge = payload["allowance_charges"][0]
		self.assertEqual(
			(charge["charge_indicator"], charge["reason_code"], charge["amount"], charge["tax_category"]),
			(True, "FC", "50.00", "S"),
		)
		totals = payload["invoice_totals"]
		self.assertEqual(
			(
				totals["charge_total_amount"],
				totals["tax_exclusive_amount"],
				totals["tax_amount"],
				totals["payable_amount"],
			),
			("50.00", "150.00", "7.50", si.rounded_total and f"{si.rounded_total:.2f}" or "157.50"),
		)
		if si.rounded_total and si.rounded_total != si.grand_total:
			# The company rounds to whole units: the rounding is stated, as ibr-co-16 expects.
			self.assertEqual(totals["payable_rounding_amount"], f"{si.rounded_total - si.grand_total:.2f}")

	def test_charge_in_a_category_no_line_has_is_refused(self):
		taxes = [
			{
				"charge_type": "Actual",
				"account_head": self.fx.freight,
				"tax_amount": 50,
				"description": "Freight",
			},
			{
				"charge_type": "On Previous Row Total",
				"row_id": 1,
				"account_head": self.fx.vat,
				"rate": 5,
				"description": "VAT 5%",
			},
		]
		si = make_invoice(self.fx, [(1, 100, self.fx.zero)], taxes=taxes)
		self.assertTrue(any("needs at least one invoice line taxed S" in p for p in self.problems(si)))

	def test_out_of_scope_needs_the_buyers_trade_licence(self):
		si = make_invoice(
			self.fx, [(1, 30, self.fx.exempt)], customer=self.fx.no_peppol, address=self.fx.no_peppol_address
		)
		frappe.db.set_value("Customer", self.fx.no_peppol, "einvoice_peppol_id", BUYER_PEPPOL)
		frappe.clear_document_cache("Customer", self.fx.no_peppol)
		self.assertTrue(any("ibr-136-ae" in p for p in self.problems(si)))

	def test_unmapped_actual_row_is_refused(self):
		taxes = [
			{"charge_type": "On Net Total", "account_head": self.fx.vat, "rate": 5, "description": "VAT 5%"},
			{"charge_type": "Actual", "account_head": self.fx.vat, "tax_amount": 10, "description": "Odd"},
		]
		si = make_invoice(self.fx, [(1, 100, None)], taxes=taxes)
		self.assertTrue(any("is charged 'Actual'" in p for p in self.problems(si)))


class TestReferencesAndPayment(FlowTestCase):
	def test_every_reference_field(self):
		si = make_invoice(
			self.fx,
			[(1, 100, None)],
			po_no="PO-4520",
			einvoice_buyer_reference="BR-1",
			einvoice_buyer_accounting_cost="4025:123",
			einvoice_contract_reference="CONTRACT-889",
			einvoice_contract_value="AED 250000",
			einvoice_tender_reference="LOT-7",
			einvoice_receipt_reference="GRN-55",
			einvoice_customs_reference="CUS-1",
			einvoice_delivery_date=today(),
			einvoice_delivery_party="Warehouse 3",
			einvoice_delivery_location_id="LOC-1",
			einvoice_preceding_invoices=[
				{"document_identifier": "ADV-001", "issue_date": add_days(today(), -10)}
			],
		)
		payload = self.payload(si)
		self.assertEqual(payload["order_reference"]["purchase_order_id"], "PO-4520")
		self.assertEqual((payload["buyer_reference"], payload["accounting_cost"]), ("BR-1", "4025:123"))
		other = payload["other_references"]
		self.assertEqual(
			other["contract_document_reference"], {"id": "CONTRACT-889", "contract_value": "AED 250000"}
		)
		self.assertEqual(
			(
				other["tender_or_lot_reference"],
				other["receipt_document_reference"],
				other["customs_reference_number"],
			),
			("LOT-7", "GRN-55", "CUS-1"),
		)
		self.assertEqual(payload["document_references"][0]["document_identifier"], "ADV-001")
		self.assertEqual(
			(payload["delivery"]["party_name"], payload["delivery"]["location_id"]), ("Warehouse 3", "LOC-1")
		)

	def test_supporting_documents(self):
		file = frappe.get_doc(
			{"doctype": "File", "file_name": "timesheet.pdf", "content": pdf_bytes(), "is_private": 1}
		).insert()
		si = make_invoice(
			self.fx,
			[(1, 100, None)],
			einvoice_documents=[
				{
					"kind": "Reference",
					"document_identifier": "CONTRACT-889",
					"invoiced_object": 1,
					"scheme_id": "ON",
				},
				{
					"kind": "Link",
					"document_identifier": "DN-4471",
					"attachment_uri": "https://files.example.ae/DN-4471.pdf",
				},
				{"kind": "Attachment", "document_identifier": "TS-2026-08", "file": file.file_url},
			],
		)
		refs = self.payload(si)["additional_document_references"]
		self.assertEqual(
			refs[0], {"document_identifier": "CONTRACT-889", "document_type_code": "130", "scheme_id": "ON"}
		)
		self.assertEqual(refs[1]["attachment_uri"], "https://files.example.ae/DN-4471.pdf")
		embedded = refs[2]["embedded_document"]
		self.assertEqual((embedded["filename"], embedded["mime_code"]), ("timesheet.pdf", "application/pdf"))
		self.assertEqual(base64.b64decode(embedded["content"]), pdf_bytes())

	def test_payment_means_follow_the_mode_of_payment(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		si.append("payments", {"mode_of_payment": self.fx.cash, "amount": 105})
		means = self.payload(si)["payment_means"]
		self.assertEqual([m["type_code"] for m in means], ["10"])

	def test_instalments(self):
		si = make_invoice(self.fx, [(1, 1000, None)])
		si.payment_schedule = []
		si.append(
			"payment_schedule",
			{"due_date": add_days(today(), 30), "payment_amount": 525, "description": "First half"},
		)
		si.append(
			"payment_schedule",
			{"due_date": add_days(today(), 60), "payment_amount": 525, "description": "Second half"},
		)
		terms = self.payload(si)["payment_terms"]
		self.assertEqual([t["amount"] for t in terms], ["525.00", "525.00"])
		self.assertEqual(terms[0]["note"], "First half")

	def test_payee_and_tax_representative(self):
		settings = self.settings(
			payee_name="Gulf Factoring LLC",
			tax_rep_name="Rep LLC",
			tax_rep_vat_number="100000123400003",
			tax_rep_address=self.fx.address,
		)
		try:
			si = make_invoice(self.fx, [(1, 100, None)])
			payload = build_payload(si, settings)
		finally:
			self.settings(payee_name=None, tax_rep_name=None, tax_rep_vat_number=None, tax_rep_address=None)
		self.assertEqual(payload["payee_party"], {"name": "Gulf Factoring LLC"})
		rep = payload["tax_representative"]
		self.assertEqual(
			(rep["name"], rep["vat_number"], rep["emirates_code"]), ("Rep LLC", "100000123400003", "RAK")
		)

	def test_sales_order_goes_only_beside_the_purchase_order(self):
		# Flick's schema check refuses a sales order reference without the buyer's PO number.
		si = make_invoice(self.fx, [(1, 100, None)])
		si.items[0].sales_order = "SAL-ORD-TEST-1"  # in memory only: no such order exists
		self.assertFalse(self.payload(si).get("order_reference"))
		si.po_no = "PO-77"
		self.assertEqual(
			self.payload(si)["order_reference"],
			{"purchase_order_id": "PO-77", "sales_order_id": "SAL-ORD-TEST-1"},
		)


class TestSelfBilling(FlowTestCase):
	def test_self_billed_invoice(self):
		pi = make_purchase_invoice(self.fx, [(2, 100, self.fx.phone)])
		payload = self.payload(pi)
		self.assertEqual(payload["document_type"], "389")
		self.assertTrue(payload["flags"]["is_selfbilled"])
		party = payload["receiving_party"]
		self.assertEqual((party["peppol_id"], party["emirates_code"]), (SUPPLIER_PEPPOL, "FUJ"))
		line_ = payload["invoice_lines"][0]
		self.assertEqual(
			(line_["supplier_item_code"], line_["buyers_item_code"]), ("SUP-PHONE", self.fx.phone)
		)
		self.assertEqual(payload["invoice_totals"]["payable_amount"], "210.00")

	def test_self_billed_credit_note(self):
		pi = make_purchase_invoice(self.fx, [(2, 100, None)], submit=True)
		self.assertIsNotNone(
			service.active_submission("Purchase Invoice", pi.name), "self-billed invoices are filed"
		)
		cn = make_debit_note(pi.name)
		cn.einvoice_credit_note_reason = "DL8.61.1.D - Goods or services returned"
		cn.insert()
		payload = self.payload(cn)
		self.assertEqual(payload["document_type"], "261")
		self.assertEqual(payload["document_references"][0]["document_identifier"], pi.name)

	def test_ordinary_purchase_invoices_are_not_filed(self):
		frappe.db.set_value("Supplier", self.fx.supplier, "einvoice_self_billing", 0)
		frappe.clear_document_cache("Supplier", self.fx.supplier)
		try:
			pi = make_purchase_invoice(self.fx, [(1, 100, None)], submit=True)
			self.assertIsNone(service.active_submission("Purchase Invoice", pi.name))
		finally:
			frappe.db.set_value("Supplier", self.fx.supplier, "einvoice_self_billing", 1)
			frappe.clear_document_cache("Supplier", self.fx.supplier)


class TestSending(FlowTestCase):
	def test_accepted_then_completed_by_events(self):
		si, sub = self.submitted_invoice()
		self.assertEqual(sub.status, st.QUEUED)
		with self.flick(FakeFlick().on("POST", r"/documents\?no_error", accepted(identifier=si.name))):
			self.assertEqual(service.send(sub.name), st.PROCESSING)
		sub.reload()
		self.assertEqual((sub.docstatus, sub.document_id, sub.document_type), (1, DOC_ID, "380"))

		events = [
			{
				"type": "exchange.status_changed",
				"document_id": DOC_ID,
				"occurred_at": "2026-10-28T17:32:58.403Z",
				"reporting": "processing",
				"exchange": "delivered",
			},
			# Stale snapshot of the exchange track, as seen on pre-production.
			{
				"type": "reporting.status_changed",
				"document_id": DOC_ID,
				"occurred_at": "2026-10-28T17:32:58.430Z",
				"reporting": "reported",
				"exchange": "processing",
			},
		]
		for event in events:
			service.apply_event(event, self.settings())
		sub.reload()
		self.assertEqual(
			(sub.reporting_status, sub.exchange_status, sub.status), ("reported", "delivered", st.COMPLETED)
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "einvoice_status"), st.COMPLETED)

	def test_duplicate_answer_is_adopted(self):
		si, sub = self.submitted_invoice()
		duplicate = response(
			200,
			{
				"status": "success",
				"data": {
					"document_id": DOC_ID,
					"document_identifier": si.name,
					"reporting_status": "reported",
					"exchange_status": "delivered",
					"is_duplicate": True,
				},
			},
		)
		fake = (
			FakeFlick()
			.on("POST", r"/documents\?no_error", duplicate)
			.on("GET", DETAIL, detail(SETTLED_OK, DELIVERED))
		)
		with self.flick(fake):
			self.assertEqual(service.send(sub.name), st.COMPLETED)
		self.assertEqual(fake.called("GET", DETAIL), 2, "stored copy compared, then state read")

	def test_duplicate_of_a_different_document_is_not_adopted(self):
		si, sub = self.submitted_invoice()
		duplicate = response(
			200,
			{
				"status": "success",
				"data": {"document_id": DOC_ID, "document_identifier": si.name, "is_duplicate": True},
			},
		)
		someone_elses = detail(SETTLED_OK, DELIVERED)
		someone_elses.body["data"]["document"] = {
			"receiving_party": {"peppol_id": "0235:1000051175"},
			"invoice_totals": {"payable_amount": "194982.72"},
		}
		with self.flick(FakeFlick().on("POST", r"/documents", duplicate).on("GET", DETAIL, someone_elses)):
			self.assertEqual(service.send(sub.name), st.REJECTED)
		sub.reload()
		self.assertEqual(sub.docstatus, 0)
		self.assertIn("buyer 0235:1000051175 there", sub.error_message)

	def test_conflict_is_adopted(self):
		si, sub = self.submitted_invoice()
		conflict = response(
			409,
			{
				"status": "conflict",
				"errors": {
					"document_id": DOC_ID,
					"document_identifier": si.name,
					"reporting_status": "processing",
					"exchange_status": "processing",
					"is_duplicate": True,
				},
			},
		)
		fake = FakeFlick().on("POST", r"/documents", conflict).on("GET", DETAIL, detail(RUNNING, RUNNING))
		with self.flick(fake):
			self.assertEqual(service.send(sub.name), st.PROCESSING)

	def test_rejection_keeps_a_draft_with_readable_issues(self):
		si, sub = self.submitted_invoice()
		rejected = response(
			400,
			{
				"status": "bad_request",
				"message": "Schematron validation failed for UAE PINT AE.",
				"errors": [
					{
						"path": "Document",
						"code": "ibr-191-ae",
						"message": "[ibr-191-ae]- Payment means type code (ibt-081) must be provided",
						"severity": "error",
					},
					{
						"path": "receiving_party.emirates_code",
						"code": "invalid_enum_value",
						"message": "Invalid enum value",
						"severity": "error",
					},
				],
			},
		)
		with self.flick(FakeFlick().on("POST", r"/documents", rejected)):
			self.assertEqual(service.send(sub.name), st.REJECTED)
		sub.reload()
		self.assertIn(
			"Payment means type code (ibt-081) must be provided (ibr-191-ae)", sub.validation_errors
		)
		self.assertIn("receiving_party.emirates_code: Invalid enum value", sub.validation_errors)
		self.assertTrue(st.can_cancel_invoice(sub)[0])

	def test_no_answer_is_retried_and_blocks_cancel_until_checked(self):
		si, sub = self.submitted_invoice()
		with self.flick(
			FakeFlick().on("POST", r"/documents", response(None, error="ReadTimeout: timed out"))
		):
			self.assertEqual(service.send(sub.name), st.ERROR)
		sub.reload()
		self.assertTrue(sub.next_retry_at)
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Sales Invoice", si.name).cancel)
		found = response(
			200,
			{
				"data": {
					"documents": [
						{
							"id": DOC_ID,
							"document_identifier": si.name,
							"reporting_status": "processing",
							"exchange_status": "processing",
						}
					]
				}
			},
		)
		fake = (
			FakeFlick().on("GET", r"document_identifier=", found).on("GET", DETAIL, detail(RUNNING, RUNNING))
		)
		with self.flick(fake):
			service.reconcile(sub)
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "docstatus"), 1)

	def test_checked_and_absent_may_be_cancelled(self):
		si, sub = self.submitted_invoice()
		with self.flick(FakeFlick().on("POST", r"/documents", response(503, None, text="unavailable"))):
			service.send(sub.name)
		with self.flick(
			FakeFlick().on("GET", r"document_identifier=", response(200, {"data": {"documents": []}}))
		):
			service.reconcile(frappe.get_doc(service.DOCTYPE, sub.name))
		frappe.get_doc("Sales Invoice", si.name).cancel()
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "status"), st.CANCELLED)

	def test_refused_credential_stops_the_company(self):
		si, sub = self.submitted_invoice()
		refused = response(403, {"error": "Authentication Failed", "message": "Invalid or revoked API Key."})
		with self.flick(FakeFlick().on("POST", r"/documents", refused)):
			with self.assertRaises(service.AuthFailure):
				service.send(sub.name)
		sub.reload()
		self.assertIn("Invalid or revoked API Key", sub.error_message)

	def test_api_key_never_reaches_the_log(self):
		si, sub = self.submitted_invoice()
		with self.flick(FakeFlick().on("POST", r"/documents", accepted(identifier=si.name))):
			service.send(sub.name)
		logs = frappe.get_all(
			"E Invoice Log",
			filters={"submission": sub.name},
			fields=["request", "response", "endpoint", "error"],
		)
		self.assertTrue(logs)
		self.assertNotIn("flick_ae_test_key_never_real", json.dumps(logs))

	def test_bulk_sending(self):
		first, sub1 = self.submitted_invoice()
		second, sub2 = self.submitted_invoice()
		results = response(
			207,
			{
				"status": "partial",
				"data": {
					"results": [
						{
							"index": 0,
							"outcome": "accepted",
							"document_id": DOC_ID,
							"document_identifier": first.name,
							"reporting_status": "processing",
							"exchange_status": "processing",
						},
						{
							"index": 1,
							"outcome": "rejected",
							"document_identifier": second.name,
							"message": "invalid",
							"errors": [
								{
									"path": "Document",
									"code": "ibr-191-ae",
									"message": "[ibr-191-ae]- Payment means",
									"severity": "error",
								}
							],
						},
					]
				},
			},
		)
		fake = FakeFlick().on("POST", r"/documents/bulk\?no_error", results)
		with self.flick(fake):
			service.send_bulk([sub1.name, sub2.name], self.settings())
		self.assertEqual(len(fake.calls[0][2]["documents"]), 2)
		self.assertEqual(
			frappe.db.get_value(service.DOCTYPE, sub1.name, ["docstatus", "status"]), (1, st.PROCESSING)
		)
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub2.name, "status"), st.REJECTED)

	def test_validate_with_flick_uses_a_throwaway_number(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		fake = FakeFlick().on("POST", r"/documents/validate", response(200, {"data": {"valid": True}}))
		with self.flick(fake):
			self.assertEqual(service.validate_with_flick(si, self.settings()), (True, []))
		sent = fake.calls[0][2]["document_identifier"]
		self.assertTrue(sent.startswith("CHECK-") and sent != si.name)

		invalid = response(
			400,
			{
				"errors": [
					{
						"path": "Document",
						"code": "ibr-127-ae",
						"message": "[ibr-127-ae]-Due date",
						"severity": "error",
					}
				]
			},
		)
		with self.flick(FakeFlick().on("POST", r"/documents/validate", invalid)):
			valid, issues = service.validate_with_flick(si, self.settings())
		self.assertEqual((valid, issues), (False, ["Due date (ibr-127-ae)"]))

	def test_flicks_pdf_is_attached_to_the_invoice(self):
		si, sub = self.submitted_invoice()
		self.settings(attach_pdf=1)
		try:
			fake = (
				FakeFlick()
				.on("POST", r"/documents\?no_error", accepted(identifier=si.name))
				.on("GET", rf"/documents/{DOC_ID}/pdf$", binary(pdf_bytes()))
			)
			with self.flick(fake):
				service.send(sub.name)
		finally:
			self.settings(attach_pdf=0)
		self.assertTrue(frappe.db.get_value(service.DOCTYPE, sub.name, "files_attached"))
		self.assertTrue(
			frappe.db.exists("File", {"attached_to_name": si.name, "file_name": f"{si.name} e-invoice.pdf"})
		)


class TestFailures(FlowTestCase):
	def accept(self):
		si, sub = self.submitted_invoice()
		with self.flick(FakeFlick().on("POST", r"/documents", accepted(identifier=si.name))):
			service.send(sub.name)
		return si, frappe.get_doc(service.DOCTYPE, sub.name)

	def test_released_number_may_be_resubmitted(self):
		si, sub = self.accept()
		failed = {"status": "failed", "settled": True, "ok": False}
		errors = [
			{
				"type": "exchange",
				"origin": "region",
				"code": "RECIPIENT_NOT_ON_NETWORK",
				"message": "AS4 transmission failed.",
				"retryable": False,
			}
		]
		event = {
			"type": "document.failed",
			"document_id": DOC_ID,
			"occurred_at": "2026-10-28T18:00:00Z",
			"reporting": "processing",
			"exchange": "failed",
		}
		with self.flick(FakeFlick().on("GET", DETAIL, detail(failed, failed, errors))):
			service.apply_event(event, self.settings())
		sub.reload()
		self.assertTrue(st.numbers_released(sub))
		self.assertIn("RECIPIENT_NOT_ON_NETWORK", sub.error_message)
		self.assertIn("Flick support", sub.next_action)
		self.assertIn("Correct the Peppol ID on the customer or supplier, then Resubmit.", sub.next_action)
		# The invoice says why, not only that it failed.
		banner = invoice_banner("Sales Invoice", si.name)
		self.assertEqual((banner["color"], banner["title"]), ("red", "E-invoice failed"))
		self.assertTrue(banner["lines"][0].startswith("Flick could not find the recipient's Peppol ID"))
		self.assertTrue(any("RECIPIENT_NOT_ON_NETWORK" in line for line in banner["lines"][1:]))

		new_name = service.resubmit(sub)
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "docstatus"), 2)
		new = frappe.get_doc(service.DOCTYPE, new_name)
		self.assertEqual((new.status, new.amended_from, new.document_id), (st.QUEUED, sub.name, None))
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "einvoice_submission"), new_name)

	def test_unsettled_failure_blocks_resubmit_and_cancel(self):
		si, sub = self.accept()
		event = {
			"type": "exchange.status_changed",
			"document_id": DOC_ID,
			"occurred_at": "2026-10-28T18:00:00Z",
			"reporting": "processing",
			"exchange": "failed",
		}
		with self.flick(
			FakeFlick().on("GET", DETAIL, detail(RUNNING, {"status": "failed", "settled": False}))
		):
			service.apply_event(event, self.settings())
		sub.reload()
		self.assertIn("Do not reissue", sub.next_action)
		self.assertRaises(frappe.ValidationError, service.resubmit, sub)
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Sales Invoice", si.name).cancel)

	def test_released_invoice_may_be_cancelled(self):
		si, sub = self.accept()
		failed = {"status": "failed", "settled": True, "ok": False}
		with self.flick(FakeFlick().on("GET", DETAIL, detail(failed, failed))):
			service.refresh(sub)
		frappe.get_doc("Sales Invoice", si.name).cancel()
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "docstatus"), 2)


class TestEvents(FlowTestCase):
	def accept(self):
		si, sub = self.submitted_invoice()
		with self.flick(FakeFlick().on("POST", r"/documents", accepted(identifier=si.name))):
			service.send(sub.name)
		return sub

	def test_feed_is_read_forward_and_acked_only_when_allowed(self):
		sub = self.accept()
		page = response(
			200,
			{
				"status": "success",
				"data": {
					"events": [
						{
							"id": "c1",
							"type": "exchange.status_changed",
							"document_id": "someone-else",
							"occurred_at": "2026-10-28T17:00:00Z",
						},
						{
							"id": "c2",
							"type": "document.completed",
							"document_id": DOC_ID,
							"occurred_at": "2026-10-28T17:33:00Z",
							"reporting": "reported",
							"exchange": "delivered",
						},
					],
					"pagination": {"next_cursor": "c2", "has_more": False},
				},
			},
		)
		incoming = response(200, {"data": {"events": [], "pagination": {"has_more": False}}})
		fake = (
			FakeFlick()
			.on("GET", r"/events\?direction=outgoing", page)
			.on("GET", r"/events\?direction=incoming", incoming)
			.on("POST", r"/events/ack", response(200, {"data": {"advanced": True}}))
		)
		with self.flick(fake):
			self.assertEqual(service.sync_events(self.settings()), 2)
		self.assertEqual(fake.called("POST", "/events/ack"), 0, "ack is off for a shared participant")
		self.assertEqual(
			frappe.db.get_value("E Invoicing Settings", self.fx.company.name, "event_cursor"), "c2"
		)
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "status"), st.COMPLETED)

		with self.flick(fake):
			service.sync_events(self.settings(ack_events=1))
		self.settings(ack_events=0)
		self.assertEqual(fake.called("POST", "/events/ack"), 1)

	def test_webhook_events(self):
		sub = self.accept()
		service.apply_webhook(
			{
				"event": "document.exchange.delivered",
				"timestamp": "2026-10-28T17:33:00Z",
				"data": {
					"document_id": DOC_ID,
					"reporting_status": "processing",
					"exchange_status": "delivered",
				},
			},
			self.settings(),
		)
		sub.reload()
		self.assertEqual((sub.exchange_status, sub.reporting_status), ("delivered", "processing"))
		service.apply_webhook(
			{
				"event": "document.completed",
				"timestamp": "2026-10-28T17:33:05Z",
				"data": {
					"document_id": DOC_ID,
					"reporting_status": "reported",
					"exchange_status": "delivered",
				},
			},
			self.settings(),
		)
		sub.reload()
		self.assertEqual(sub.status, st.COMPLETED)


class TestReceiving(FlowTestCase):
	INCOMING_ID = "01a18392-0000-7000-8000-000000000001"

	def default_branch_of_test_company(self):
		"""techountant_trading numbers purchase invoices by the user's default branch. Point
		Administrator's default at the test company's branch, inside the rolled-back test only."""
		if not frappe.db.exists("DocType", "Trading Branch User"):
			return
		branch = frappe.db.get_value("Trading Branch", {"company": self.fx.company.name}, "name")
		if not branch:
			return
		frappe.db.delete("Trading Branch User", {"user": frappe.session.user})
		doc = frappe.get_doc("Trading Branch", branch)
		doc.append("users", {"user": frappe.session.user, "is_default": 1})
		doc.save()

	def received_detail(self):
		document = {
			"document_identifier": "SUP-INV-77",
			"document_type": "380",
			"issue_date": str(today()),
			"document_currency": "AED",
			"issuing_party": {
				"legal_name": "_Test EInv Supplier",
				"identifiers": {"AE:VAT": {"value": "102026001500003"}},
			},
			"invoice_lines": [
				{
					"id": "1",
					"name": "Smart phone",
					"quantity": "2",
					"uom": "H87",
					"unit_price": "100.00",
					"tax_category": "S",
					"tax_percentage": "5",
					"supplier_item_code": "SUP-PHONE",
				},
				{
					"id": "2",
					"name": "Mystery part",
					"quantity": "1",
					"uom": "H87",
					"unit_price": "50.00",
					"tax_category": "S",
					"tax_percentage": "5",
					"supplier_item_code": "NOPE",
				},
			],
			"invoice_totals": {"payable_amount": "262.50", "tax_amount": "12.50"},
		}
		return response(
			200,
			{
				"data": {
					"id": self.INCOMING_ID,
					"document_identifier": "SUP-INV-77",
					"direction": "incoming",
					"counterparty": {"name": "_Test EInv Supplier", "peppol_id": SUPPLIER_PEPPOL},
					"document": document,
				}
			},
		)

	def test_received_invoice_becomes_a_draft_purchase_invoice(self):
		fake = (
			FakeFlick()
			.on("GET", rf"/documents/{self.INCOMING_ID}$", self.received_detail())
			.on("GET", rf"/documents/{self.INCOMING_ID}/pdf$", binary(pdf_bytes()))
			.on(
				"GET",
				rf"/documents/{self.INCOMING_ID}/xml\?raw=true$",
				binary(b"<Invoice/>", "application/xml"),
			)
		)
		with self.flick(fake):
			name = inbound.receive(self.INCOMING_ID, self.settings())
			self.assertEqual(
				inbound.receive(self.INCOMING_ID, self.settings()), name, "receiving twice is harmless"
			)
		record = frappe.get_doc("E Invoice Received", name)
		self.assertEqual(
			(record.supplier, record.document_identifier, record.total_amount),
			(self.fx.supplier, "SUP-INV-77", 262.5),
		)
		self.assertEqual(frappe.db.count("File", {"attached_to_name": name}), 2)

		self.default_branch_of_test_company()
		pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		self.assertEqual((pi.docstatus, pi.bill_no, pi.supplier), (0, "SUP-INV-77", self.fx.supplier))
		self.assertEqual(
			[(row.item_code, row.item_name) for row in pi.items],
			[(self.fx.phone, "Smart phone"), (self.fx.item, "Mystery part")],
		)
		self.assertEqual(pi.taxes_and_charges, self.fx.purchase_vat)
		self.assertAlmostEqual(pi.grand_total, 262.5)
		self.assertIn("no item matched", frappe.db.get_value("E Invoice Received", name, "notes"))
		self.assertEqual(pi.einvoice_received, name)
		self.assertIsNone(service.active_submission("Purchase Invoice", pi.name))
		# The second line only waits on the Item for Unmatched Lines: no submitting it there.
		with self.assertRaisesRegex(frappe.ValidationError, "Choose our item for these lines"):
			pi.submit()


class TestReceivingAgainstOrders(FlowTestCase):
	"""Received invoices billed against our purchase orders and receipts, and supplier item codes
	remembered from what the reviewer chose."""

	def setUp(self):
		TestReceiving.default_branch_of_test_company(self)
		self.cable = _item(f"{PREFIX} Cable", "Cable")

	def received(self, lines, **document):
		"""An E Invoice Received record as receive() stores one, without asking Flick."""
		identifier = f"SUP-{frappe.generate_hash(length=8)}"
		document = {
			"document_identifier": identifier,
			"document_type": "380",
			"issue_date": str(today()),
			"document_currency": "AED",
			"invoice_lines": lines,
			**document,
		}
		return (
			frappe.get_doc(
				{
					"doctype": "E Invoice Received",
					"document_id": frappe.generate_hash(length=24),
					"company": self.fx.company.name,
					"status": "New",
					"document_identifier": identifier,
					"document_type": "380",
					"issue_date": today(),
					"supplier": self.fx.supplier,
					"currency": "AED",
					"document_json": json.dumps(document),
				}
			)
			.insert()
			.name
		)

	def line(self, id, qty, rate, code, **extra):
		return {
			"id": str(id),
			"name": f"Supplier's line {id}",
			"quantity": str(qty),
			"uom": "H87",
			"unit_price": f"{rate:.2f}",
			"tax_category": "S",
			"tax_percentage": "5",
			"supplier_item_code": code,
			**extra,
		}

	def purchase_order(self, lines):
		po = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"company": self.fx.company.name,
				"supplier": self.fx.supplier,
				"transaction_date": today(),
				"schedule_date": add_days(today(), 7),
				"currency": "AED",
				"conversion_rate": 1,
				# Some sites number orders by cost centre.
				"cost_center": self.fx.company.cost_center,
				"items": [
					{"item_code": item, "qty": qty, "rate": rate, "schedule_date": add_days(today(), 7)}
					for item, qty, rate in lines
				],
				**_branch_of(self.fx.company, "Purchase Order"),
			}
		)
		po.flags.ignore_mandatory = True
		po.insert()
		po.submit()
		return po

	def purchase_receipt(self, po, **values):
		pr = make_purchase_receipt(po.name)
		pr.update({"cost_center": self.fx.company.cost_center, **values})
		pr.flags.ignore_mandatory = True
		pr.insert()
		pr.submit()
		return pr

	def notes(self, name):
		return frappe.db.get_value("E Invoice Received", name, "notes") or ""

	def test_invoice_quoting_our_order_bills_it(self):
		po = self.purchase_order([(self.fx.phone, 2, 100), (self.cable, 3, 10)])
		name = self.received(
			[
				self.line(1, 2, 100, "SUP-PHONE"),
				# A code we don't know yet, on the order line the supplier names.
				self.line(2, 3, 10, "CBL-9", order_line_reference=f"{po.name}-2"),
			],
			order_reference={"purchase_order_id": po.name},
		)
		pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		self.assertEqual(
			[(row.item_code, row.purchase_order, row.po_detail, row.qty) for row in pi.items],
			[(self.fx.phone, po.name, po.items[0].name, 2), (self.cable, po.name, po.items[1].name, 3)],
		)
		self.assertEqual(pi.items[1].einvoice_supplier_item_code, "CBL-9")
		self.assertTrue(self.notes(name).startswith(f"Made from Purchase Order {po.name}."))

		pi.submit()
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "per_billed"), 100)
		# The reviewer accepted CBL-9 as our cable: the next invoice matches it by itself.
		self.assertEqual(inbound._match_item({"supplier_item_code": "CBL-9"}, self.fx.supplier), self.cable)
		comment = frappe.get_all("Comment", filters={"reference_name": name, "comment_type": "Info"}, pluck="content")
		self.assertIn(f"CBL-9 is {self.cable}", " ".join(comment))

	def test_invoice_quoting_their_delivery_note_bills_our_receipt(self):
		po = self.purchase_order([(self.fx.phone, 2, 100)])
		pr = self.purchase_receipt(po, supplier_delivery_note=f"DN-{frappe.generate_hash(length=6)}")
		name = self.received(
			[self.line(1, 2, 100, "SUP-PHONE")],
			other_references={"despatch_document_reference": pr.supplier_delivery_note},
		)
		pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		[row] = pi.items
		self.assertEqual((row.purchase_receipt, row.pr_detail, row.po_detail), (pr.name, pr.items[0].name, po.items[0].name))
		self.assertTrue(self.notes(name).startswith(f"Made from Purchase Receipt {pr.name}."))

	def test_order_quoted_by_number_once_received_bills_the_receipt(self):
		po = self.purchase_order([(self.fx.phone, 2, 100)])
		pr = self.purchase_receipt(po)
		name = self.received([self.line(1, 2, 100, "SUP-PHONE")], order_reference={"purchase_order_id": po.name})
		pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		self.assertEqual(pi.items[0].purchase_receipt, pr.name)

	def test_differences_from_our_order_are_left_to_the_reviewer(self):
		po = self.purchase_order([(self.fx.phone, 2, 100), (self.cable, 3, 10)])
		name = self.received(
			[
				self.line(1, 5, 120, "SUP-PHONE"),  # more, and dearer, than we ordered
				self.line(2, 3, 9, "CBL-9", order_line_reference="2"),  # cheaper
				self.line(3, 1, 50, "NOPE"),  # not on our order at all
			],
			order_reference={"purchase_order_id": po.name},
			invoice_totals={"payable_amount": "710.85"},  # (600 + 27 + 50) plus 5% VAT
		)
		same_rate = frappe.db.get_single_value("Buying Settings", "maintain_same_rate")
		frappe.db.set_single_value("Buying Settings", "maintain_same_rate", 0)
		try:
			pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		finally:
			frappe.db.set_single_value("Buying Settings", "maintain_same_rate", same_rate)
		self.assertEqual(
			[(row.item_code, row.qty, row.rate, bool(row.purchase_order)) for row in pi.items],
			[(self.fx.phone, 2, 100, True), (self.cable, 3, 9, True), (self.fx.item, 1, 50, False)],
		)
		notes = self.notes(name)
		self.assertIn("Line 1: bills 5.0, but 2.0 wait to be billed", notes)
		self.assertIn("Line 1: the supplier charges 120.0, more than the 100.0", notes)
		self.assertIn("Line 2: the supplier charges 9.0, less than the 10.0", notes)
		self.assertIn("Line 3 (Supplier's line 3) is on none of the receipts or orders it quotes.", notes)
		self.assertIn("The draft totals", notes)

	def test_line_reviewed_to_our_item_is_remembered(self):
		name = self.received([self.line(1, 1, 40, "NEW-77"), self.line(2, 1, 50, "NOPE")])
		pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		self.assertEqual([row.item_code for row in pi.items], [self.fx.item, self.fx.item])
		pi.items[0].item_code = self.cable  # the reviewer knows what NEW-77 is
		pi.remove(pi.items[1])  # and that NOPE is not ours to book
		pi.save()
		pi.submit()
		self.assertEqual(inbound._match_item({"supplier_item_code": "NEW-77"}, self.fx.supplier), self.cable)
		# A line taken off the invoice teaches nothing.
		self.assertFalse(frappe.db.exists("Item Supplier", {"supplier": self.fx.supplier, "supplier_part_no": "NOPE"}))

	def test_placeholder_item_is_on_every_site(self):
		name = ensure_placeholder_item()
		self.assertEqual(ensure_placeholder_item(), name, "made once")
		self.assertEqual(
			frappe.db.get_value("Item", name, ["is_sales_item", "is_stock_item", "is_purchase_item"]),
			(0, 0, 1),
		)
		self.assertEqual(inbound.placeholder_item(), name)

	def test_without_a_setting_lines_wait_on_the_apps_placeholder(self):
		placeholder = ensure_placeholder_item()
		self.settings(purchase_item=None)
		try:
			name = self.received([self.line(1, 1, 50, "NOPE")])
			pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
		finally:
			self.settings(purchase_item=self.fx.item)
		self.assertEqual([row.item_code for row in pi.items], [placeholder])
		with self.assertRaisesRegex(frappe.ValidationError, "Choose our item for these lines"):
			pi.submit()

	def test_with_no_placeholder_at_all_lines_keep_no_item(self):
		self.settings(purchase_item=None)
		try:
			name = self.received([self.line(1, 1, 50, "NOPE")])
			with patch.object(inbound, "placeholder_item", return_value=None):
				try:
					pi = frappe.get_doc("Purchase Invoice", inbound.create_purchase_invoice(name))
				except frappe.ValidationError as e:
					# This site drops lines without an item code as they save (a blank-row clean-up):
					# the draft is refused rather than made without the supplier's line.
					self.assertIn("removes purchase invoice lines without an item code", str(e))
					return
		finally:
			self.settings(purchase_item=self.fx.item)
		[row] = pi.items
		self.assertEqual(
			(row.item_code, row.item_name, row.expense_account),
			(None, "Supplier's line 1", frappe.get_cached_value("Company", self.fx.company.name, "default_expense_account")),
		)

	def test_price_with_more_decimals_than_the_site_keeps_is_explained(self):
		# 1000 at 0.123456 is 123.46; a site keeping 2 or 3 decimals for rates can't say that.
		name = self.received(
			[self.line(1, 1000, 0.123456, "SUP-PHONE", unit_price="0.123456", line_extension_amount="123.46")],
			invoice_totals={"payable_amount": "129.63", "tax_amount": "6.17"},
		)
		inbound.create_purchase_invoice(name)
		notes = self.notes(name)
		self.assertIn("Line 1: the supplier's price 0.123456 has more decimals than this site keeps", notes)
		self.assertIn("against the supplier's 123.46", notes)
		self.assertIn("on the supplier's invoice 6.17: claim only what the invoice states", notes)

	def test_a_later_choice_moves_a_remembered_code(self):
		gadget = _item(f"{PREFIX} Gadget", "Gadget")
		self.assertEqual(inbound._remember(self.fx.supplier, "GAD-1", gadget), f"GAD-1 is {gadget}")
		self.assertIsNone(inbound._remember(self.fx.supplier, "GAD-1", gadget))
		self.assertEqual(inbound._remember(self.fx.supplier, "GAD-1", self.cable), f"GAD-1 is {self.cable} (was {gadget})")
		self.assertEqual(inbound._match_item({"supplier_item_code": "GAD-1"}, self.fx.supplier), self.cable)

	def test_order_line_numbers(self):
		orders = {"PUR-ORD-2026-00012"}
		self.assertEqual(inbound._order_line("3", orders), (None, 3))
		self.assertEqual(inbound._order_line("PUR-ORD-2026-00012-3", orders), ("PUR-ORD-2026-00012", 3))
		self.assertEqual(inbound._order_line("PUR-ORD-2026-00012 / 10", orders), ("PUR-ORD-2026-00012", 10))
		self.assertIsNone(inbound._order_line("PUR-ORD-2026-00012", orders))  # the order alone names no line
		self.assertIsNone(inbound._order_line("LINE-A", orders))


class TestGuidance(FlowTestCase):
	"""The setup checklist, the banner on invoices and the Flick workspace's figures."""

	def test_setup_overview_follows_the_settings(self):
		[row] = setup.overview(self.fx.company.name)
		self.assertTrue(row["record"] and row["enabled"])
		unmapped = setup.unmapped_templates(self.fx.company.name, self.settings())
		for step in row["steps"]:
			expected = not unmapped if step["label"].startswith("Map every VAT template") else True
			self.assertEqual(step["done"], expected, step["label"])

	def test_unmapped_template_is_a_setup_step(self):
		template = frappe.get_doc(
			{
				"doctype": "Item Tax Template",
				"title": "_Test EInv Setup Unmapped",
				"company": self.fx.company.name,
				"taxes": [{"tax_type": self.fx.vat, "tax_rate": 0}],
			}
		).insert()
		self.assertIn(template.name, setup.unmapped_templates(self.fx.company.name, self.settings()))
		[row] = setup.overview(self.fx.company.name)
		step = next(step for step in row["steps"] if step["label"].startswith("Map every VAT template"))
		self.assertFalse(step["done"])
		self.assertTrue(step["detail"].startswith("Not mapped yet:"))

	def test_company_without_a_record_has_every_step_left(self):
		steps = setup.steps(self.fx.company.name, None)
		self.assertTrue(steps[0]["label"].startswith("Create this company's E Invoicing Settings record"))
		self.assertFalse(any(step["done"] for step in steps))

	def test_banner_on_a_draft_lists_what_is_missing(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], customer=self.fx.no_peppol, address=self.fx.no_peppol_address
		)
		banner = invoice_banner("Sales Invoice", si.name)
		self.assertEqual(banner["color"], "orange")
		self.assertRegex(banner["title"], r"^Not ready to e-invoice: (1 problem|\d+ problems) to fix before submitting\.$")
		self.assertTrue(any("has no Peppol ID" in line for line in banner["lines"]))

	def test_banner_on_a_draft_ready_to_send(self):
		si = make_invoice(self.fx, [(1, 100, None)])
		banner = invoice_banner("Sales Invoice", si.name)
		self.assertEqual(banner["color"], "green")
		self.assertIn("queued when you submit it", banner["title"])  # the fixture sends in batches
		self.assertEqual(banner["action"], {"label": "Check with Flick", "check": True})
		self.assertIn("Flick validates it again when it is sent", banner["lines"][0])
		self.settings(validate_before_submit=1)
		try:
			banner = invoice_banner("Sales Invoice", si.name)
		finally:
			self.settings(validate_before_submit=0)
		self.assertIn("stops the submit if it finds a problem", banner["lines"][0])

	def test_banner_on_a_skipped_invoice_says_why(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], customer=self.fx.walk_in, address=self.fx.walk_in_address
		)
		banner = invoice_banner("Sales Invoice", si.name)
		self.assertEqual(banner["color"], "blue")
		self.assertEqual(banner["title"], "E-invoicing not applicable: B2C sale (customer type Individual), out of scope for now.")

	def test_banner_after_submitting_follows_the_submission(self):
		si, sub = self.submitted_invoice()
		banner = invoice_banner("Sales Invoice", si.name)
		self.assertEqual((banner["color"], banner["title"]), ("blue", "E-invoice queued"))
		self.assertEqual(banner["action"]["route"], ["Form", "E Invoice Submission", sub.name])
		self.assertEqual(banner["lines"], ["Waiting to be sent to Flick."])

	def test_dashboard_counts_submissions_and_tracks(self):
		company = self.fx.company.name
		before = dashboard(days=30, company=company)
		si, sub = self.submitted_invoice()
		frappe.db.set_value(
			"E Invoice Submission",
			sub.name,
			{
				"status": "Failed",
				"reporting_status": "failed",
				"reporting_settled": 0,
				"exchange_status": "delivered",
			},
		)
		after = dashboard(days=30, company=company)
		self.assertEqual(after["statuses"].get("Failed", 0), before["statuses"].get("Failed", 0) + 1)
		self.assertEqual(
			after["tracks"]["reporting"]["retrying"], before["tracks"]["reporting"]["retrying"] + 1
		)
		self.assertEqual(after["tracks"]["delivery"]["done"], before["tracks"]["delivery"]["done"] + 1)
		self.assertIn(sub.name, [row.name for row in after["attention"]["rows"]])
		[row] = [row for row in after["companies"] if row["company"] == company]
		self.assertTrue(row["enabled"])

	def test_dashboard_block_and_workspace_are_installed(self):
		ensure_dashboard()
		block = frappe.get_doc("Custom HTML Block", DASHBOARD_BLOCK)
		self.assertIn("flick_einvoicing.api.dashboard", block.script)
		self.assertEqual({row.role for row in block.roles}, set(DASHBOARD_ROLES))
		self.assertFalse(block.private)
		workspace = frappe.get_doc("Workspace", "Flick")
		self.assertEqual([row.custom_block_name for row in workspace.custom_blocks], [DASHBOARD_BLOCK])
		self.assertFalse(frappe.db.exists("Workspace", "E-Invoicing"))

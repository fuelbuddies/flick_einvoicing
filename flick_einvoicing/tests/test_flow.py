import json
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from frappe.tests.utils import FrappeTestCase

from flick_einvoicing.einvoice import service
from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.payload import build_payload, check_keys
from flick_einvoicing.einvoice.scope import settings_for
from flick_einvoicing.providers.flick import FlickClient
from flick_einvoicing.tests.utils import (
	BUYER_PEPPOL,
	BUYER_TRN,
	DOC_ID,
	FakeFlick,
	accepted,
	detail,
	make_fixtures,
	make_invoice,
	response,
	uae_company,
)

SETTLED_OK = {"status": "reported", "settled": True, "ok": True}
DELIVERED = {"status": "delivered", "settled": True, "ok": True}


class FlowTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		company = uae_company()
		if not company:
			raise cls.skipTest(cls, "needs a company in the United Arab Emirates keeping books in AED")
		cls.fx = make_fixtures(company)

	def settings(self):
		frappe.clear_document_cache("E Invoicing Settings", self.fx.company.name)
		return settings_for(self.fx.company.name)

	def flick(self, fake):
		return patch.object(FlickClient, "_http", side_effect=fake)

	def submitted_invoice(self, lines=((1, 1000, None),), **kw):
		si = make_invoice(self.fx, lines, submit=True, **kw)
		sub = service.active_submission("Sales Invoice", si.name)
		self.assertIsNotNone(sub, "a submission is created when the invoice is submitted")
		return si, sub


class TestPayload(FlowTestCase):
	def test_standard_invoice(self):
		# 3 x 40 less 20 discount: net 100.00, so the unit price needs more than 2 decimals.
		si = make_invoice(self.fx, [(3, 40, None)], discount=20)
		payload = build_payload(si, self.settings())

		self.assertEqual(payload["document_identifier"], si.name)
		self.assertEqual(payload["document_type"], "380")
		self.assertEqual(payload["document_currency"], "AED")
		self.assertEqual(payload["due_date"], str(si.due_date))
		self.assertEqual(payload["flags"], dict.fromkeys(payload["flags"], False))
		self.assertEqual(len(payload["flags"]), 9)

		line = payload["invoice_lines"][0]
		self.assertEqual(line["quantity"], "3")
		self.assertEqual(line["unit_price"], "33.333333")
		self.assertEqual(line["line_extension_amount"], "100.00")
		self.assertEqual(line["uom"], "H87")
		self.assertEqual(line["tax_category"], "S")
		self.assertEqual(line["tax_percentage"], "5")
		self.assertEqual(line["description"], "Throwaway test item")

		totals = payload["invoice_totals"]
		self.assertEqual(totals["tax_exclusive_amount"], "100.00")
		self.assertEqual(totals["tax_amount"], "5.00")
		self.assertEqual(totals["tax_inclusive_amount"], "105.00")
		self.assertEqual(totals["payable_amount"], "105.00")

		buyer = payload["receiving_party"]
		self.assertEqual(buyer["peppol_id"], BUYER_PEPPOL)
		self.assertEqual(buyer["identifiers"], {"AE:VAT": {"value": BUYER_TRN}})
		self.assertEqual(buyer["emirates_code"], "RAK")
		self.assertEqual(buyer["country_code"], "AE")

		means = payload["payment_means"][0]
		self.assertEqual(means["type_code"], "30")
		self.assertEqual(means["payment_account_id"], "AE070331234567890123456")
		check_keys(payload)

	def test_mixed_categories(self):
		si = make_invoice(self.fx, [(1, 100, None), (1, 50, self.fx.zero), (1, 20, self.fx.exempt)])
		payload = build_payload(si, self.settings())
		lines = {line["id"]: line for line in payload["invoice_lines"]}
		self.assertEqual([lines[i]["tax_category"] for i in ("1", "2", "3")], ["S", "Z", "E"])
		self.assertEqual(lines["2"]["tax_percentage"], "0")
		self.assertEqual(lines["3"]["tax_exemption_reason_code"], "DL8.46.1")
		self.assertEqual(payload["document_type"], "380")
		self.assertEqual(payload["invoice_totals"]["tax_amount"], "5.00")
		self.assertEqual(payload["invoice_totals"]["payable_amount"], "175.00")

	def test_only_exempt_lines_make_a_480(self):
		si = make_invoice(self.fx, [(2, 30, self.fx.exempt)])
		self.assertEqual(build_payload(si, self.settings())["document_type"], "480")

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
		from flick_einvoicing.einvoice.payload import invoice_problems

		problems = invoice_problems(si, self.settings())
		self.assertTrue(any("has no tax treatment" in p for p in problems), problems)

	def test_b2c_gets_no_submission(self):
		si = make_invoice(
			self.fx, [(1, 100, None)], customer=self.fx.walk_in, address=self.fx.walk_in_address, submit=True
		)
		self.assertIsNone(service.active_submission("Sales Invoice", si.name))

	def test_credit_note(self):
		si = make_invoice(self.fx, [(3, 100, None)], submit=True)
		cn = make_sales_return(si.name)
		cn.einvoice_credit_note_reason = "DL8.61.1.D - Goods or services returned"
		cn.items[0].qty = -1
		cn.insert()
		payload = build_payload(cn, self.settings())
		self.assertEqual(payload["document_type"], "381")
		self.assertEqual(payload["credit_note_reason_code"], "DL8.61.1.D")
		self.assertEqual(payload["document_references"][0]["document_identifier"], si.name)
		self.assertNotIn("due_date", payload)
		line = payload["invoice_lines"][0]
		self.assertEqual(line["quantity"], "1")
		self.assertEqual(line["line_extension_amount"], "100.00")
		self.assertEqual(payload["invoice_totals"]["payable_amount"], "105.00")

	def test_credit_note_needs_a_reason(self):
		si = make_invoice(self.fx, [(1, 100, None)], submit=True)
		cn = make_sales_return(si.name)
		cn.insert()
		from flick_einvoicing.einvoice.payload import invoice_problems

		self.assertTrue(any("Credit Note Reason" in p for p in invoice_problems(cn, self.settings())))


class TestSending(FlowTestCase):
	def test_accepted_then_completed_by_events(self):
		si, sub = self.submitted_invoice()
		self.assertEqual(sub.status, st.QUEUED)

		fake = FakeFlick().on("POST", r"/documents\?no_error", accepted(identifier=si.name))
		with self.flick(fake):
			self.assertEqual(service.send(sub.name), st.PROCESSING)
		sub.reload()
		self.assertEqual(sub.docstatus, 1)
		self.assertEqual(sub.document_id, DOC_ID)
		self.assertEqual(json.loads(sub.request_payload)["document_identifier"], si.name)

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
					"status": "completed",
					"reporting_status": "reported",
					"exchange_status": "delivered",
					"is_duplicate": True,
				},
			},
		)
		fake = (
			FakeFlick()
			.on("POST", r"/documents\?no_error", duplicate)
			.on("GET", rf"/documents/{DOC_ID}", detail(SETTLED_OK, DELIVERED))
		)
		with self.flick(fake):
			self.assertEqual(service.send(sub.name), st.COMPLETED)
		self.assertEqual(fake.called("GET", DOC_ID), 2, "stored copy compared, then state read")

	def test_duplicate_of_a_different_document_is_not_adopted(self):
		si, sub = self.submitted_invoice()
		duplicate = response(
			200,
			{
				"status": "success",
				"data": {
					"document_id": DOC_ID,
					"document_identifier": si.name,
					"status": "completed",
					"reporting_status": "reported",
					"exchange_status": "delivered",
					"is_duplicate": True,
				},
			},
		)
		someone_elses = detail(SETTLED_OK, DELIVERED)
		someone_elses.body["data"]["document"] = {
			"receiving_party": {"peppol_id": "0235:1000051175"},
			"document_type": "380",
			"invoice_totals": {"payable_amount": "194982.72"},
		}
		fake = (
			FakeFlick().on("POST", r"/documents", duplicate).on("GET", rf"/documents/{DOC_ID}", someone_elses)
		)
		with self.flick(fake):
			self.assertEqual(service.send(sub.name), st.REJECTED)
		sub.reload()
		self.assertEqual(sub.docstatus, 0)
		self.assertIn("different document", sub.error_message)
		self.assertIn("buyer 0235:1000051175 there", sub.error_message)
		self.assertTrue(st.can_cancel_invoice(sub)[0])

	def test_conflict_is_adopted(self):
		si, sub = self.submitted_invoice()
		conflict = response(
			409,
			{
				"status": "conflict",
				"message": "in use",
				"errors": {
					"document_id": DOC_ID,
					"document_identifier": si.name,
					"status": "processing",
					"reporting_status": "processing",
					"exchange_status": "processing",
					"is_duplicate": True,
				},
			},
		)
		fake = (
			FakeFlick()
			.on("POST", r"/documents", conflict)
			.on(
				"GET",
				rf"/documents/{DOC_ID}",
				detail(
					{"status": "processing", "settled": False}, {"status": "processing", "settled": False}
				),
			)
		)
		with self.flick(fake):
			self.assertEqual(service.send(sub.name), st.PROCESSING)
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "docstatus"), 1)

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
		self.assertEqual(sub.docstatus, 0)
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
		self.assertEqual(sub.attempt_count, 1)
		self.assertTrue(sub.next_retry_at)
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Sales Invoice", si.name).cancel)

		found = response(
			200,
			{
				"status": "success",
				"data": {
					"documents": [
						{
							"id": DOC_ID,
							"document_identifier": si.name,
							"status": "processing",
							"reporting_status": "processing",
							"exchange_status": "processing",
						}
					]
				},
			},
		)
		fake = (
			FakeFlick()
			.on("GET", r"document_identifier=", found)
			.on(
				"GET",
				rf"/documents/{DOC_ID}",
				detail(
					{"status": "processing", "settled": False}, {"status": "processing", "settled": False}
				),
			)
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
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "status"), st.QUEUED)
		frappe.get_doc("Sales Invoice", si.name).cancel()
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "status"), st.CANCELLED)

	def test_refused_credential_stops_the_company(self):
		si, sub = self.submitted_invoice()
		refused = response(403, {"error": "Authentication Failed", "message": "Invalid or revoked API Key."})
		with self.flick(FakeFlick().on("POST", r"/documents", refused)):
			with self.assertRaises(service.AuthFailure):
				service.send(sub.name)
		sub.reload()
		self.assertEqual(sub.status, st.ERROR)
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
		for log in logs:
			self.assertNotIn("flick_ae_test_key_never_real", json.dumps(log))


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
		with self.flick(FakeFlick().on("GET", rf"/documents/{DOC_ID}", detail(failed, failed, errors))):
			service.apply_event(event, self.settings())
		sub.reload()
		self.assertEqual(sub.status, st.FAILED)
		self.assertTrue(st.numbers_released(sub))
		self.assertIn("RECIPIENT_NOT_ON_NETWORK", sub.error_message)
		self.assertIn("Flick support", sub.next_action)

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
		still_trying = detail(
			{"status": "processing", "settled": False}, {"status": "failed", "settled": False}
		)
		with self.flick(FakeFlick().on("GET", rf"/documents/{DOC_ID}", still_trying)):
			service.apply_event(event, self.settings())
		sub.reload()
		self.assertEqual(sub.status, st.FAILED)
		self.assertIn("Do not reissue", sub.next_action)
		self.assertRaises(frappe.ValidationError, service.resubmit, sub)
		self.assertRaises(frappe.ValidationError, frappe.get_doc("Sales Invoice", si.name).cancel)

	def test_released_invoice_may_be_cancelled(self):
		si, sub = self.accept()
		failed = {"status": "failed", "settled": True, "ok": False}
		with self.flick(FakeFlick().on("GET", rf"/documents/{DOC_ID}", detail(failed, failed))):
			service.refresh(sub)
		frappe.get_doc("Sales Invoice", si.name).cancel()
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "docstatus"), 2)


class TestEvents(FlowTestCase):
	def test_feed_is_read_forward_and_acked_only_when_allowed(self):
		si, sub = self.submitted_invoice()
		with self.flick(FakeFlick().on("POST", r"/documents", accepted(identifier=si.name))):
			service.send(sub.name)

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
		fake = (
			FakeFlick()
			.on("GET", r"/events\?", page)
			.on("POST", r"/events/ack", response(200, {"data": {"advanced": True}}))
		)

		with self.flick(fake):
			self.assertEqual(service.sync_events(self.settings()), 2)
		self.assertEqual(fake.called("POST", "/events/ack"), 0, "ack is off for a shared participant")
		self.assertEqual(
			frappe.db.get_value("E Invoicing Settings", self.fx.company.name, "event_cursor"), "c2"
		)
		self.assertEqual(frappe.db.get_value(service.DOCTYPE, sub.name, "status"), st.COMPLETED)

		frappe.db.set_value("E Invoicing Settings", self.fx.company.name, "ack_events", 1)
		with self.flick(fake):
			service.sync_events(self.settings())
		self.assertEqual(fake.called("POST", "/events/ack"), 1)
		self.assertIn("cursor=c2", fake.calls[-2][1])

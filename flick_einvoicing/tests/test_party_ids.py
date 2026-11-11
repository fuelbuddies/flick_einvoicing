from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

from flick_einvoicing.data_form import _clean
from flick_einvoicing.party_ids import normalize_peppol_id, normalize_trn


class TestPartyIds(FrappeTestCase):
	def test_trn(self):
		self.assertEqual(normalize_trn("100-2345-6789-0003"), "100234567890003")
		self.assertIsNone(normalize_trn("na"))
		for bad in ("12345", "1002345678900031", "10023456789000A"):
			with self.assertRaises(frappe.ValidationError):
				normalize_trn(bad)

	def test_peppol_id(self):
		self.assertEqual(normalize_peppol_id("1234567890"), "0235:1234567890")
		self.assertEqual(normalize_peppol_id("0235:1234567890"), "0235:1234567890")
		for bad in ("100234567890003", "2234567890", "123456789", "0235:12345"):
			with self.assertRaises(frappe.ValidationError):
				normalize_peppol_id(bad)

	def _q(self, field, fieldtype="Text"):
		return SimpleNamespace(
			label="Q", fieldtype=fieldtype, reqd=1, options=None, customer_field=field, fieldname="q"
		)

	def test_form_answers(self):
		self.assertEqual(_clean(self._q("tax_id"), "100 234 567 890 003"), "100234567890003")
		self.assertEqual(_clean(self._q("tax_id"), "NA"), "NA")
		self.assertEqual(_clean(self._q("einvoice_peppol_id"), "0235:1234567890"), "1234567890")
		with self.assertRaises(frappe.ValidationError):
			_clean(self._q("tax_id"), "12345")
		with self.assertRaises(frappe.ValidationError):
			_clean(self._q("einvoice_go_live_date", "Date"), "2020-01-01")

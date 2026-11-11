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



class TestUomFallback(FrappeTestCase):
	def _resolve(self, uom):
		from flick_einvoicing.einvoice.payload import _Builder

		problems = []
		if not frappe.get_meta("UOM").has_field("common_code"):
			self.skipTest("UOM has no Common Code on this ERPNext")
		fake = SimpleNamespace(_uom_common_code=_Builder._uom_common_code, 
			settings=SimpleNamespace(uom_code_list="UN/ECE Recommendation 20"), problem=problems.append
		)
		return _Builder._resolve_uom(fake, uom, "Row 1"), problems

	def test_uom_common_code_used_without_link(self):
		if not frappe.db.exists("UOM", "ZZ Test Litre"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "ZZ Test Litre", "common_code": "ltr"}).insert()
		self.assertEqual(self._resolve("ZZ Test Litre"), ("LTR", []))

	def test_unknown_uom_common_code_is_rejected(self):
		if not frappe.db.exists("UOM", "ZZ Test Bad"):
			frappe.get_doc({"doctype": "UOM", "uom_name": "ZZ Test Bad", "common_code": "QQQ"}).insert()
		code, problems = self._resolve("ZZ Test Bad")
		self.assertIsNone(code)
		self.assertTrue(problems)

	def test_fallback_order(self):
		"""Without an Applies To link, the UOM's own code is used and still checked against PINT-AE."""
		from flick_einvoicing.einvoice.payload import _Builder

		for uom_code, expected in (("LTR", "LTR"), ("QQQ", None)):
			problems = []
			fake = SimpleNamespace(
				settings=SimpleNamespace(uom_code_list="UN/ECE Recommendation 20"),
				problem=problems.append,
				_uom_common_code=lambda uom, c=uom_code: c,
			)
			self.assertEqual(_Builder._resolve_uom(fake, "ZZ No Link UOM", "Row 1"), expected)
			self.assertEqual(bool(problems), expected is None)

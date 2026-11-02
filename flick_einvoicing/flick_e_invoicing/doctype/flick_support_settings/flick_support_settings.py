# Copyright (c) 2026, Ashar Shamsudheen and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class FlickSupportSettings(Document):
	def validate(self):
		if not cint(self.enabled):
			return
		if "techountant_support" in frappe.get_installed_apps():
			frappe.throw(_("This site already reports through Techountant Support. Leave this turned off."))
		from flick_einvoicing.support import validate_hub_url

		if not (self.hub_url and self.site_key and self.get_password("site_secret", raise_exception=False)):
			frappe.throw(_("Set Hub URL, Site Key and Site Secret to turn support on"))
		validate_hub_url(self.hub_url)

	def on_update(self):
		before = self.get_doc_before_save()
		if before and cint(before.enabled) and not cint(self.enabled):
			from flick_einvoicing.support import hold_unsent

			hold_unsent()

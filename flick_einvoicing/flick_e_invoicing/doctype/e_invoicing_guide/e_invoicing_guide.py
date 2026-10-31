# Copyright (c) 2026, Ashar Shamsudheen and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from flick_einvoicing.einvoice import guide


class EInvoicingGuide(Document):
	def onload(self):
		# Rendered on every load, so the site's own settings in it are current.
		self.set_onload("guide", guide.render())

# Copyright (c) 2026, Techountant and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from flick_einvoicing.einvoice import status as st


class EInvoiceSubmission(Document):
	def validate(self):
		other = frappe.db.get_value(
			self.doctype,
			{
				"invoice_doctype": self.invoice_doctype,
				"invoice": self.invoice,
				"docstatus": ("<", 2),
				"name": ("!=", self.name),
			},
			"name",
		)
		if other and self.docstatus == 0:
			frappe.throw(
				_("{0} {1} already has e-invoice submission {2}.").format(
					self.invoice_doctype, self.invoice, other
				)
			)

	def before_submit(self):
		# Submitted means Flick accepted it. A draft submitted by hand would never be sent.
		if not self.flags.accepted_by_flick:
			frappe.throw(
				_("A submission is submitted only when Flick accepts it. Use Retry to send it."),
				title=_("UAE e-invoicing"),
			)

	def before_cancel(self):
		# Cancelling here never withdraws anything from the FTA. It is only allowed once Flick
		# has released the number, which is when a corrected copy may be resubmitted.
		if not (self.flags.allow_cancel or st.numbers_released(self)):
			frappe.throw(
				_("A document Flick accepted can't be cancelled. Correct it with a credit note."),
				title=_("UAE e-invoicing"),
			)

	def on_trash(self):
		if self.attempt_count:
			frappe.throw(_("This submission was sent to Flick, so it is kept as a record."))

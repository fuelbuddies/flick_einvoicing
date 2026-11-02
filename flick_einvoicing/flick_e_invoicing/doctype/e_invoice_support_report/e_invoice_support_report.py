# Copyright (c) 2026, Ashar Shamsudheen and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

from flick_einvoicing import support


class EInvoiceSupportReport(Document):
	def before_insert(self):
		if not support.is_enabled():
			frappe.throw(_("Support is turned off. A System Manager can enable it in Flick Support Settings."))
		# delivery fields are the app's to set, whatever came in with the request
		self.update(
			{
				"send_status": "Queued",
				"remote_ticket": None,
				"remote_status": None,
				"attempts": 0,
				"next_retry_at": None,
				"sent_at": None,
				"last_error": None,
				"resolution": None,
				"unsent_occurrences": 0,
				"occurrence_count": 1,
			}
		)
		self.set("messages", [])
		if self.flags.from_error_log:
			return
		if self.report_type not in ("Bug", "Feature Request"):
			self.report_type = "Bug"
		self.reported_by = frappe.session.user
		self.traceback = None
		self.fingerprint = None
		self.title = (support.redact(self.title, keep_emails=True) or "").strip()[:240]
		self.description = support.redact(self.description, keep_emails=True)
		self.page_context = json.dumps(
			{"route": self.route, "doctype": self.related_doctype, "name": self.related_name}, indent=1
		)

	def validate(self):
		# write permission exists only because Desk needs it to fill in a new form
		if not self.is_new() and not self.flags.ignore_permissions:
			frappe.throw(_("A report cannot be edited once raised. Add a reply instead."), frappe.PermissionError)

	def after_insert(self):
		if not self.flags.from_error_log:
			support.enqueue_send(self.name)

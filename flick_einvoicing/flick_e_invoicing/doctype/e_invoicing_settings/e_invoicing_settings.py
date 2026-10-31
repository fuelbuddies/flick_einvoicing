# Copyright (c) 2026, Ashar Shamsudheen and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

from flick_einvoicing.einvoice import code_lists, codes

UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


class EInvoicingSettings(Document):
	def validate(self):
		self.base_url = (self.base_url or "").strip().rstrip("/")
		if self.base_url and not self.base_url.startswith("https://"):
			frappe.throw(_("Base URL must start with https://"))
		if self.base_url.endswith("/v1"):
			frappe.throw(_("Leave /v1 off the Base URL; the app adds the API paths itself."))

		self.participant_id = (self.participant_id or "").strip()
		if self.participant_id and not UUID_PATTERN.match(self.participant_id):
			frappe.throw(
				_(
					"Participant ID should be the UUID Flick gave you, like 0190a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b."
				)
			)

		self.validate_tax_treatments()
		self.validate_charge_reasons()
		if self.enabled:
			self.validate_ready()

		rounding = frappe.db.get_single_value("System Settings", "rounding_method") or ""
		if self.enabled and rounding.startswith("Banker"):
			frappe.msgprint(
				_(
					"ERPNext rounds half a fil to even here, while Flick rounds it up. On an exact half fil "
					"the e-invoice's VAT is then one fil above the books. Commercial Rounding in System "
					"Settings keeps the two identical."
				),
				indicator="orange",
			)

		if self.environment == "Production" and "preprod" in self.base_url:
			frappe.msgprint(
				_("Environment is Production but the Base URL looks like pre-production."), indicator="orange"
			)

	def validate_tax_treatments(self):
		seen = set()
		for row in self.tax_treatments:
			key = (row.template_type, row.template)
			if key in seen:
				frappe.throw(_("Row {0}: {1} is mapped twice.").format(row.idx, row.template))
			seen.add(key)

			template_company = frappe.db.get_value(row.template_type, row.template, "company")
			if template_company and template_company != self.company:
				frappe.throw(
					_("Row {0}: {1} belongs to {2}, not {3}.").format(
						row.idx, row.template, template_company, self.company
					)
				)

			category = codes.code_of(row.tax_category)
			if category == "E":
				if not row.exemption_reason_code:
					frappe.throw(_("Row {0}: exempt supplies need an exemption reason.").format(row.idx))
			else:
				row.exemption_reason_code = None
				row.exemption_reason_text = None
			if category == "AE":
				if not row.reverse_charge_item_type:
					frappe.throw(
						_("Row {0}: reverse charge needs the goods type it applies to.").format(row.idx)
					)
			else:
				row.reverse_charge_item_type = None
			if category == codes.MARGIN:
				frappe.msgprint(
					_("Row {0}: Flick currently rejects every profit-margin-scheme document.").format(
						row.idx
					),
					indicator="orange",
				)

		for row in self.get("charge_accounts") or []:
			company = frappe.db.get_value("Account", row.account, "company")
			if company and company != self.company:
				frappe.throw(_("Charge row {0}: {1} belongs to {2}.").format(row.idx, row.account, company))
		modes = [row.mode_of_payment for row in self.get("payment_modes") or []]
		if len(modes) != len(set(modes)):
			frappe.throw(_("Each Mode of Payment can be mapped once."))

	def validate_charge_reasons(self):
		"""Charge reasons come from PINT-AE's UNCL 7161 list. A value pasted as "FC - Freight
		service" is saved as FC."""
		reasons = code_lists.codes(code_lists.CHARGE_REASONS)
		rows = [(self, "negative_discount_reason_code", _("Reason Code for Negative Discounts"))]
		rows += [
			(row, "reason_code", _("Charge row {0}: reason code").format(row.idx))
			for row in self.get("charge_accounts") or []
		]
		for doc, field, label in rows:
			code = codes.code_of((doc.get(field) or "").strip())
			doc.set(field, code)
			if code and code not in reasons:
				frappe.throw(
					_(
						"{0} {1} is not in PINT-AE's UNCL 7161 list of charge reasons. Pick one from the list."
					).format(label, code)
				)

	def validate_ready(self):
		oauth = self.auth_method == "OAuth Client"
		required = {
			"base_url": _("Base URL"),
			**(
				{"client_id": _("Client ID"), "client_secret": _("Client Secret")}
				if oauth
				else {"api_key": _("API Key")}
			),
			"participant_id": _("Participant ID"),
			"start_date": _("Start Date"),
			"payment_means_code": _("Payment Means"),
			"uom_code_list": _("UOM Code List"),
		}
		missing = [label for field, label in required.items() if not self.get(field)]
		if missing:
			frappe.throw(_("Fill these in before enabling e-invoicing: {0}").format(", ".join(missing)))
		if (
			codes.code_of(self.payment_means_code) in codes.PAYMENT_MEANS_NEEDING_ACCOUNT
			and not self.payment_account_id
		):
			frappe.throw(_("Credit transfer needs the IBAN the buyer pays into."))
		if not self.tax_treatments:
			frappe.throw(_("Map at least one tax template before enabling e-invoicing."))
		if self.status_updates == "Webhook" and not self.webhook_subscription_id:
			frappe.throw(
				_("Register the webhook with Flick first, or read status updates from the events feed.")
			)

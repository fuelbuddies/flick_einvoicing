"""Which invoices are e-invoiced at all."""

import frappe
from frappe.utils import formatdate, getdate

SETTINGS = "E Invoicing Settings"


def settings_for(company: str | None):
	if not company or not frappe.db.exists(SETTINGS, company):
		return None
	return frappe.get_cached_doc(SETTINGS, company)


def skip_reason(invoice, settings) -> str | None:
	"""Why this invoice gets no submission, or None when it must be filed."""
	if not settings or not settings.enabled:
		return "E-invoicing is not enabled for this company."
	if invoice.doctype == "Purchase Invoice":
		# Only self-billing makes a purchase invoice ours to file; otherwise the supplier files it.
		if invoice.get("einvoice_received"):
			return "Received over Peppol: the supplier filed it."
		if not frappe.get_cached_value("Supplier", invoice.supplier, "einvoice_self_billing"):
			return "No self-billing agreement with this supplier."
		if settings.skip_internal_customers and invoice.get("is_internal_supplier"):
			return "Internal supplier, excluded in E Invoicing Settings."
	if invoice.get("is_opening") == "Yes":
		return "Opening invoices record balances, not supplies."
	if settings.start_date and getdate(invoice.posting_date) < getdate(settings.start_date):
		return f"Posted before the e-invoicing start date, {formatdate(settings.start_date)}."
	if invoice.get("is_consolidated"):
		return "Consolidated from POS invoices, which are filed one by one."
	if settings.skip_internal_customers and invoice.get("is_internal_customer"):
		return "Internal customer, excluded in E Invoicing Settings."
	if (
		invoice.doctype != "Purchase Invoice"
		and settings.skip_b2c
		and frappe.get_cached_value("Customer", invoice.customer, "customer_type") == "Individual"
	):
		return "B2C sale (customer type Individual), out of scope for now."
	return None

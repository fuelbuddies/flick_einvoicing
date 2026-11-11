"""TRN and Peppol ID checks shared by the customer data form and the Customer form, so a wrong
number is caught where it is typed instead of when the first invoice is sent."""

import re

import frappe
from frappe import _

from flick_einvoicing.einvoice import codes

NOT_REGISTERED = ("NA", "N/A")


def _digits(value: str) -> str:
	return re.sub(r"[\s\-]", "", value or "")


def normalize_trn(value: str) -> str | None:
	"""A 15-digit TRN with spaces and dashes removed; None for "NA". Raises on anything else."""
	value = (value or "").strip()
	if value.upper() in NOT_REGISTERED:
		return None
	trn = _digits(value)
	if not re.match(codes.TRN_PATTERN, trn):
		frappe.throw(_("TRN {0} should be exactly 15 digits.").format(value), title=_("Invalid TRN"))
	return trn


def normalize_peppol_id(value: str) -> str:
	"""0235: plus the 10-digit TIN starting with 1. Accepts the bare TIN. Raises on anything else."""
	value = (value or "").strip()
	tin = _digits(value.split(":", 1)[1] if value.startswith("0235:") else value)
	if re.match(r"^\d{15}$", tin):
		frappe.throw(
			_("{0} looks like a 15-digit TRN. The e-invoicing TIN is the 10-digit number starting with 1.").format(
				value
			),
			title=_("Invalid TIN"),
		)
	peppol_id = f"0235:{tin}"
	if not re.match(codes.PEPPOL_ID_PATTERN, peppol_id):
		frappe.throw(
			_("{0} is not a valid e-invoicing TIN. It should be 10 digits starting with 1, e.g. 1234567890.").format(
				value
			),
			title=_("Invalid TIN"),
		)
	return peppol_id


def validate_customer(doc, method=None):
	"""Check TRN and Peppol ID on UAE (Peppol-routed) customers, only when they are changed, so
	existing customers with old values can still be edited."""
	if (doc.get("einvoice_routing") or codes.ROUTING_PEPPOL) != codes.ROUTING_PEPPOL:
		return
	if doc.get("tax_id") and (doc.is_new() or doc.has_value_changed("tax_id")):
		doc.tax_id = normalize_trn(doc.tax_id)
	if doc.get("einvoice_peppol_id") and (doc.is_new() or doc.has_value_changed("einvoice_peppol_id")):
		doc.einvoice_peppol_id = normalize_peppol_id(doc.einvoice_peppol_id)

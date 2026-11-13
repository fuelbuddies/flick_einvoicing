"""The letter you send suppliers before go-live: your company's buyer details for their
e-invoices to you, on your letterhead. Printed from Company with the Supplier E-Invoicing Letter
print format, which reads letter_data()."""

import frappe
from frappe import _

from flick_einvoicing.einvoice import codes

SETTINGS = "E Invoicing Settings"


def letter_data(company: str) -> dict:
	"""The rows of the letter and the details around them, with what is missing."""
	from erpnext.setup.doctype.company.company import get_default_company_address

	c = frappe.get_cached_doc("Company", company)
	address = None
	name = get_default_company_address(company)
	if name:
		address = frappe.get_cached_doc("Address", name)
	settings = frappe.db.get_value(SETTINGS, company, ["seller_contact_email"], as_dict=True) or {}
	emirate = address.get("emirate") if address else None

	street = ", ".join(x for x in ((address.address_line1, address.address_line2) if address else ()) if x)
	rows = [
		(_("Legal Entity Name"), c.company_name),
		(_("Peppol ID"), c.get("einvoice_peppol_id")),
		(_("VAT Number (TRN)"), c.tax_id),
		(_("Trade Licence Number"), c.get("einvoice_trade_licence")),
		(_("Trade Licence Issuing Authority"), c.get("einvoice_trade_licence_authority")),
		(_("Street Name (Address Line 1)"), street),
		(_("City Name (Address Line 2)"), address.city if address else None),
		(_("Emirate Code (Country Subentity)"), codes.EMIRATE_CODES.get(emirate, emirate)),
		(_("Country"), (address.country if address else None) or c.country),
	]
	phase = (c.get("einvoice_phase") or "").split(" –")[0]
	return {
		"company": c.company_name,
		"rows": rows,
		"missing": [label for label, value in rows if not value],
		"phase": phase,
		"go_live": c.get("einvoice_go_live_date"),
		"email": settings.get("seller_contact_email") or c.email,
	}


@frappe.whitelist()
def check(company: str) -> list[str]:
	"""What the letter would leave blank, for the Company form to warn about before printing."""
	frappe.has_permission("Company", "read", company, throw=True)
	return letter_data(company)["missing"]

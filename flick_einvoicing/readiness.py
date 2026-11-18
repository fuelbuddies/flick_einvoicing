# Copyright (c) 2026, Techountant and contributors
# For license information, please see license.txt

"""E-Invoicing Readiness page: which customers and suppliers are ready for e-invoicing, what each is missing, and (for
customers) where their data collection form stands."""

import re

import frappe
from frappe import _

from flick_einvoicing.einvoice import codes

PEPPOL_ID = re.compile(codes.PEPPOL_ID_PATTERN)
TRN = re.compile(codes.TRN_PATTERN)


def data(party_type, filters):
	filters = frappe._dict(filters or {})
	fields = ["name", "tax_id", "einvoice_peppol_id", "einvoice_trade_licence", "einvoice_trade_licence_authority"]
	if party_type == "Customer":
		fields += ["customer_name as party_name", "einvoice_routing", "einvoice_party_identifier",
			"einvoice_free_zone", "einvoice_phase", "einvoice_go_live_date", "einvoice_contact_email"]
	else:
		fields += ["supplier_name as party_name", "einvoice_self_billing"]
	conditions = {"disabled": 0}
	if filters.party:
		conditions["name"] = filters.party
	if party_type == "Customer" and filters.customer_group:
		conditions["customer_group"] = filters.customer_group
	if party_type == "Supplier" and filters.supplier_group:
		conditions["supplier_group"] = filters.supplier_group
	parties = frappe.get_list(party_type, filters=conditions, fields=fields, order_by="name", limit_page_length=0)

	requests = {}
	if party_type == "Customer" and parties:
		for r in frappe.get_all(
			"Customer Data Request",
			filters={"customer": ["in", [p.name for p in parties]]},
			fields=["name", "customer", "status", "creation", "submitted_on", "sent_on", "send_count"],
			order_by="creation desc",
		):
			requests.setdefault(r.customer, r)

	emails = {}
	if party_type == "Customer" and parties:
		from flick_einvoicing.data_form import contact_emails

		emails = contact_emails([p.name for p in parties])

	rows = []
	for p in parties:
		missing = check(party_type, p)
		status = "Ready" if not missing else "Incomplete"
		if party_type == "Supplier" and not p.einvoice_self_billing and not missing:
			status = "Ready"
		if filters.only_incomplete and status == "Ready":
			continue
		row = {
			"party": p.name,
			"party_name": p.party_name,
			"status": status,
			"missing": ", ".join(missing),
			"peppol_id": p.einvoice_peppol_id,
			"tax_id": p.tax_id,
			"trade_licence": p.einvoice_trade_licence,
		}
		if party_type == "Customer":
			req = requests.get(p.name)
			row.update(
				routing=p.einvoice_routing or codes.ROUTING_PEPPOL,
				free_zone=p.einvoice_free_zone,
				phase=(p.einvoice_phase or "").split(" –")[0],
				go_live=p.einvoice_go_live_date,
				contact_email=p.einvoice_contact_email,
				send_to=emails.get(p.name, (None, None))[0],
				send_to_source=emails.get(p.name, (None, None))[1],
				form_status=req.status if req else "",
				form_request=req.name if req else None,
				form_date=(req.submitted_on or req.creation) if req else None,
				sent_on=req.sent_on if req else None,
				send_count=req.send_count if req else 0,
			)
		else:
			row["self_billing"] = p.einvoice_self_billing
		rows.append(row)
	return rows


def check(party_type, p) -> list[str]:
	missing = []
	routing = (p.get("einvoice_routing") or codes.ROUTING_PEPPOL) if party_type == "Customer" else codes.ROUTING_PEPPOL
	needs_peppol = routing == codes.ROUTING_PEPPOL and (party_type == "Customer" or p.einvoice_self_billing)
	if needs_peppol:
		if not p.einvoice_peppol_id:
			missing.append(_("Peppol ID"))
		elif not PEPPOL_ID.match(p.einvoice_peppol_id.strip()):
			missing.append(_("Peppol ID format (0235: + 10 digits starting with 1)"))
	if p.tax_id and not TRN.match(re.sub(r"\s", "", p.tax_id)):
		missing.append(_("TRN format (15 digits)"))
	if party_type == "Customer" and routing == codes.ROUTING_PEPPOL and not p.tax_id:
		missing.append(_("TRN (or confirm not VAT registered)"))
	if routing != codes.ROUTING_PEPPOL and not (p.get("einvoice_party_identifier") or p.einvoice_trade_licence):
		missing.append(_("Buyer Identifier or Trade Licence"))
	if party_type == "Supplier" and p.einvoice_self_billing and not p.einvoice_trade_licence:
		missing.append(_("Trade Licence (self-billing)"))
	if p.einvoice_trade_licence and not p.einvoice_trade_licence_authority:
		missing.append(_("Licensing Authority"))
	return missing


def address_gaps(party_type: str, name: str, abroad_ok: bool) -> list[str]:
	"""What the party's primary address lacks for an e-invoice; the invoice may still pick another."""
	from frappe.contacts.doctype.address.address import get_default_address

	address_name = get_default_address(party_type, name)
	if not address_name:
		return [_("Address")]
	address = frappe.get_cached_doc("Address", address_name)
	gaps = [
		_("Address {0}").format(label)
		for field, label in (("address_line1", _("Line 1")), ("city", _("City/Town")), ("country", _("Country")))
		if not address.get(field)
	]
	in_uae = address.country and frappe.get_cached_value("Country", address.country, "code") == "ae"
	if address.country and not in_uae and not abroad_ok:
		gaps.append(_("Address outside the UAE (set E-Invoice Routing for a buyer abroad)"))
	if in_uae and frappe.get_meta("Address").has_field("emirate") and not address.get("emirate"):
		gaps.append(_("Address Emirate"))
	return gaps


@frappe.whitelist()
def party_status(doctype: str, name: str) -> dict:
	"""For the Customer and Supplier forms: what this party still needs before it can be e-invoiced."""
	if doctype not in ("Customer", "Supplier"):
		frappe.throw(_("Not a party: {0}").format(doctype))
	party = frappe.get_doc(doctype, name)
	party.check_permission("read")
	if not frappe.db.exists("E Invoicing Settings", {"enabled": 1}):
		return {"applies": False}
	if doctype == "Supplier" and not party.get("einvoice_self_billing"):
		return {"applies": False}  # suppliers matter only when we bill for them
	missing = check(doctype, party)
	routing = (party.get("einvoice_routing") or codes.ROUTING_PEPPOL) if doctype == "Customer" else codes.ROUTING_PEPPOL
	missing += address_gaps(doctype, name, abroad_ok=routing != codes.ROUTING_PEPPOL)
	return {"applies": True, "missing": missing}


@frappe.whitelist()
def get_rows(party_type: str = "Customer", filters: dict | str | None = None) -> list[dict]:
	party_type = party_type if party_type in ("Customer", "Supplier") else "Customer"
	frappe.has_permission(party_type, "read", throw=True)
	return data(party_type, frappe.parse_json(filters) if isinstance(filters, str) else filters)

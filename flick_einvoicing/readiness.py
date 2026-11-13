# Copyright (c) 2026, Techountant and contributors
# For license information, please see license.txt

"""E-Invoicing Readiness page: which customers and suppliers are ready for e-invoicing, what each is missing, and (for
customers) where their data collection form stands."""

import re

import frappe
from frappe import _

from flick_einvoicing.einvoice import codes

PEPPOL_ID = re.compile(r"^0235:\d{10}$")
TRN = re.compile(r"^\d{15}$")


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
			missing.append(_("Peppol ID format (0235:+10 digits)"))
	if p.tax_id and not TRN.match(re.sub(r"\s", "", p.tax_id)):
		missing.append(_("TRN format (15 digits)"))
	if party_type == "Customer" and routing == codes.ROUTING_PEPPOL and not p.tax_id:
		missing.append(_("TRN (or confirm not VAT registered)"))
	if routing != codes.ROUTING_PEPPOL and not (p.get("einvoice_party_identifier") or p.einvoice_trade_licence):
		missing.append(_("Buyer Identifier or Trade Licence"))
	if p.einvoice_trade_licence and not p.einvoice_trade_licence_authority:
		missing.append(_("Licensing Authority"))
	return missing


@frappe.whitelist()
def get_rows(party_type: str = "Customer", filters: dict | str | None = None) -> list[dict]:
	party_type = party_type if party_type in ("Customer", "Supplier") else "Customer"
	frappe.has_permission(party_type, "read", throw=True)
	return data(party_type, frappe.parse_json(filters) if isinstance(filters, str) else filters)

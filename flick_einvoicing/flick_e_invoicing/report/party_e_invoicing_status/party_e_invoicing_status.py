# Copyright (c) 2026, Techountant and contributors
# For license information, please see license.txt

"""Which customers and suppliers are ready for e-invoicing, what each is missing, and (for
customers) where their data collection form stands."""

import re

import frappe
from frappe import _

from flick_einvoicing.einvoice import codes

PEPPOL_ID = re.compile(r"^0235:\d{10}$")
TRN = re.compile(r"^\d{15}$")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	party_type = filters.party_type if filters.party_type in ("Customer", "Supplier") else "Customer"
	return columns(party_type), data(party_type, filters)


def columns(party_type):
	cols = [
		{"fieldname": "party", "label": _(party_type), "fieldtype": "Link", "options": party_type, "width": 180},
		{"fieldname": "party_name", "label": _("Name"), "fieldtype": "Data", "width": 200},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 100},
		{"fieldname": "missing", "label": _("Missing / To Check"), "fieldtype": "Data", "width": 260},
	]
	if party_type == "Customer":
		cols.append({"fieldname": "routing", "label": _("Routing"), "fieldtype": "Data", "width": 140})
	else:
		cols.append({"fieldname": "self_billing", "label": _("Self-Billing"), "fieldtype": "Check", "width": 90})
	cols += [
		{"fieldname": "peppol_id", "label": _("Peppol ID"), "fieldtype": "Data", "width": 150},
		{"fieldname": "tax_id", "label": _("TRN"), "fieldtype": "Data", "width": 140},
		{"fieldname": "trade_licence", "label": _("Trade Licence"), "fieldtype": "Data", "width": 120},
	]
	if party_type == "Customer":
		cols += [
			{"fieldname": "free_zone", "label": _("Free Zone"), "fieldtype": "Check", "width": 80},
			{"fieldname": "phase", "label": _("Phase"), "fieldtype": "Data", "width": 90},
			{"fieldname": "go_live", "label": _("Go-Live"), "fieldtype": "Date", "width": 100},
			{"fieldname": "contact_email", "label": _("Contact Email"), "fieldtype": "Data", "width": 170},
			{"fieldname": "form_status", "label": _("Data Form"), "fieldtype": "Data", "width": 100},
			{"fieldname": "form_request", "label": _("Form Request"), "fieldtype": "Link", "options": "Customer Data Request", "width": 110},
			{"fieldname": "form_date", "label": _("Form Date"), "fieldtype": "Datetime", "width": 150},
			{"fieldname": "action", "label": _("Action"), "fieldtype": "Data", "width": 120},
		]
	return cols


def data(party_type, filters):
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
			fields=["name", "customer", "status", "creation", "submitted_on"],
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
				action="",
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

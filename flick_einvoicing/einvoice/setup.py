"""What each UAE company still needs before it can e-invoice: the checklist shown on the
E Invoicing Settings list and form, and on the Flick workspace.

Every company that issues e-invoices needs its own E Invoicing Settings record, named after
the company. The steps follow what the settings refuse to enable without, plus the one gap
that blocks invoices later: VAT templates with no tax treatment."""

import frappe
from frappe import _

from flick_einvoicing.einvoice import codes

SETTINGS = "E Invoicing Settings"
UAE = "United Arab Emirates"
# The templates an e-invoice reads VAT treatment from. Purchase templates matter only for
# self-billing, which few clients use, so they are not part of the checklist.
TEMPLATE_TYPES = ("Sales Taxes and Charges Template", "Item Tax Template")
LISTED_TEMPLATES = 3


def overview(company: str | None = None, details: bool = True) -> list[dict]:
	"""One row per UAE company the user can see. Without `details`, only whether it is set up."""
	filters = {"country": UAE, **({"name": company} if company else {})}
	rows = []
	for name in frappe.get_list("Company", filters=filters, pluck="name", order_by="name"):
		settings = frappe.get_doc(SETTINGS, name) if frappe.db.exists(SETTINGS, name) else None
		row = {"company": name, "record": bool(settings), "enabled": bool(settings and settings.enabled)}
		if settings:
			row.update(
				environment=settings.environment,
				submission_mode=settings.submission_mode,
				status_updates=settings.status_updates,
				events_synced_at=settings.events_synced_at,
			)
		if details:
			row["steps"] = steps(name, settings)
			row["warnings"] = warnings(name)
		rows.append(row)
	return rows


def steps(company: str, settings) -> list[dict]:
	s = settings
	if s and s.auth_method == "OAuth Client":
		credential = bool(s.client_id and s.client_secret)
	else:
		credential = bool(s and s.api_key)
	needs_iban = bool(
		s
		and codes.code_of(s.payment_means_code) in codes.PAYMENT_MEANS_NEEDING_ACCOUNT
		and not s.payment_account_id
	)
	unmapped = unmapped_templates(company, s)
	result = [
		_step(_("Create this company's E Invoicing Settings record"), bool(s)),
		_step(
			_("Connect to Flick: Base URL, credential and Participant ID, then Test Connection"),
			bool(s and s.base_url and credential and s.participant_id),
		),
		_step(
			_("Set the start date and the default payment means"),
			bool(s and s.start_date and s.payment_means_code) and not needs_iban,
			_("Credit transfer needs the IBAN the buyer pays into.") if needs_iban else None,
		),
		_step(
			_("Map every VAT template under Tax Treatments"),
			bool(s and s.tax_treatments) and not unmapped,
			# ERPNext makes Excise templates for every UAE company; most never use them.
			_("Not mapped yet: {0}. Disable any this company doesn't use.").format(_listed(unmapped))
			if unmapped
			else None,
		),
	]
	if s and s.status_updates == "Webhook":
		result.append(_step(_("Register the webhook (Webhook menu)"), bool(s.webhook_subscription_id)))
	result.append(_step(_("Tick Enabled"), bool(s and s.enabled)))
	return result


def warnings(company: str) -> list[str]:
	if frappe.db.exists(
		"Sales Taxes and Charges Template", {"company": company, "is_default": 1, "disabled": 0}
	):
		return []
	return [
		_(
			"No sales tax template is marked Default, so invoices can be saved without one, and those "
			"cannot be e-invoiced. Mark the template most sales use as Default."
		)
	]


def unmapped_templates(company: str, settings) -> list[str]:
	mapped = {(row.template_type, row.template) for row in (settings.tax_treatments if settings else [])}
	return [
		name
		for doctype in TEMPLATE_TYPES
		for name in frappe.get_all(doctype, filters={"company": company, "disabled": 0}, pluck="name")
		if (doctype, name) not in mapped
	]


def _step(label: str, done: bool, detail: str | None = None) -> dict:
	return {"label": label, "done": done, "detail": detail}


def _listed(names: list[str]) -> str:
	shown = ", ".join(names[:LISTED_TEMPLATES])
	more = len(names) - LISTED_TEMPLATES
	return _("{0} and {1} more").format(shown, more) if more > 0 else shown

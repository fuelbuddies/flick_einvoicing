"""Sales Invoice, POS Invoice and (self-billed) Purchase Invoice hooks."""

import frappe
from frappe import _

from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.payload import invoice_problems
from flick_einvoicing.einvoice.scope import settings_for, skip_reason
from flick_einvoicing.einvoice.service import (
	DOCTYPE,
	active_submission,
	create_submission,
	validate_with_flick,
)


def before_insert(doc, method=None):
	default_export(doc)
	default_free_zone(doc)


def default_free_zone(doc):
	"""Tick Free Trade Zone Supply on a new invoice to a customer marked as a free zone company,
	with the customer's TRN as the beneficiary. Never unticks; a person's choice stands."""
	if doc.doctype not in ("Sales Invoice", "POS Invoice") or doc.get("einvoice_is_ftz") or not doc.get("customer"):
		return
	free_zone, tax_id = frappe.get_cached_value("Customer", doc.customer, ["einvoice_free_zone", "tax_id"]) or (0, None)
	if not free_zone:
		return
	doc.einvoice_is_ftz = 1
	if not doc.get("einvoice_fz_beneficiary_id") and tax_id and tax_id.strip().upper() not in ("NA", "N/A"):
		doc.einvoice_fz_beneficiary_id = tax_id.strip()


def default_export(doc):
	"""Tick Export on a new sales invoice to a buyer abroad when the supply goes outside the UAE.

	Flick's is_export is a supply of goods or services outside the country, with a delivery address
	abroad. The shipping address decides; without one (services, usually) the buyer's billing
	address abroad becomes the delivery address. Goods delivered inside the UAE stay unticked.
	Never unticks; a person's choice stands."""
	from flick_einvoicing.einvoice import codes

	if doc.doctype != "Sales Invoice" or doc.get("einvoice_is_export") or not doc.get("customer"):
		return
	if frappe.get_cached_value("Customer", doc.customer, "einvoice_routing") != codes.ROUTING_EXPORT:
		return
	address = doc.get("shipping_address_name") or doc.get("customer_address")
	country = address and frappe.db.get_value("Address", address, "country")
	if country and country != "United Arab Emirates":
		if not doc.get("shipping_address_name"):
			doc.shipping_address_name = address
		doc.einvoice_is_export = 1


def before_submit(doc, method=None):
	"""Block the submit while anything the e-invoice needs is missing.

	Runs at submit rather than validate, so drafts can still be saved half-finished.
	"""
	settings = settings_for(doc.company)
	if skip_reason(doc, settings):
		return
	problems = invoice_problems(doc, settings)
	if not problems and settings.get("validate_before_submit"):
		valid, issues = validate_with_flick(doc, settings)
		if valid is None:
			# Flick couldn't be asked: don't hold up the business, the send will tell.
			frappe.msgprint(issues[0], title=_("Not checked with Flick"), indicator="orange")
		elif not valid:
			problems = issues
	if problems:
		items = "".join(f"<li>{frappe.utils.escape_html(p)}</li>" for p in problems)
		frappe.throw(
			_("This invoice can't be e-invoiced yet:") + f"<ul>{items}</ul>",
			title=_("UAE e-invoicing"),
		)


def on_submit(doc, method=None):
	settings = settings_for(doc.company)
	if skip_reason(doc, settings):
		return
	sub = create_submission(doc)
	if settings.submission_mode == "Live":
		# Only after the invoice's own transaction commits, so a failed submit sends nothing.
		frappe.enqueue(
			"flick_einvoicing.jobs.send_one",
			queue="short",
			name=sub.name,
			enqueue_after_commit=True,
		)


def before_cancel(doc, method=None):
	sub = active_submission(doc.doctype, doc.name)
	if not sub:
		return
	# Lock the row so a sender can't pick it up between this check and on_cancel.
	frappe.db.get_value(DOCTYPE, sub.name, "name", for_update=True)
	sub.reload()
	allowed, reason = st.can_cancel_invoice(sub)
	if not allowed:
		frappe.throw(reason, title=_("UAE e-invoicing"))


def on_cancel(doc, method=None):
	sub = active_submission(doc.doctype, doc.name)
	if not sub:
		return
	sub.flags.ignore_permissions = True
	sub.flags.ignore_links = True  # the invoice it links to is being cancelled
	if sub.docstatus == 0:
		sub.status = st.CANCELLED
		sub.next_action = st.next_action(sub)
		sub.save()
	elif st.numbers_released(sub):
		# Frappe refuses to cancel an invoice a submitted document still links to, and checks
		# that after on_cancel, so the released submission is cancelled first.
		sub.cancel()
	frappe.db.set_value(doc.doctype, doc.name, "einvoice_status", sub.status, update_modified=False)

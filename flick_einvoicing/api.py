"""Buttons on E Invoicing Settings and E Invoice Submission."""

import frappe
from frappe import _

from flick_einvoicing.einvoice import service
from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.scope import settings_for
from flick_einvoicing.providers import get_client

SUBMISSION = "E Invoice Submission"


def _submission(name: str):
	frappe.has_permission(SUBMISSION, "write", doc=name, throw=True)
	return frappe.get_doc(SUBMISSION, name)


@frappe.whitelist()
def test_connection(company: str) -> dict:
	frappe.has_permission("E Invoicing Settings", "write", doc=company, throw=True)
	settings = frappe.get_doc("E Invoicing Settings", company)
	response = get_client(settings).list_participants()
	if not response.ok:
		frappe.throw(_("Flick answered {0}: {1}").format(response.status or "nothing", response.message))
	participants = (response.data or {}).get("participants") or []
	match = next((p for p in participants if p.get("id") == settings.participant_id), None)
	return {
		"participants": [
			{
				"id": p.get("id"),
				"name": p.get("registered_name"),
				"peppol_id": p.get("peppol_id"),
				"status": p.get("status"),
			}
			for p in participants
		],
		"participant_found": bool(match),
		"participant": match
		and {
			"name": match.get("registered_name"),
			"peppol_id": match.get("peppol_id"),
			"status": match.get("status"),
			"onboarding": match.get("onboarding"),
		},
	}


@frappe.whitelist()
def retry(name: str) -> str:
	sub = _submission(name)
	if sub.docstatus != 0 or sub.status not in (st.REJECTED, st.ERROR, st.QUEUED):
		frappe.throw(_("Only drafts that were rejected or failed to send can be retried."))
	try:
		return service.send(name, manual=True)
	except service.AuthFailure as e:
		frappe.db.commit()  # keep the saved Error before reporting it
		frappe.throw(_("Flick refused the credential: {0}").format(e))


@frappe.whitelist()
def check_on_flick(name: str) -> str:
	sub = _submission(name)
	if sub.docstatus == 0:
		return service.reconcile(sub)
	service.refresh(sub)
	return sub.status


@frappe.whitelist()
def resubmit(name: str) -> str:
	return service.resubmit(_submission(name))


@frappe.whitelist()
def sync_events_now(company: str) -> int:
	frappe.has_permission("E Invoicing Settings", "write", doc=company, throw=True)
	settings = settings_for(company)
	if not settings or not settings.enabled:
		frappe.throw(_("Enable e-invoicing for {0} first.").format(company))
	return service.sync_events(settings)


@frappe.whitelist()
def send_queued_now() -> None:
	frappe.only_for(("System Manager", "Accounts Manager"))
	frappe.enqueue(
		"flick_einvoicing.jobs.send_queued",
		queue="long",
		job_id="flick_einvoicing_send_queued",
		deduplicate=True,
	)

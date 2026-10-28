"""Buttons on the app's forms and on invoices, plus the webhook Flick posts to."""

import hashlib
import hmac
import json
from urllib.parse import quote

import frappe
from frappe import _

from flick_einvoicing.einvoice import inbound, service
from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.payload import invoice_problems
from flick_einvoicing.einvoice.scope import settings_for, skip_reason
from flick_einvoicing.providers import get_client
from flick_einvoicing.providers.flick import ARTIFACTS

SUBMISSION = "E Invoice Submission"
SETTINGS = "E Invoicing Settings"
# Roll-ups plus arrivals; subscribing to the granular types as well would notify twice.
WEBHOOK_EVENT_TYPES = ["document.completed", "document.failed", "document.received"]
INVOICE_DOCTYPES = ("Sales Invoice", "POS Invoice", "Purchase Invoice")


def _submission(name: str, permission: str = "write"):
	frappe.has_permission(SUBMISSION, permission, doc=name, throw=True)
	return frappe.get_doc(SUBMISSION, name)


def _settings(company: str):
	frappe.has_permission(SETTINGS, "write", doc=company, throw=True)
	settings = frappe.get_doc(SETTINGS, company)
	if not settings.base_url or not settings.participant_id:
		frappe.throw(_("Fill in the connection details for {0} first.").format(company))
	return settings


def _any_settings():
	"""Credentials to ask the Peppol network with: the user's company's, else any enabled."""
	company = frappe.defaults.get_user_default("Company")
	settings = settings_for(company) if company else None
	if settings and settings.enabled:
		return settings
	name = frappe.db.get_value(SETTINGS, {"enabled": 1}, "name")
	if not name:
		frappe.throw(_("Enable e-invoicing for a company first."))
	return frappe.get_doc(SETTINGS, name)


# Settings


@frappe.whitelist()
def test_connection(company: str) -> dict:
	settings = _settings(company)
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
def sync_events_now(company: str) -> int:
	settings = _settings(company)
	if not settings.enabled:
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


@frappe.whitelist()
def register_webhook(company: str) -> dict:
	settings = _settings(company)
	endpoint = f"{frappe.utils.get_url()}/api/method/flick_einvoicing.api.webhook?company={quote(company)}"
	if not endpoint.startswith("https://"):
		frappe.throw(_("Flick only posts to HTTPS. This site answers at {0}.").format(frappe.utils.get_url()))
	response = get_client(settings).create_webhook(
		name=f"ERPNext {frappe.local.site} {company}"[:100],
		endpoint=endpoint,
		event_types=WEBHOOK_EVENT_TYPES,
	)
	data = response.data if isinstance(response.data, dict) else {}
	if not response.ok or not data.get("secret"):
		frappe.throw(_("Flick did not register the webhook: {0}").format(response.message))
	# The secret is shown once: keep it now, encrypted.
	settings.webhook_subscription_id = data.get("uuid")
	settings.webhook_secret = data["secret"]
	settings.status_updates = "Webhook"
	settings.save()
	return {"subscription": data.get("uuid"), "endpoint": endpoint}


@frappe.whitelist()
def rotate_webhook_secret(company: str) -> None:
	settings = _settings(company)
	if not settings.webhook_subscription_id:
		frappe.throw(_("No webhook is registered."))
	response = get_client(settings).rotate_webhook_secret(settings.webhook_subscription_id)
	secret = (response.data or {}).get("secret") if isinstance(response.data, dict) else None
	if not response.ok or not secret:
		frappe.throw(_("Flick did not rotate the secret: {0}").format(response.message))
	settings.webhook_secret = secret
	settings.save()


@frappe.whitelist()
def remove_webhook(company: str) -> None:
	settings = _settings(company)
	if settings.webhook_subscription_id:
		response = get_client(settings).delete_webhook(settings.webhook_subscription_id)
		if not response.ok and response.status != 404:
			frappe.throw(_("Flick did not remove the webhook: {0}").format(response.message))
	settings.webhook_subscription_id = None
	settings.webhook_secret = None
	settings.status_updates = "Events Feed"
	settings.save()


@frappe.whitelist()
def webhook_deliveries(company: str) -> list:
	settings = _settings(company)
	if not settings.webhook_subscription_id:
		return []
	response = get_client(settings).webhook_deliveries(settings.webhook_subscription_id)
	if not response.ok:
		frappe.throw(_("Flick answered {0}: {1}").format(response.status or "nothing", response.message))
	data = response.data
	return (data.get("deliveries") if isinstance(data, dict) else data) or []


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(company: str):
	"""Flick's push. Trusted only once X-Flick-Signature (HMAC-SHA256 of the raw body under the
	subscription's secret) checks out."""
	settings = settings_for(company)
	secret = settings.get_password("webhook_secret", raise_exception=False) if settings else None
	raw = frappe.request.get_data() or b""
	signature = frappe.get_request_header("X-Flick-Signature") or ""
	expected = "sha256=" + hmac.new((secret or "").encode(), raw, hashlib.sha256).hexdigest()
	if not settings or not settings.enabled or not secret or not hmac.compare_digest(signature, expected):
		frappe.local.response.http_status_code = 401
		return {"ok": False}

	payload = json.loads(raw or b"{}")
	if payload.get("participant_id") not in (None, settings.participant_id):
		return {"ok": True, "ignored": True}

	frappe.set_user("Administrator")  # the signature, not a session, authenticates Flick
	try:
		service.apply_webhook(payload, settings)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="E-invoice webhook failed")
		frappe.local.response.http_status_code = 500  # Flick retries; we are idempotent
		return {"ok": False}
	return {"ok": True}


# Submissions


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
def download(name: str, kind: str = "pdf"):
	"""Flick's PDF, the UBL, or any stored artifact (signed SBD, MLS receipts, the TDD)."""
	sub = _submission(name, "read")
	if kind not in ("pdf", "xml", *ARTIFACTS):
		frappe.throw(_("Unknown file: {0}").format(kind))
	if not sub.document_id:
		frappe.throw(_("Flick has not accepted this document yet."))
	response = get_client(settings_for(sub.company)).get_file(sub.document_id, kind, submission=sub.name)
	if not response.ok or response.content is None:
		frappe.throw(_("Flick has no {0} for this document yet: {1}").format(kind, response.message))
	extension = "pdf" if kind == "pdf" else "xml"
	frappe.local.response.filename = f"{sub.document_identifier}-{kind.lower()}.{extension}"
	frappe.local.response.filecontent = response.content
	frappe.local.response.type = "download"


# Invoices and parties


@frappe.whitelist()
def check_invoice(doctype: str, name: str) -> dict:
	"""Run every check without submitting: ours, then Flick's full validation."""
	if doctype not in INVOICE_DOCTYPES:
		frappe.throw(_("Not an invoice: {0}").format(doctype))
	invoice = frappe.get_doc(doctype, name)
	invoice.check_permission("read")
	settings = settings_for(invoice.company)
	reason = skip_reason(invoice, settings)
	if reason:
		return {"skipped": reason}
	problems = invoice_problems(invoice, settings)
	if problems:
		return {"valid": False, "problems": problems, "checked_by": "this app"}
	valid, issues = service.validate_with_flick(invoice, settings)
	return {"valid": valid, "problems": issues, "checked_by": "Flick"}


@frappe.whitelist()
def check_peppol(doctype: str, name: str) -> dict:
	if doctype not in ("Customer", "Supplier"):
		frappe.throw(_("Peppol IDs live on customers and suppliers."))
	party = frappe.get_doc(doctype, name)
	party.check_permission("read")
	peppol_id = (party.get("einvoice_peppol_id") or "").strip()
	if not peppol_id:
		frappe.throw(_("{0} has no Peppol ID.").format(name))
	settings = _any_settings()
	response = get_client(settings).lookup(peppol_id)
	if not response.ok:
		frappe.throw(_("Flick answered {0}: {1}").format(response.status or "nothing", response.message))
	result = response.data if isinstance(response.data, dict) else {}
	result["pre_production"] = settings.environment != "Production"
	return result


# Received invoices


@frappe.whitelist()
def create_purchase_invoice(name: str) -> str:
	frappe.has_permission("E Invoice Received", "write", doc=name, throw=True)
	frappe.has_permission("Purchase Invoice", "create", throw=True)
	return inbound.create_purchase_invoice(name)


@frappe.whitelist()
def ignore_received(name: str) -> None:
	frappe.has_permission("E Invoice Received", "write", doc=name, throw=True)
	record = frappe.get_doc("E Invoice Received", name)
	if record.purchase_invoice:
		frappe.throw(_("A purchase invoice was already made from it."))
	record.status = "Ignored"
	record.save()

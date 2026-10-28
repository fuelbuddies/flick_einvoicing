"""The submission lifecycle: create, send, follow up. Used by hooks, jobs and buttons.

Nothing here commits; callers decide transaction boundaries (jobs commit per record).
"""

import json
import re

import frappe
from frappe.utils import add_to_date, convert_utc_to_system_timezone, get_datetime, now_datetime

from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.payload import InvoiceProblems, build_payload
from flick_einvoicing.einvoice.scope import settings_for
from flick_einvoicing.providers import get_client
from flick_einvoicing.providers.flick import RETENTION_RULE

DOCTYPE = "E Invoice Submission"
BACKOFF_MINUTES = (5, 15, 60, 180, 360)
AUTH_RETRY_MINUTES = 60


class AuthFailure(Exception):
	"""Flick refused the credential. Nothing else for that company will get through."""


# Creating and mirroring


def active_submission(invoice_doctype: str, invoice_name: str):
	name = frappe.db.get_value(
		DOCTYPE,
		{"invoice_doctype": invoice_doctype, "invoice": invoice_name, "docstatus": ("<", 2)},
		"name",
		order_by="creation desc",
	)
	return frappe.get_doc(DOCTYPE, name) if name else None


def create_submission(invoice):
	sub = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"invoice_doctype": invoice.doctype,
			"invoice": invoice.name,
			"company": invoice.company,
			"customer": invoice.customer,
			"issue_date": invoice.posting_date,
			"document_identifier": invoice.name,
			"currency": invoice.currency,
			"grand_total": abs(invoice.grand_total or 0),
			"status": st.QUEUED,
		}
	)
	sub.next_action = st.next_action(sub)
	sub.insert(ignore_permissions=True)
	mirror(sub)
	return sub


def mirror(sub):
	"""Copy the one-word status onto the invoice, for list views and the form."""
	frappe.db.set_value(
		sub.invoice_doctype,
		sub.invoice,
		{"einvoice_status": sub.status, "einvoice_submission": sub.name},
		update_modified=False,
	)


# Sending


def send(name: str, manual: bool = False) -> str | None:
	"""Send one draft submission. Safe to call twice: the row lock serialises senders, and
	?no_error makes Flick answer a number it already holds with that document."""
	row = frappe.db.get_value(DOCTYPE, name, ["docstatus", "status"], as_dict=True, for_update=True)
	if not row or row.docstatus != 0:
		return row.status if row else None
	if row.status not in st.SENDABLE and not (manual and row.status == st.REJECTED):
		return row.status

	sub = frappe.get_doc(DOCTYPE, name)
	settings = settings_for(sub.company)
	if not settings or not settings.enabled:
		return sub.status

	invoice = frappe.get_doc(sub.invoice_doctype, sub.invoice)
	try:
		payload = build_payload(invoice, settings)
	except InvoiceProblems as e:
		_save_draft(sub, st.REJECTED, validation_errors="\n".join(e.problems))
		return sub.status

	sub.request_payload = json.dumps(payload, indent=1)
	sub.document_type = payload.get("document_type")
	sub.attempt_count = (sub.attempt_count or 0) + 1
	sub.last_attempt = now_datetime()
	response = get_client(settings).submit(payload, submission=sub.name)
	return _after_submit(sub, response, settings)


def _after_submit(sub, response, settings) -> str:
	code = response.status
	data = response.data if isinstance(response.data, dict) else {}

	if code == 202 or (code == 200 and data.get("document_id")):
		return _accept(sub, data, settings, duplicate=bool(data.get("is_duplicate")))

	if code == 409:
		held = (response.body or {}).get("errors")
		if isinstance(held, dict) and held.get("document_id"):
			return _accept(sub, held, settings, duplicate=True)

	if code == 400:
		_save_draft(
			sub, st.REJECTED, validation_errors=format_issues(response), error_message=response.message
		)
		return sub.status

	if code in (401, 403):
		_save_draft(
			sub,
			st.ERROR,
			error_message=f"Flick refused the credential: {response.message}",
			retry_minutes=AUTH_RETRY_MINUTES,
		)
		raise AuthFailure(response.message)

	if code == 429:
		_save_draft(
			sub,
			st.ERROR,
			error_message="Flick rate limit reached.",
			retry_minutes=(response.retry_after or 60) / 60,
		)
		return sub.status

	if code == 500 and RETENTION_RULE in response.message:
		# Flick's old answer for a number it already holds: look it up rather than retry.
		return reconcile(sub, settings, after_retention_rule=True)

	# No answer, an HTML error page, or a 5xx: the document may or may not have landed.
	# Retrying is safe because the next POST adopts it if it did.
	attempt = max(sub.attempt_count or 1, 1)
	_save_draft(
		sub,
		st.ERROR,
		error_message=response.message,
		retry_minutes=BACKOFF_MINUTES[min(attempt, len(BACKOFF_MINUTES)) - 1],
	)
	return sub.status


def _accept(sub, data: dict, settings, duplicate: bool) -> str:
	if duplicate:
		differences = _held_by_another_document(sub, data, settings)
		if differences is None:
			# Couldn't read Flick's copy to compare; the next POST will answer the same way.
			_save_draft(
				sub, st.ERROR, error_message="Could not read Flick's copy of this number.", retry_minutes=15
			)
			return sub.status
		if differences:
			# Terminal: retrying would get the same answer. Nothing of ours was filed.
			_save_draft(
				sub,
				st.REJECTED,
				error_message=(
					f"Flick already holds number {sub.document_identifier} for a different document "
					f"({'; '.join(differences)}). It was not adopted. Check with Flick support."
				),
			)
			return sub.status
	sub.document_id = data.get("document_id") or data.get("id")
	sub.reporting_status = data.get("reporting_status")
	sub.exchange_status = data.get("exchange_status")
	sub.status = st.combined(sub.reporting_status, sub.exchange_status)
	sub.validation_errors = None
	sub.error_message = None
	sub.next_retry_at = None
	sub.response_payload = json.dumps(data, indent=1)
	sub.next_action = st.next_action(sub)
	sub.flags.ignore_permissions = True
	sub.submit()
	mirror(sub)
	if duplicate or sub.status != st.PROCESSING:
		# An adopted document may be further along; the detail read has the whole truth.
		refresh(sub, settings)
	return sub.status


def _held_by_another_document(sub, data: dict, settings) -> list[str] | None:
	"""Compare the document Flick holds under our number with what we would send.

	A duplicate answer only proves the number is taken. Adopting it blindly would mark
	someone else's document as this invoice's, for example on a shared test participant.
	Returns the differences, or None when Flick's copy could not be read.
	"""
	document_id = data.get("document_id") or data.get("id")
	response = get_client(settings).get_document(document_id, submission=sub.name)
	if not response.ok or not isinstance(response.data, dict):
		return None
	theirs = response.data.get("document") or {}
	ours = json.loads(sub.request_payload) if sub.request_payload else {}
	if not ours:
		return []  # nothing sent yet from here to compare with (reconciling an old attempt)

	checks = {
		"buyer": ("receiving_party", "peppol_id"),
		"document type": ("document_type",),
		"issue date": ("issue_date",),
		"currency": ("document_currency",),
		"amount due": ("invoice_totals", "payable_amount"),
	}
	differences = []
	for label, path in checks.items():
		mine, held = _dig(ours, path), _dig(theirs, path)
		if mine is None or held is None:
			continue
		if label == "amount due":
			same = _decimal(mine) == _decimal(held)
		else:
			same = str(mine) == str(held)
		if not same:
			differences.append(f"{label} {held!s} there, {mine!s} here")
	return differences


def _dig(data: dict, path: tuple):
	for key in path:
		if not isinstance(data, dict):
			return None
		data = data.get(key)
	return data


def _decimal(value):
	from decimal import Decimal, InvalidOperation

	try:
		return Decimal(str(value))
	except InvalidOperation:
		return value


def _save_draft(sub, status: str, validation_errors=None, error_message=None, retry_minutes=None):
	sub.status = status
	sub.validation_errors = validation_errors
	sub.error_message = error_message
	sub.next_retry_at = (
		add_to_date(now_datetime(), seconds=int(retry_minutes * 60)) if retry_minutes else None
	)
	sub.next_action = st.next_action(sub)
	_save(sub)


def _save(sub):
	# A submission's links are fixed when it is created. Re-checking them on every status
	# update would fail once the invoice is cancelled, which is allowed in some cases.
	sub.flags.ignore_permissions = True
	sub.flags.ignore_links = True
	sub.save()
	mirror(sub)


def format_issues(response) -> str:
	lines = []
	for issue in response.issues:
		message = re.sub(r"^\[[^\]]+\]-\s*", "", issue.get("message") or "")
		path = issue.get("path")
		where = f"{path}: " if path and path != "Document" else ""
		code = issue.get("code")
		lines.append(f"{where}{message}" + (f" ({code})" if code else ""))
	return "\n".join(lines) or response.message


# Following up


def refresh(sub, settings=None) -> bool:
	"""Read the document from Flick and take its state as the truth. True when applied."""
	settings = settings or settings_for(sub.company)
	if sub.docstatus != 1 or not sub.document_id or not settings:
		return False
	response = get_client(settings).get_document(sub.document_id, submission=sub.name)
	if response.ok and isinstance(response.data, dict):
		apply_detail(sub, response.data)
		return True
	return False


def apply_detail(sub, data: dict):
	reporting = data.get("reporting") or {}
	exchange = data.get("exchange") or {}
	errors = data.get("errors") or []
	sub.reporting_status = reporting.get("status") or sub.reporting_status
	sub.reporting_settled = 1 if reporting.get("settled") else 0
	sub.exchange_status = exchange.get("status") or sub.exchange_status
	sub.exchange_settled = 1 if exchange.get("settled") else 0
	sub.status = st.combined(sub.reporting_status, sub.exchange_status)
	sub.error_message = format_track_errors(errors) or None
	sub.next_action = st.next_action(sub, origins={e.get("origin") for e in errors if e.get("origin")})
	sub.response_payload = json.dumps({k: v for k, v in data.items() if k != "document"}, indent=1)
	_save_submitted(sub)


def format_track_errors(errors: list[dict]) -> str:
	lines = []
	for error in errors:
		where = (error.get("type") or "").capitalize() or "Flick"
		detail = ", ".join(x for x in (error.get("code"), error.get("origin")) if x)
		retry = "" if error.get("retryable") is None else (" Flick will retry." if error["retryable"] else "")
		lines.append(f"{where} ({detail}): {error.get('message') or ''}{retry}".strip())
	return "\n".join(lines)


def _save_submitted(sub):
	_save(sub)


def apply_event(event: dict, settings) -> bool:
	"""Apply one feed event. Returns False when the document is not one of ours."""
	document_id = event.get("document_id")
	name = document_id and frappe.db.get_value(DOCTYPE, {"document_id": document_id, "docstatus": 1}, "name")
	if not name:
		return False
	sub = frappe.get_doc(DOCTYPE, name)

	occurred = _system_time(event.get("occurred_at"))
	if occurred and sub.last_event_at and occurred < get_datetime(sub.last_event_at):
		return True  # replayed after a newer one; nothing to learn

	kind = event.get("type")
	reporting = exchange = None
	if kind == "reporting.status_changed":
		reporting = event.get("reporting")
	elif kind == "exchange.status_changed":
		exchange = event.get("exchange")
	elif kind == "document.completed":
		reporting, exchange = event.get("reporting"), event.get("exchange")
	elif kind != "document.failed":
		return True

	sub.reporting_status = st.merge_track(sub.reporting_status, reporting, st.REPORTING_DONE)
	sub.exchange_status = st.merge_track(sub.exchange_status, exchange, st.EXCHANGE_DONE)
	# A finished track is final, so it is settled; failures get their flag from the detail read.
	if sub.reporting_status in st.REPORTING_DONE:
		sub.reporting_settled = 1
	if sub.exchange_status in st.EXCHANGE_DONE:
		sub.exchange_settled = 1
	if occurred:
		sub.last_event_at = occurred

	sub.status = st.combined(sub.reporting_status, sub.exchange_status)
	if kind == "document.failed" or sub.status == st.FAILED:
		# Events don't say whether a failure is settled, and their snapshots can be stale:
		# only the detail read is trusted for failures.
		if refresh(sub, settings):
			return True
		if kind == "document.failed":
			sub.status = st.FAILED

	sub.next_action = st.next_action(sub)
	_save_submitted(sub)
	return True


def _system_time(value: str | None):
	parsed = st.parse_time(value)
	if not parsed:
		return None
	return convert_utc_to_system_timezone(parsed.replace(tzinfo=None)).replace(tzinfo=None)


def sync_events(settings, max_pages: int = 20) -> int:
	"""Read the outgoing feed forward from our cursor. Returns the number of events seen."""
	client = get_client(settings)
	cursor = settings.event_cursor or None
	seen = 0
	reset = False
	for _page in range(max_pages):
		response = client.events(cursor=cursor)
		if response.status == 400 and cursor and not reset:
			cursor, reset = None, True  # a cursor from another environment; start from Flick's position
			continue
		if not response.ok or not isinstance(response.data, dict):
			break

		events = response.data.get("events") or []
		for event in events:
			apply_event(event, settings)
		seen += len(events)

		pagination = response.data.get("pagination") or {}
		next_cursor = pagination.get("next_cursor") or cursor
		frappe.db.set_value(
			"E Invoicing Settings",
			settings.name,
			{"event_cursor": next_cursor, "events_synced_at": now_datetime()},
			update_modified=False,
		)
		if events and next_cursor and settings.ack_events:
			# Acknowledge only after our side is saved; callers commit before the next page.
			client.ack(next_cursor)
		cursor = next_cursor
		if not pagination.get("has_more"):
			break
	return seen


def reconcile(sub, settings=None, after_retention_rule: bool = False) -> str:
	"""Ask Flick whether a draft's number is already filed, and adopt the document if so."""
	settings = settings or settings_for(sub.company)
	if sub.docstatus != 0 or not settings:
		return sub.status
	response = get_client(settings).find_by_identifier(sub.document_identifier, submission=sub.name)
	if not response.ok:
		_save_draft(
			sub, st.ERROR, error_message=f"Could not check Flick: {response.message}", retry_minutes=15
		)
		return sub.status

	rows = (response.data or {}).get("documents") or []
	match = next((r for r in rows if r.get("document_identifier") == sub.document_identifier), None)
	if match:
		return _accept(
			sub,
			{
				"document_id": match.get("id"),
				"reporting_status": match.get("reporting_status"),
				"exchange_status": match.get("exchange_status"),
			},
			settings,
			duplicate=True,
		)
	if after_retention_rule:
		_save_draft(
			sub,
			st.ERROR,
			error_message="Flick says this number is already filed but no document with it was found. Check with Flick support.",
			retry_minutes=360,
		)
	else:
		# Confirmed not on Flick: safe to send again, and safe to cancel the invoice.
		_save_draft(sub, st.QUEUED, error_message=None)
	return sub.status


def resubmit(sub) -> str:
	"""Send a corrected copy under the same number, allowed only once Flick released it."""
	allowed, reason = st.can_resubmit(sub)
	if not allowed:
		frappe.throw(reason, title="Cannot resubmit")
	sub.flags.ignore_permissions = True
	sub.flags.allow_cancel = True
	sub.flags.ignore_links = True  # the submitted invoice still links here until mirror() moves it
	sub.cancel()

	copy = frappe.copy_doc(sub)
	copy.docstatus = 0  # copy_doc keeps the source's docstatus, here cancelled
	copy.amended_from = sub.name
	for field in (
		"document_id",
		"reporting_status",
		"exchange_status",
		"request_payload",
		"response_payload",
		"validation_errors",
		"error_message",
		"last_attempt",
		"last_event_at",
		"next_retry_at",
	):
		copy.set(field, None)
	copy.reporting_settled = copy.exchange_settled = 0
	copy.attempt_count = 0
	copy.status = st.QUEUED
	copy.next_action = st.next_action(copy)
	copy.insert(ignore_permissions=True)
	mirror(copy)
	return copy.name

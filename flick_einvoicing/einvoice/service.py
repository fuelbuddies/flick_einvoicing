"""The submission lifecycle: create, send, follow up. Used by hooks, jobs and buttons.

Nothing here commits; callers decide transaction boundaries (jobs commit per record).
"""

import json
import re
from decimal import Decimal, InvalidOperation

import frappe
from frappe.utils import add_to_date, convert_utc_to_system_timezone, get_datetime, now_datetime

from flick_einvoicing.einvoice import status as st
from flick_einvoicing.einvoice.payload import InvoiceProblems, build_payload
from flick_einvoicing.einvoice.scope import settings_for
from flick_einvoicing.providers import get_client
from flick_einvoicing.providers.flick import INCOMING, OUTGOING, RETENTION_RULE

DOCTYPE = "E Invoice Submission"
BACKOFF_MINUTES = (5, 15, 60, 180, 360)
AUTH_RETRY_MINUTES = 60
BULK_LIMIT = 50


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
			"customer": invoice.get("customer"),
			"supplier": invoice.get("supplier"),
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


def _claim(name: str, manual: bool = False):
	"""Lock a draft for sending. The row lock serialises senders; returns None when there is
	nothing to send."""
	row = frappe.db.get_value(DOCTYPE, name, ["docstatus", "status"], as_dict=True, for_update=True)
	if not row or row.docstatus != 0:
		return None
	if row.status not in st.SENDABLE and not (manual and row.status == st.REJECTED):
		return None
	return frappe.get_doc(DOCTYPE, name)


def _prepare(sub, settings) -> dict | None:
	invoice = frappe.get_doc(sub.invoice_doctype, sub.invoice)
	try:
		payload = build_payload(invoice, settings)
	except InvoiceProblems as e:
		_save_draft(sub, st.REJECTED, validation_errors="\n".join(e.problems))
		return None
	sub.request_payload = json.dumps(payload, indent=1)
	sub.document_type = payload.get("document_type")
	sub.attempt_count = (sub.attempt_count or 0) + 1
	sub.last_attempt = now_datetime()
	return payload


def send(name: str, manual: bool = False) -> str | None:
	"""Send one draft submission. Safe to call twice: the row lock serialises senders, and
	?no_error makes Flick answer a number it already holds with that document."""
	sub = _claim(name, manual)
	if not sub:
		return frappe.db.get_value(DOCTYPE, name, "status")
	settings = settings_for(sub.company)
	if not settings or not settings.enabled:
		return sub.status
	payload = _prepare(sub, settings)
	if payload is None:
		return sub.status
	response = get_client(settings).submit(payload, submission=sub.name)
	return _after_response(sub, response.status, response.data, response, settings)


def send_bulk(names: list[str], settings) -> None:
	"""Batch mode through the bulk endpoint: one request for up to 50 invoices, each still
	filed on its own and answered on its own."""
	subs, payloads = [], []
	for name in names[:BULK_LIMIT]:
		sub = _claim(name)
		if not sub:
			continue
		payload = _prepare(sub, settings)
		if payload is not None:
			subs.append(sub)
			payloads.append(payload)
	if not payloads:
		return

	response = get_client(settings).submit_bulk(payloads)
	results = (response.data or {}).get("results") if isinstance(response.data, dict) else None
	if response.status in (401, 403):
		for sub in subs:
			_save_draft(
				sub,
				st.ERROR,
				error_message=f"Flick refused the credential: {response.message}",
				retry_minutes=AUTH_RETRY_MINUTES,
			)
		raise AuthFailure(response.message)
	if not isinstance(results, list):
		# No per-document answer: none of them is known to have landed or not.
		for sub in subs:
			_after_response(sub, response.status, None, response, settings)
		return

	by_index = {result.get("index"): result for result in results if isinstance(result, dict)}
	for index, sub in enumerate(subs):
		result = by_index.get(index) or {}
		outcome = result.get("outcome")
		if outcome == "accepted":
			_accept(sub, result, settings, duplicate=False)
		elif outcome == "duplicate":
			_accept(sub, result, settings, duplicate=True)
		elif outcome == "rejected":
			_save_draft(
				sub,
				st.REJECTED,
				validation_errors=format_issues(result.get("errors") or []) or result.get("message"),
			)
		else:
			# failed, not_processed or missing: unknown, never "not filed". Resending is safe.
			_retry_later(sub, result.get("message") or f"Bulk outcome: {outcome or 'none'}")


def _after_response(sub, code, data, response, settings) -> str:
	data = data if isinstance(data, dict) else {}

	if code == 202 or (code == 200 and data.get("document_id")):
		return _accept(sub, data, settings, duplicate=bool(data.get("is_duplicate")))

	if code == 409:
		held = (response.body or {}).get("errors")
		if isinstance(held, dict) and held.get("document_id"):
			return _accept(sub, held, settings, duplicate=True)

	if code == 400:
		_save_draft(
			sub,
			st.REJECTED,
			validation_errors=format_issues(response.issues) or response.message,
			error_message=response.message,
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
	return _retry_later(sub, response.message)


def _retry_later(sub, message: str) -> str:
	attempt = max(sub.attempt_count or 1, 1)
	_save_draft(
		sub,
		st.ERROR,
		error_message=message,
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
	sub.flags.accepted_by_flick = True
	sub.submit()
	mirror(sub)
	if duplicate or sub.status != st.PROCESSING:
		# An adopted document may be further along; the detail read has the whole truth.
		refresh(sub, settings)
	attach_files(sub, settings)
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
		same = _decimal(mine) == _decimal(held) if label == "amount due" else str(mine) == str(held)
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


def format_issues(issues: list[dict]) -> str:
	lines = []
	for issue in issues:
		message = re.sub(r"^\[[^\]]+\]-\s*", "", issue.get("message") or "")
		path = issue.get("path")
		where = f"{path}: " if path and path != "Document" else ""
		code = issue.get("code")
		lines.append(f"{where}{message}" + (f" ({code})" if code else ""))
	return "\n".join(lines)


# Checking with Flick before anything is filed


def validate_with_flick(invoice, settings) -> tuple[bool | None, list[str]]:
	"""Run Flick's full validation on an invoice without filing it.

	/validate consumes the identifier it is given, so the real number is swapped for a
	throwaway one. Returns (True, []) when valid, (False, issues) when Flick refuses it, and
	(None, [reason]) when Flick could not be asked.
	"""
	try:
		payload = build_payload(invoice, settings)
	except InvoiceProblems as e:
		return False, e.problems
	payload["document_identifier"] = f"CHECK-{frappe.generate_hash(length=12).upper()}"
	response = get_client(settings).validate(payload)
	if response.ok:
		return True, []
	if response.status == 400:
		return False, format_issues(response.issues).splitlines() or [response.message]
	return None, [f"Flick could not validate it ({response.status or 'no answer'}): {response.message}"]


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
	sub.next_action = st.next_action(
		sub,
		origins={e.get("origin") for e in errors if e.get("origin")},
		codes={e.get("code") for e in errors if e.get("code")},
	)
	sub.response_payload = json.dumps({k: v for k, v in data.items() if k != "document"}, indent=1)
	_save(sub)


def format_track_errors(errors: list[dict]) -> str:
	lines = []
	for error in errors:
		where = (error.get("type") or "").capitalize() or "Flick"
		detail = ", ".join(x for x in (error.get("code"), error.get("origin")) if x)
		retry = " Flick will retry." if error.get("retryable") else ""
		lines.append(f"{where} ({detail}): {error.get('message') or ''}{retry}".strip())
	return "\n".join(lines)


def attach_files(sub, settings) -> bool:
	"""Keep Flick's rendering of the e-invoice with the ERPNext invoice: the PDF, and the UBL if
	asked for. Never stops the flow; refresh_stale tries again later."""
	if sub.files_attached or sub.docstatus != 1 or not sub.document_id:
		return True
	wanted = [
		kind for kind, on in (("pdf", settings.get("attach_pdf")), ("xml", settings.get("attach_xml"))) if on
	]
	if not wanted:
		return True
	client = get_client(settings)
	try:
		for kind in wanted:
			response = client.get_file(sub.document_id, kind, submission=sub.name)
			if not response.ok or response.content is None:
				return False
			file_name = f"{sub.document_identifier} e-invoice.{kind}"
			if not frappe.db.exists(
				"File",
				{
					"attached_to_doctype": sub.invoice_doctype,
					"attached_to_name": sub.invoice,
					"file_name": file_name,
				},
			):
				frappe.get_doc(
					{
						"doctype": "File",
						"file_name": file_name,
						"attached_to_doctype": sub.invoice_doctype,
						"attached_to_name": sub.invoice,
						"is_private": 1,
						"content": response.content,
					}
				).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title=f"E-invoice files could not be attached: {sub.name}")
		return False
	sub.files_attached = 1
	_save(sub)
	return True


# Status events, from the feed or from a webhook

# Webhook event types, mapped onto the feed's vocabulary: (feed type, which track it moves).
WEBHOOK_EVENTS = {
	"document.completed": ("document.completed", None),
	"document.failed": ("document.failed", None),
	"document.reporting.reported": ("reporting.status_changed", ("reporting", "reported")),
	"document.reporting.failed": ("reporting.status_changed", ("reporting", "failed")),
	"document.exchange.delivered": ("exchange.status_changed", ("exchange", "delivered")),
	"document.exchange.failed": ("exchange.status_changed", ("exchange", "failed")),
}


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
	_save(sub)
	return True


def apply_webhook(payload: dict, settings) -> bool:
	"""A webhook delivery, at least once and in no particular order: the same rules as the
	feed apply, keyed on the document and its own timestamp."""
	data = payload.get("data") or {}
	kind = payload.get("event")
	if kind == "document.received":
		from flick_einvoicing.einvoice import inbound

		inbound.receive(data.get("document_id"), settings)
		return True
	if kind not in WEBHOOK_EVENTS:
		return False
	feed_type, track = WEBHOOK_EVENTS[kind]
	event = {
		"type": feed_type,
		"document_id": data.get("document_id"),
		"occurred_at": payload.get("timestamp"),
		"reporting": data.get("reporting_status"),
		"exchange": data.get("exchange_status"),
	}
	if track:
		event[track[0]] = track[1]
		event["exchange" if track[0] == "reporting" else "reporting"] = None
	return apply_event(event, settings)


def _system_time(value: str | None):
	parsed = st.parse_time(value)
	if not parsed:
		return None
	return convert_utc_to_system_timezone(parsed.replace(tzinfo=None)).replace(tzinfo=None)


def sync_events(settings, max_pages: int = 20) -> int:
	"""Read the outgoing feed, and the incoming one when receiving invoices, forward from our
	cursors. Returns the number of events seen."""
	seen = _sync_feed(
		settings, OUTGOING, "event_cursor", lambda event: apply_event(event, settings), max_pages
	)
	if settings.get("receive_invoices"):
		from flick_einvoicing.einvoice import inbound

		def on_incoming(event):
			if event.get("type") == "document.received":
				inbound.receive(event.get("document_id"), settings)

		seen += _sync_feed(settings, INCOMING, "incoming_cursor", on_incoming, max_pages)
	return seen


def inbound_fetch_failed():
	from flick_einvoicing.einvoice.inbound import FetchFailed

	return FetchFailed


def _sync_feed(settings, direction: str, cursor_field: str, handle, max_pages: int) -> int:
	client = get_client(settings)
	cursor = settings.get(cursor_field) or None
	seen = 0
	reset = False
	for _page in range(max_pages):
		response = client.events(cursor=cursor, direction=direction)
		if response.status == 400 and cursor and not reset:
			cursor, reset = None, True  # a cursor from another environment; start from Flick's position
			continue
		if not response.ok or not isinstance(response.data, dict):
			break

		events = response.data.get("events") or []
		stopped = None
		for event in events:
			try:
				handle(event)
			except inbound_fetch_failed() as e:
				# Flick's guide: acknowledge only once our side is done. Stay on this event (resume
				# after the last one handled) so the next run reads it again; nothing is lost.
				frappe.log_error(f"E-invoice: incoming document not fetched, retried next run: {e}")
				stopped = event
				break
			seen += 1
		if stopped is not None:
			done = events.index(stopped)
			if done:
				frappe.db.set_value(
					"E Invoicing Settings",
					settings.name,
					{cursor_field: events[done - 1].get("id") or cursor, "events_synced_at": now_datetime()},
					update_modified=False,
				)
			break

		pagination = response.data.get("pagination") or {}
		next_cursor = pagination.get("next_cursor") or cursor
		frappe.db.set_value(
			"E Invoicing Settings",
			settings.name,
			{cursor_field: next_cursor, "events_synced_at": now_datetime()},
			update_modified=False,
		)
		if events and next_cursor and settings.ack_events:
			# Acknowledge only after our side is saved; callers commit before the next run.
			client.ack(next_cursor, direction=direction)
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
		# Flick's guide: treat this answer as "already filed, do not retry". Sending again never works.
		_save_draft(
			sub,
			st.REJECTED,
			validation_errors=(
				"Flick answered that this invoice number is blocked by a retention rule, but no document with "
				"it was found. Flick's developer guide (UAE, Known defects) says to treat that answer as "
				"\"already filed, do not retry\", so it is not sent again automatically. Give the invoice a new "
				"number (cancel and amend it), or ask Flick support about this number."
			),
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
	for fieldname in (
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
		copy.set(fieldname, None)
	copy.reporting_settled = copy.exchange_settled = copy.files_attached = 0
	copy.attempt_count = 0
	copy.status = st.QUEUED
	copy.next_action = st.next_action(copy)
	copy.insert(ignore_permissions=True)
	mirror(copy)
	return copy.name

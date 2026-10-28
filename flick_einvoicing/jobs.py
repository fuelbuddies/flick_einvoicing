"""Background work. Each record gets its own transaction, so one bad invoice can't stop a batch."""

import frappe
from frappe.utils import add_to_date, now_datetime

from flick_einvoicing.einvoice import service
from flick_einvoicing.einvoice import status as st

BATCH = 50


def _commit():
	if not frappe.flags.in_test:
		frappe.db.commit()


def _rollback():
	if not frappe.flags.in_test:
		frappe.db.rollback()


def _enabled_settings():
	return [
		frappe.get_doc("E Invoicing Settings", name)
		for name in frappe.get_all("E Invoicing Settings", filters={"enabled": 1}, pluck="name")
	]


def send_one(name: str):
	"""Live mode: enqueued right after the invoice is submitted."""
	try:
		service.send(name)
	except service.AuthFailure:
		pass  # the submission is saved as Error with the reason; the scheduler retries later
	_commit()


def send_queued():
	"""Batch mode, plus every retry that is due, company by company.

	Pages on (creation, name) rather than an offset: sending removes rows from the set being
	paged, so an offset would silently skip a page each round.
	"""
	for settings in _enabled_settings():
		last = None
		while True:
			rows = _due(settings.company, last)
			if not rows:
				break
			last = rows[-1]
			for row in rows:
				try:
					service.send(row.name)
					_commit()
				except service.AuthFailure:
					_commit()  # keep the Error record, stop this company until the next run
					rows = None
					break
				except Exception:
					_rollback()
					frappe.log_error(title=f"E-invoice send failed: {row.name}")
			if rows is None:
				break


def _due(company: str, after=None) -> list:
	table = frappe.qb.DocType("E Invoice Submission")
	query = (
		frappe.qb.from_(table)
		.select(table.name, table.creation)
		.where(
			(table.company == company)
			& (table.docstatus == 0)
			& table.status.isin(st.SENDABLE)
			& (table.next_retry_at.isnull() | (table.next_retry_at <= now_datetime()))
		)
		.orderby(table.creation)
		.orderby(table.name)
		.limit(BATCH)
	)
	if after:
		query = query.where(
			(table.creation > after.creation)
			| ((table.creation == after.creation) & (table.name > after.name))
		)
	return query.run(as_dict=True)


def sync_events():
	for settings in _enabled_settings():
		try:
			service.sync_events(settings)
			_commit()
		except Exception:
			_rollback()
			frappe.log_error(title=f"E-invoice events sync failed: {settings.company}")


def refresh_stale():
	"""Safety net: re-read documents still unfinished a day after their last change."""
	cutoff = add_to_date(now_datetime(), hours=-24)
	names = frappe.get_all(
		"E Invoice Submission",
		filters={"docstatus": 1, "status": ("in", (st.PROCESSING, st.FAILED)), "modified": ("<", cutoff)},
		pluck="name",
		limit=500,
	)
	for name in names:
		try:
			service.refresh(frappe.get_doc("E Invoice Submission", name))
			_commit()
		except Exception:
			_rollback()
			frappe.log_error(title=f"E-invoice refresh failed: {name}")

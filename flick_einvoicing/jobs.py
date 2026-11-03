"""Background work. Each record gets its own transaction, so one bad invoice can't stop a batch."""

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

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


def _batch_due(settings) -> bool:
	"""A Batch company sends once every Send Every (Minutes); Live companies (retries) every run."""
	if settings.submission_mode != "Batch":
		return True
	minutes = max(cint(settings.get("batch_interval")) or 10, 1)
	last = settings.get("last_batch_at")
	# 30 seconds' slack, so a job started a little early by the scheduler still counts.
	return not last or get_datetime(last) <= add_to_date(now_datetime(), minutes=-minutes, seconds=30)


def send_queued(force: bool = False):
	"""Batch mode, plus every retry that is due, company by company.

	Runs every minute; each Batch company sends at its own interval, unless forced from Send Now.
	Pages on (creation, name) rather than an offset: sending removes rows from the set being
	paged, so an offset would silently skip a page each round.
	"""
	for settings in _enabled_settings():
		if not (force or _batch_due(settings)):
			continue
		if settings.submission_mode == "Batch":
			frappe.db.set_value(
				"E Invoicing Settings", settings.name, "last_batch_at", now_datetime(), update_modified=False
			)
			_commit()
		if settings.submission_mode == "Batch" and settings.get("use_bulk"):
			_send_bulk(settings)
			continue
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


def _send_bulk(settings):
	last = None
	while True:
		rows = _due(settings.company, last)
		if not rows:
			return
		last = rows[-1]
		try:
			service.send_bulk([row.name for row in rows], settings)
			_commit()
		except service.AuthFailure:
			_commit()
			return
		except Exception:
			_rollback()
			frappe.log_error(title=f"E-invoice bulk send failed: {settings.company}")
			return


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

	# Flick's PDF and UBL for documents where attaching failed the first time.
	for row in frappe.get_all(
		"E Invoice Submission",
		filters={
			"docstatus": 1,
			"files_attached": 0,
			"modified": ("<", add_to_date(now_datetime(), hours=-1)),
		},
		fields=["name", "company"],
		limit=200,
	):
		settings = (
			frappe.get_cached_doc("E Invoicing Settings", row.company)
			if frappe.db.exists("E Invoicing Settings", row.company)
			else None
		)
		if not settings or not settings.enabled:
			continue
		try:
			service.attach_files(frappe.get_doc("E Invoice Submission", row.name), settings)
			_commit()
		except Exception:
			_rollback()
			frappe.log_error(title=f"E-invoice files failed: {row.name}")

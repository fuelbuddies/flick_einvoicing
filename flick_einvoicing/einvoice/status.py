"""The status model: Flick's two tracks, the one word we show for them, and the rules that
decide what a person may do next. Pure functions, so the rules can be tested on their own.

The rule that matters most: a failed track that is not settled is still being retried by
Flick. Reissuing then files a second document for the same sale under the client's TRN.
Nothing here ever reissues on its own; it only says whether a person may.
"""

from datetime import datetime, timezone

# Local statuses. The first four belong to drafts (never accepted by Flick).
QUEUED = "Queued"
REJECTED = "Rejected"
ERROR = "Error"
CANCELLED = "Cancelled"
PROCESSING = "Processing"
COMPLETED = "Completed"
FAILED = "Failed"

SENDABLE = (QUEUED, ERROR)
ACCEPTED = (PROCESSING, COMPLETED, FAILED)
# Refused, stuck on the way, or failed on a track: someone should look.
NEEDS_ATTENTION = (REJECTED, ERROR, FAILED)

REPORTING_DONE = {"reported", "not_required"}
EXCHANGE_DONE = {"delivered", "received", "not_required"}
TRACK_FAILED = "failed"

# Error origins Flick says leave the true state unknown (contact support, never reissue).
UNCERTAIN_ORIGINS = {"core", "region"}


# Where one track of an accepted document stands, for the workspace dashboard. A failed track
# Flick has not settled is "retrying": still running, like one in progress.
TRACK_STAGES = ("done", "not_required", "in_progress", "retrying", "failed", "cancelled")


def track_stage(state: str | None, settled) -> str:
	if state in ("reported", "delivered", "received"):
		return "done"
	if state in ("not_required", "cancelled"):
		return state
	if state == TRACK_FAILED:
		return "failed" if settled else "retrying"
	return "in_progress"


def merge_track(current: str | None, incoming: str | None, done: set[str]) -> str | None:
	"""A finished track never moves backwards.

	Events carry snapshots that can be stale for the track they are not about (an
	exchange event saying "delivered", then a reporting event a few ms later still saying
	exchange "processing"). A failed track may still move on, because Flick retries it.
	"""
	if not incoming:
		return current
	if current in done:
		return current
	return incoming


def combined(reporting: str | None, exchange: str | None) -> str:
	if TRACK_FAILED in (reporting, exchange):
		return FAILED
	if reporting in REPORTING_DONE and exchange in EXCHANGE_DONE:
		return COMPLETED
	return PROCESSING


def numbers_released(sub) -> bool:
	"""Flick releases a number only when its document concluded with neither track succeeding."""
	return (
		sub.reporting_status == TRACK_FAILED
		and sub.exchange_status == TRACK_FAILED
		and bool(sub.reporting_settled)
		and bool(sub.exchange_settled)
	)


def can_resubmit(sub) -> tuple[bool, str]:
	if sub.docstatus != 1:
		return False, "Only a document Flick accepted can be resubmitted; drafts are retried instead."
	if numbers_released(sub):
		return True, ""
	if unsettled_failure(sub):
		return False, "Flick is still retrying this document. Reissuing now would file it twice."
	if sub.reporting_status in REPORTING_DONE or sub.exchange_status in EXCHANGE_DONE:
		return (
			False,
			"Part of this document succeeded, so Flick holds the number. Correct it with a credit note.",
		)
	return False, "This document has not finished yet."


def unsettled_failure(sub) -> bool:
	return (sub.reporting_status == TRACK_FAILED and not sub.reporting_settled) or (
		sub.exchange_status == TRACK_FAILED and not sub.exchange_settled
	)


def can_cancel_invoice(sub) -> tuple[bool, str]:
	"""Whether the ERPNext invoice behind a submission may be cancelled."""
	if sub is None or sub.status == CANCELLED:
		return True, ""
	if sub.docstatus == 0:
		if sub.status in (QUEUED, REJECTED) or (sub.status == ERROR and not sub.attempt_count):
			return True, ""
		return False, (
			"The last attempt to send this invoice may have reached Flick. "
			"Use Check on Flick on the E-Invoice Submission before cancelling."
		)
	if numbers_released(sub):
		return True, ""
	return False, (
		"This invoice is filed with the FTA or on its way. "
		"Cancelling it in ERPNext would not withdraw it; issue a credit note instead."
	)


def next_action(sub, origins: set[str] | None = None) -> str:
	"""One plain sentence telling the finance team what to do now."""
	status = sub.status
	if status == QUEUED:
		return "Waiting to be sent to Flick."
	if status == REJECTED:
		return "Flick refused the document and nothing was filed. Fix the problems listed, then Retry."
	if status == ERROR:
		return "Sending did not complete. It is retried automatically; if it keeps failing, check the connection."
	if status == CANCELLED:
		return "The invoice was cancelled before it was filed."
	if status == COMPLETED:
		return "Reported to the FTA and delivered to the buyer."
	if status == PROCESSING:
		return "Accepted by Flick. Reporting and delivery are still running."

	if unsettled_failure(sub):
		return "Flick is still retrying. Do not reissue: that would file the same sale twice."
	if numbers_released(sub):
		text = "Neither reporting nor delivery succeeded, so Flick released the number. Fix the cause below, then Resubmit."
		if origins and origins & UNCERTAIN_ORIGINS:
			text += " Flick marks this failure as uncertain: confirm with Flick support before resubmitting."
		return text
	if sub.reporting_status in REPORTING_DONE and sub.exchange_status == TRACK_FAILED:
		return "Reported to the FTA but not delivered to the buyer. Do not reissue; sort out delivery with Flick, and correct with a credit note if needed."
	if sub.exchange_status in EXCHANGE_DONE and sub.reporting_status == TRACK_FAILED:
		return "Delivered to the buyer but FTA reporting failed. This needs review with Flick support. Do not reissue."
	return "One track failed for good and the other is still running. Wait for it to finish."


def parse_time(value: str | None) -> datetime | None:
	"""Flick timestamps are RFC 3339 in UTC, sometimes with nanoseconds."""
	if not value:
		return None
	text = value.strip().replace("Z", "+00:00")
	main, _, rest = text.partition(".")
	if rest:
		digits = "".join(ch for ch in rest if ch.isdigit())
		offset = rest[len(digits) :]
		text = f"{main}.{digits[:6].ljust(6, '0')}{offset}"
	parsed = datetime.fromisoformat(text)
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)

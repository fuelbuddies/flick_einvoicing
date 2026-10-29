from types import SimpleNamespace
from unittest import TestCase

from flick_einvoicing.einvoice import status as st


def sub(**values):
	defaults = {
		"docstatus": 1,
		"status": st.PROCESSING,
		"reporting_status": "processing",
		"exchange_status": "processing",
		"reporting_settled": 0,
		"exchange_settled": 0,
		"attempt_count": 1,
	}
	defaults.update(values)
	return SimpleNamespace(**defaults)


class TestTracks(TestCase):
	def test_a_finished_track_never_moves_back(self):
		# Seen on pre-production: a reporting event, 6 ms after an exchange event said
		# "delivered", still carried exchange "processing".
		self.assertEqual(st.merge_track("delivered", "processing", st.EXCHANGE_DONE), "delivered")
		self.assertEqual(st.merge_track("reported", "failed", st.REPORTING_DONE), "reported")

	def test_a_failed_track_can_still_move_on(self):
		self.assertEqual(st.merge_track("failed", "processing", st.EXCHANGE_DONE), "processing")
		self.assertEqual(st.merge_track("failed", "delivered", st.EXCHANGE_DONE), "delivered")

	def test_missing_incoming_keeps_current(self):
		self.assertEqual(st.merge_track("processing", None, st.EXCHANGE_DONE), "processing")

	def test_combined(self):
		self.assertEqual(st.combined("reported", "delivered"), st.COMPLETED)
		self.assertEqual(st.combined("not_required", "delivered"), st.COMPLETED)
		self.assertEqual(st.combined("reported", "processing"), st.PROCESSING)
		self.assertEqual(st.combined("processing", "failed"), st.FAILED)
		self.assertEqual(st.combined(None, None), st.PROCESSING)


class TestSettledRule(TestCase):
	"""Reissuing on an unsettled failure files a second document for the same sale under the
	client's TRN, the one defect a tax authority would see (brief §5)."""

	def test_unsettled_failure_blocks_every_way_of_reissuing(self):
		s = sub(status=st.FAILED, exchange_status="failed", exchange_settled=0)
		allowed, reason = st.can_resubmit(s)
		self.assertFalse(allowed)
		self.assertIn("retrying", reason)
		self.assertFalse(st.can_cancel_invoice(s)[0])
		self.assertIn("Do not reissue", st.next_action(s))

	def test_unsettled_reporting_failure_also_blocks(self):
		s = sub(
			status=st.FAILED,
			reporting_status="failed",
			reporting_settled=0,
			exchange_status="delivered",
			exchange_settled=1,
		)
		self.assertFalse(st.can_resubmit(s)[0])
		self.assertFalse(st.can_cancel_invoice(s)[0])

	def test_both_tracks_settled_failures_release_the_number(self):
		s = sub(
			status=st.FAILED,
			reporting_status="failed",
			reporting_settled=1,
			exchange_status="failed",
			exchange_settled=1,
		)
		self.assertTrue(st.numbers_released(s))
		self.assertTrue(st.can_resubmit(s)[0])
		self.assertTrue(st.can_cancel_invoice(s)[0])
		self.assertIn("released the number", st.next_action(s))

	def test_uncertain_origin_adds_flicks_caution(self):
		s = sub(
			status=st.FAILED,
			reporting_status="failed",
			reporting_settled=1,
			exchange_status="failed",
			exchange_settled=1,
		)
		self.assertIn("support", st.next_action(s, origins={"region"}))
		self.assertNotIn("support", st.next_action(s, origins={"authority"}))

	def test_reported_but_undelivered_needs_a_credit_note(self):
		s = sub(
			status=st.FAILED,
			reporting_status="reported",
			reporting_settled=1,
			exchange_status="failed",
			exchange_settled=1,
		)
		allowed, reason = st.can_resubmit(s)
		self.assertFalse(allowed)
		self.assertIn("credit note", reason)
		self.assertFalse(st.can_cancel_invoice(s)[0])

	def test_drafts_are_never_resubmitted(self):
		self.assertFalse(st.can_resubmit(sub(docstatus=0, status=st.REJECTED))[0])


class TestCancelRule(TestCase):
	def test_draft_that_never_reached_flick_may_be_cancelled(self):
		self.assertTrue(st.can_cancel_invoice(sub(docstatus=0, status=st.QUEUED, attempt_count=0))[0])
		self.assertTrue(st.can_cancel_invoice(sub(docstatus=0, status=st.REJECTED))[0])
		self.assertTrue(st.can_cancel_invoice(sub(docstatus=0, status=st.ERROR, attempt_count=0))[0])

	def test_an_unanswered_attempt_blocks_cancelling(self):
		allowed, reason = st.can_cancel_invoice(sub(docstatus=0, status=st.ERROR, attempt_count=1))
		self.assertFalse(allowed)
		self.assertIn("Check on Flick", reason)

	def test_accepted_documents_block_cancelling(self):
		self.assertFalse(st.can_cancel_invoice(sub())[0])
		self.assertFalse(
			st.can_cancel_invoice(
				sub(status=st.COMPLETED, reporting_status="reported", exchange_status="delivered")
			)[0]
		)

	def test_no_submission_means_nothing_to_protect(self):
		self.assertTrue(st.can_cancel_invoice(None)[0])


class TestParseTime(TestCase):
	def test_nanoseconds_and_z(self):
		parsed = st.parse_time("2026-10-28T17:32:55.166795363Z")
		self.assertEqual(parsed.isoformat(), "2026-10-28T17:32:55.166795+00:00")

	def test_milliseconds(self):
		self.assertEqual(st.parse_time("2026-10-28T17:32:58.403Z").microsecond, 403000)

	def test_empty(self):
		self.assertIsNone(st.parse_time(None))


class TestTrackStage(TestCase):
	def test_stages_for_the_dashboard(self):
		self.assertEqual(st.track_stage("reported", 1), "done")
		self.assertEqual(st.track_stage("received", 1), "done")
		self.assertEqual(st.track_stage("not_required", 1), "not_required")
		self.assertEqual(st.track_stage("processing", 0), "in_progress")
		self.assertEqual(st.track_stage(None, 0), "in_progress")
		self.assertEqual(st.track_stage("failed", 0), "retrying", "Flick has not settled it yet")
		self.assertEqual(st.track_stage("failed", 1), "failed")

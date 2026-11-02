from unittest import TestCase

from flick_einvoicing.support import code_only, fingerprint_of, is_ours, redact

WITH_VARIABLES = """Traceback with variables (most recent call last):
  File "apps/flick_einvoicing/flick_einvoicing/jobs.py", line 64, in send_queued
    send(row)
      row = {'customer': 'Acme Trading', 'grand_total': 5000}
      api_key = 'abc'
  File "apps/frappe/frappe/client.py", line 3, in get
    return x
      note = 'see apps/flick_einvoicing/readme'
KeyError: 'missing'"""

OTHER_APP = """Traceback with variables (most recent call last):
  File "apps/erpnext/erpnext/x.py", line 1, in y
    z()
      path = 'File "apps/flick_einvoicing/a.py", line 1, in b'
ValueError: boom"""


class TestWhatLeaves(TestCase):
	def test_variables_are_dropped(self):
		clean = code_only(WITH_VARIABLES)
		self.assertNotIn("Acme", clean)
		self.assertNotIn("api_key", clean)
		self.assertIn("send(row)", clean)
		self.assertTrue(clean.endswith("KeyError: 'missing'"))

	def test_only_our_frames_count(self):
		self.assertTrue(is_ours(code_only(WITH_VARIABLES)))
		self.assertFalse(is_ours(code_only(OTHER_APP)))

	def test_fingerprint_ignores_line_numbers(self):
		a = code_only(WITH_VARIABLES)
		self.assertEqual(fingerprint_of(a), fingerprint_of(a.replace("line 64", "line 99")))

	def test_redact(self):
		self.assertNotIn("hunter2", redact("password=hunter2"))

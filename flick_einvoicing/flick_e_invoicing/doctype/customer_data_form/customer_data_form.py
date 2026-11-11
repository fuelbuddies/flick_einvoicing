# Copyright (c) 2026, Techountant and contributors
# For license information, please see license.txt

import re

from frappe.model.document import Document


class CustomerDataForm(Document):
	def validate(self):
		"""Give each question a stable key. Keys already set are kept, so rewording a question
		does not orphan answers stored under it."""
		used = set()
		for row in self.questions:
			key = row.fieldname or re.sub(r"[^a-z0-9]+", "_", (row.label or "").lower()).strip("_")[:40] or "question"
			base, n = key, 2
			while key in used:
				key, n = f"{base}_{n}", n + 1
			row.fieldname = key
			used.add(key)

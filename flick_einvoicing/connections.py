"""The E-Invoicing group in the Connections tab of invoices."""


def _add(data, doctype, extra=None):
	data = data or {}
	data.setdefault("non_standard_fieldnames", {})["E Invoice Submission"] = "invoice"
	# the submission links back with a Dynamic Link: invoice + invoice_doctype
	data.setdefault("dynamic_links", {})["invoice"] = [doctype, "invoice_doctype"]
	items = ["E Invoice Submission", *(extra or [])]
	data.setdefault("transactions", []).append({"label": "E-Invoicing", "items": items})
	return data


def sales_invoice(data):
	return _add(data, "Sales Invoice")


def pos_invoice(data):
	return _add(data, "POS Invoice")


def purchase_invoice(data):
	data = _add(data, "Purchase Invoice", ["E Invoice Received"])
	data["non_standard_fieldnames"]["E Invoice Received"] = "purchase_invoice"
	return data

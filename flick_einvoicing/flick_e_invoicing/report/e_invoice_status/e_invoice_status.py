# Copyright (c) 2026, Techountant and contributors
# For license information, please see license.txt

import frappe
from frappe import _

NEEDS_ATTENTION = ("Rejected", "Error", "Failed")
COLORS = {
	"Completed": "Green",
	"Processing": "Orange",
	"Queued": "Blue",
	"Failed": "Red",
	"Rejected": "Red",
	"Error": "Red",
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	data = get_data(filters)
	return get_columns(), data, None, get_chart(data), get_summary(data)


def get_columns():
	return [
		{
			"label": _("Submission"),
			"fieldname": "name",
			"fieldtype": "Link",
			"options": "E Invoice Submission",
			"width": 150,
		},
		{"label": _("Invoice Type"), "fieldname": "invoice_doctype", "fieldtype": "Data", "width": 110},
		{
			"label": _("Invoice"),
			"fieldname": "invoice",
			"fieldtype": "Dynamic Link",
			"options": "invoice_doctype",
			"width": 160,
		},
		{"label": _("Date"), "fieldname": "issue_date", "fieldtype": "Date", "width": 100},
		{
			"label": _("Customer"),
			"fieldname": "customer",
			"fieldtype": "Link",
			"options": "Customer",
			"width": 180,
		},
		{"label": _("Type"), "fieldname": "document_type", "fieldtype": "Data", "width": 60},
		{
			"label": _("Grand Total"),
			"fieldname": "grand_total",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 120,
		},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Reporting"), "fieldname": "reporting_status", "fieldtype": "Data", "width": 100},
		{"label": _("Delivery"), "fieldname": "exchange_status", "fieldtype": "Data", "width": 100},
		{"label": _("Attempts"), "fieldname": "attempt_count", "fieldtype": "Int", "width": 80},
		{"label": _("Next Action"), "fieldname": "next_action", "fieldtype": "Data", "width": 360},
		{
			"label": _("Currency"),
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
		},
	]


def get_data(filters):
	conditions = {"docstatus": ("<", 2)}
	if filters.company:
		conditions["company"] = filters.company
	if filters.from_date and filters.to_date:
		conditions["issue_date"] = ("between", (filters.from_date, filters.to_date))
	if filters.status:
		conditions["status"] = filters.status
	if filters.needs_attention:
		conditions["status"] = ("in", NEEDS_ATTENTION)
	return frappe.get_all(
		"E Invoice Submission",
		filters=conditions,
		fields=[
			"name",
			"invoice_doctype",
			"invoice",
			"issue_date",
			"customer",
			"document_type",
			"grand_total",
			"currency",
			"status",
			"reporting_status",
			"exchange_status",
			"attempt_count",
			"next_action",
		],
		order_by="issue_date desc, creation desc",
	)


def _counts(data):
	counts = {}
	for row in data:
		counts[row.status] = counts.get(row.status, 0) + 1
	return counts


def get_chart(data):
	counts = _counts(data)
	if not counts:
		return None
	labels = list(counts)
	return {
		"data": {
			"labels": [_(label) for label in labels],
			"datasets": [{"name": _("Invoices"), "values": [counts[l] for l in labels]}],
		},
		"type": "bar",
		"colors": ["#5e64ff"],
	}


def get_summary(data):
	counts = _counts(data)
	attention = sum(counts.get(status, 0) for status in NEEDS_ATTENTION)
	return [
		{
			"value": counts.get("Completed", 0),
			"label": _("Completed"),
			"datatype": "Int",
			"indicator": "Green",
		},
		{
			"value": counts.get("Processing", 0) + counts.get("Queued", 0),
			"label": _("In Progress"),
			"datatype": "Int",
			"indicator": "Orange",
		},
		{"value": attention, "label": _("Need Attention"), "datatype": "Int", "indicator": "Red"},
	]

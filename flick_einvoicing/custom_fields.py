"""The only fields this app adds to standard doctypes. Everything else lives in E Invoice Submission."""

from flick_einvoicing.einvoice.codes import CREDIT_NOTE_REASONS, options

SUBMISSION_STATUSES = "\nQueued\nRejected\nError\nCancelled\nProcessing\nCompleted\nFailed"


def _invoice_fields(container: dict) -> list[dict]:
	return [
		container,
		{
			"fieldname": "einvoice_status",
			"label": "E-Invoice Status",
			"fieldtype": "Select",
			"options": SUBMISSION_STATUSES,
			"insert_after": container["fieldname"],
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "einvoice_submission",
			"label": "E-Invoice Submission",
			"fieldtype": "Link",
			"options": "E Invoice Submission",
			"insert_after": "einvoice_status",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "einvoice_column_break",
			"fieldtype": "Column Break",
			"insert_after": "einvoice_submission",
		},
		{
			"fieldname": "einvoice_credit_note_reason",
			"label": "Credit Note Reason",
			"fieldtype": "Select",
			"options": options(CREDIT_NOTE_REASONS),
			"insert_after": "einvoice_column_break",
			"depends_on": "eval:doc.is_return",
			"no_copy": 1,
			"print_hide": 1,
			"description": "Required on returns when UAE e-invoicing is enabled for the company.",
		},
	]


def get_custom_fields() -> dict[str, list[dict]]:
	return {
		"Sales Invoice": _invoice_fields(
			{
				"fieldname": "einvoice_tab",
				"label": "E-Invoicing",
				"fieldtype": "Tab Break",
				"insert_after": "remarks",
			}
		),
		# POS Invoice has no tabs; a Tab Break would re-flow its whole form, so use a section.
		"POS Invoice": _invoice_fields(
			{
				"fieldname": "einvoice_section",
				"label": "E-Invoicing",
				"fieldtype": "Section Break",
				"insert_after": "remarks",
				"collapsible": 1,
			}
		),
		"Customer": [
			{
				"fieldname": "einvoice_peppol_id",
				"label": "Peppol ID",
				"fieldtype": "Data",
				"insert_after": "tax_id",
				"description": "UAE e-invoicing address: 0235: followed by the 10-digit TIN, e.g. 0235:1020260015",
			}
		],
	}

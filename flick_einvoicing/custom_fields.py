"""The fields this app adds to standard doctypes. Everything else lives in its own doctypes.

Invoice fields sit in one E-Invoicing tab (a section on POS Invoice, which has no tabs), and
most only show once a case needs them.
"""

from flick_einvoicing.einvoice import codes
from flick_einvoicing.einvoice.codes import CREDIT_NOTE_REASONS, options

SUBMISSION_STATUSES = "\nQueued\nRejected\nError\nCancelled\nProcessing\nCompleted\nFailed"
ROUTING_OPTIONS = "\n".join((codes.ROUTING_PEPPOL, codes.ROUTING_EXPORT, codes.ROUTING_NOT_SUBJECT))
PERIOD_SHOWN = "eval:doc.einvoice_is_summary || doc.einvoice_is_continuous"


def _chain(container: dict, fields: list[dict]) -> list[dict]:
	"""Give each field the one before it as insert_after."""
	previous = container["fieldname"]
	for field in fields:
		field.setdefault("insert_after", previous)
		previous = field["fieldname"]
	return [container, *fields]


def _invoice_fields(container: dict, received_link: bool = False) -> list[dict]:
	status = [
		{
			"fieldname": "einvoice_status",
			"label": "E-Invoice Status",
			"fieldtype": "Select",
			"options": SUBMISSION_STATUSES,
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"print_hide": 1,
			"in_standard_filter": 1,
			# a column where most invoices are e-invoiced; purchase invoices only are when self-billed
			"in_list_view": 0 if received_link else 1,
		},
		{
			"fieldname": "einvoice_submission",
			"label": "E-Invoice Submission",
			"fieldtype": "Link",
			"options": "E Invoice Submission",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
	]
	if received_link:
		status.append(
			{
				"fieldname": "einvoice_received",
				"label": "Received E-Invoice",
				"fieldtype": "Link",
				"options": "E Invoice Received",
				"read_only": 1,
				"no_copy": 1,
				"print_hide": 1,
			}
		)
	status += [
		{"fieldname": "einvoice_column_break", "fieldtype": "Column Break"},
		{
			"fieldname": "einvoice_credit_note_reason",
			"label": "Credit Note Reason",
			"fieldtype": "Select",
			"options": options(CREDIT_NOTE_REASONS),
			"depends_on": "eval:doc.is_return",
			"no_copy": 1,
			"print_hide": 1,
			"description": "Required on returns when UAE e-invoicing is enabled for the company.",
		},
	]

	transaction = [
		{
			"fieldname": "einvoice_transaction_section",
			"label": "Transaction Type",
			"fieldtype": "Section Break",
			"collapsible": 0,  # open by default: Export and the other flags are seen at a glance
			"description": "Only for the special cases below; an ordinary invoice needs none of them.",
		},
		_check("einvoice_is_ftz", "Free Trade Zone Supply"),
		_data(
			"einvoice_fz_beneficiary_id",
			"Free Zone Beneficiary TRN",
			depends_on="eval:doc.einvoice_is_ftz",
			mandatory_depends_on="eval:doc.einvoice_is_ftz",
		),
		_check(
			"einvoice_is_deemed",
			"Deemed Supply",
			description="A supply without consideration. Reported to the FTA only, not delivered to a buyer.",
		),
		_check("einvoice_is_dab", "Disclosed Agent Billing"),
		_data(
			"einvoice_principal_id",
			"Principal TRN",
			depends_on="eval:doc.einvoice_is_dab",
			mandatory_depends_on="eval:doc.einvoice_is_dab",
			description="The principal you invoice for; must differ from your own TRN.",
		),
		{"fieldname": "einvoice_transaction_column", "fieldtype": "Column Break"},
		_check("einvoice_is_ecommerce", "E-Commerce Supply", description="Needs a shipping address."),
		_check("einvoice_is_export", "Export", description="Needs a shipping address outside the UAE."),
		_check(
			"einvoice_is_summary",
			"Summary Invoice",
			description="Several supplies to this buyer in one month.",
		),
		_check("einvoice_is_continuous", "Continuous Supply"),
		{
			"fieldname": "einvoice_period_start",
			"label": "Period Start",
			"fieldtype": "Date",
			"depends_on": PERIOD_SHOWN,
			"no_copy": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "einvoice_period_end",
			"label": "Period End",
			"fieldtype": "Date",
			"depends_on": PERIOD_SHOWN,
			"no_copy": 1,
			"print_hide": 1,
		},
	]

	references = [
		{
			"fieldname": "einvoice_references_section",
			"label": "References",
			"fieldtype": "Section Break",
			"collapsible": 1,
		},
		{
			"fieldname": "einvoice_preceding_invoices",
			"label": "Preceding Invoices",
			"fieldtype": "Table",
			"options": "E Invoice Preceding Invoice",
			"no_copy": 1,
			"description": "Advance invoices this invoice settles. On a credit or debit note, the original "
			"when ERPNext doesn't hold it, such as an invoice from your previous system: give its date too.",
		},
		_data("einvoice_buyer_reference", "Buyer Reference"),
		_data(
			"einvoice_buyer_accounting_cost",
			"Buyer Accounting Reference",
			description="The buyer's cost centre code.",
		),
		{
			"fieldname": "einvoice_tax_point_date",
			"label": "Tax Point Date",
			"fieldtype": "Date",
			"no_copy": 1,
			"print_hide": 1,
			"description": "Only when VAT became due before the posting date.",
		},
		{"fieldname": "einvoice_references_column", "fieldtype": "Column Break"},
		_data("einvoice_contract_reference", "Contract Reference"),
		_data("einvoice_contract_value", "Contract Value", description="As text, for example AED 250000."),
		_data("einvoice_tender_reference", "Tender or Lot"),
		_data("einvoice_receipt_reference", "Buyer's Goods Receipt"),
		_data("einvoice_customs_reference", "Customs Declaration Number"),
	]

	delivery = [
		{
			"fieldname": "einvoice_delivery_section",
			"label": "Delivery",
			"fieldtype": "Section Break",
			"collapsible": 1,
			"description": "The delivery address is the invoice's Shipping Address, and the Incoterm is the one set under Taxes and Charges.",
		},
		{
			"fieldname": "einvoice_delivery_date",
			"label": "Actual Delivery Date",
			"fieldtype": "Date",
			"no_copy": 1,
			"print_hide": 1,
			"description": "Left empty, the latest delivery note or line delivery date is used.",
		},
		_data(
			"einvoice_delivery_party",
			"Received By",
			description="When someone other than the buyer receives the goods.",
		),
		{"fieldname": "einvoice_delivery_column", "fieldtype": "Column Break"},
		_data("einvoice_delivery_location_id", "Delivery Location ID"),
		_data("einvoice_delivery_location_scheme", "Location ID Scheme"),
	]

	documents = [
		{
			"fieldname": "einvoice_documents_section",
			"label": "Supporting Documents",
			"fieldtype": "Section Break",
			"collapsible": 1,
		},
		{
			"fieldname": "einvoice_documents",
			"label": "Supporting Documents",
			"fieldtype": "Table",
			"options": "E Invoice Supporting Document",
			"no_copy": 1,
			"description": "Sent with the e-invoice: an attached file (up to 20 MB), a link, or a reference the buyer already holds.",
		},
	]
	return _chain(container, status + transaction + references + delivery + documents)


def _check(fieldname, label, **extra):
	return {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": "Check",
		"no_copy": 1,
		"print_hide": 1,
		**extra,
	}


def _data(fieldname, label, **extra):
	return {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": "Data",
		"no_copy": 1,
		"print_hide": 1,
		**extra,
	}


def _tab(fieldname="einvoice_tab"):
	return {
		"fieldname": fieldname,
		"label": "E-Invoicing",
		"fieldtype": "Tab Break",
		"insert_after": "remarks",
	}


def _party_identity(after: str, prefix_fields: list[dict]) -> list[dict]:
	fields = [
		*prefix_fields,
		{
			"fieldname": "einvoice_trade_licence",
			"label": "Trade Licence Number",
			"fieldtype": "Data",
			"description": "Optional on the e-invoice; sent with the licensing authority below.",
		},
		{
			"fieldname": "einvoice_trade_licence_authority",
			"label": "Licensing Authority",
			"fieldtype": "Data",
			"depends_on": "eval:doc.einvoice_trade_licence",
			"mandatory_depends_on": "eval:doc.einvoice_trade_licence",
		},
	]
	previous = after
	for field in fields:
		field.setdefault("insert_after", previous)
		previous = field["fieldname"]
	return fields


def get_custom_fields() -> dict[str, list[dict]]:
	return {
		"Sales Invoice": _invoice_fields(_tab()),
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
		# Self-billing: you issue the supplier's invoice, and invoices received over Peppol
		# become draft purchase invoices linked back to what arrived.
		"Purchase Invoice": _invoice_fields(_tab(), received_link=True),
		"Customer": [
			*_party_identity(
				"tax_id",
				[
					{
						"fieldname": "einvoice_legal_name",
						"label": "Legal Name",
						"fieldtype": "Data",
						"description": "Registered legal name, sent as the buyer name on the e-invoice. Left empty, the customer name is sent.",
					},
					{
						"fieldname": "einvoice_routing",
						"label": "E-Invoice Routing",
						"fieldtype": "Select",
						"options": ROUTING_OPTIONS,
						"default": codes.ROUTING_PEPPOL,
						"description": "Buyers abroad who are not on Peppol, and buyers outside UAE e-invoicing, are reported to the FTA without delivery.",
					},
					{
						"fieldname": "einvoice_peppol_id",
						"label": "Peppol ID",
						"fieldtype": "Data",
						"depends_on": f"eval:(doc.einvoice_routing || '{codes.ROUTING_PEPPOL}') == '{codes.ROUTING_PEPPOL}'",
						"description": "UAE e-invoicing address: 0235: followed by the 10-digit TIN, e.g. 0235:1234567890",
					},
				],
			),
			{
				"fieldname": "einvoice_trade_name",
				"label": "Trading Name",
				"fieldtype": "Data",
				"insert_after": "einvoice_trade_licence_authority",
				"description": "Sent on the e-invoice when the buyer trades under a name other than its legal name.",
			},
			{
				"fieldname": "einvoice_party_identifier",
				"label": "Buyer Identifier",
				"fieldtype": "Data",
				"insert_after": "einvoice_trade_name",
				"description": "For a buyer with no TRN and no Peppol ID of its own (not VAT registered, or outside UAE e-invoicing). Left empty, the trade licence number is sent.",
			},
			{
				"fieldname": "einvoice_mandate_id",
				"label": "Direct Debit Mandate",
				"fieldtype": "Data",
				"insert_after": "einvoice_party_identifier",
			},
			{
				"fieldname": "einvoice_mandate_account",
				"label": "Debited Account (IBAN)",
				"fieldtype": "Data",
				"insert_after": "einvoice_mandate_id",
				"depends_on": "eval:doc.einvoice_mandate_id",
			},
			{
				"fieldname": "einvoice_free_zone",
				"label": "Free Zone Company",
				"fieldtype": "Check",
				"insert_after": "einvoice_mandate_account",
				"description": "New sales invoices to this customer start with Free Trade Zone Supply ticked and its TRN as the beneficiary. Untick on the invoice when the supply is not a free zone one.",
			},
			{
				"fieldname": "einvoice_readiness_section",
				"label": "E-Invoicing Readiness",
				"fieldtype": "Section Break",
				"insert_after": "einvoice_free_zone",
				"collapsible": 1,
			},
			{
				"fieldname": "einvoice_phase",
				"label": "E-Invoicing Phase",
				"fieldtype": "Select",
				"options": "\n" + codes.EINVOICE_PHASES,
				"insert_after": "einvoice_readiness_section",
			},
			{
				"fieldname": "einvoice_go_live_date",
				"label": "Planned Go-Live Date",
				"fieldtype": "Date",
				"insert_after": "einvoice_phase",
				"description": "If the customer goes live before its mandatory date.",
			},
			{
				"fieldname": "einvoice_readiness_column",
				"fieldtype": "Column Break",
				"insert_after": "einvoice_go_live_date",
			},
			{
				"fieldname": "einvoice_contact_email",
				"label": "E-Invoicing Contact Email",
				"fieldtype": "Data",
				"options": "Email",
				"insert_after": "einvoice_readiness_column",
			},
		],
		"Supplier": _party_identity(
			"tax_id",
			[
				{
					"fieldname": "einvoice_peppol_id",
					"label": "Peppol ID",
					"fieldtype": "Data",
					"description": "0235: followed by the 10-digit TIN. Matches invoices received over Peppol to this supplier.",
				},
				{
					"fieldname": "einvoice_self_billing",
					"label": "Self-Billing Agreement",
					"fieldtype": "Check",
					"description": "You issue this supplier's invoices: its purchase invoices are e-invoiced as self-billed (389, 261 for returns).",
				},
			],
		),
		"Purchase Invoice Item": [
			{
				"fieldname": "einvoice_supplier_item_code",
				"label": "Supplier's Item Code",
				"fieldtype": "Data",
				"insert_after": "item_name",
				"read_only": 1,
				"depends_on": "eval:doc.einvoice_supplier_item_code",
				"description": "From the supplier's e-invoice. Submitting remembers it on the item chosen here.",
			},
			{
				"fieldname": "einvoice_line_no",
				"label": "E-Invoice Line",
				"fieldtype": "Int",
				"insert_after": "einvoice_supplier_item_code",
				"read_only": 1,
				"hidden": 1,
				"no_copy": 1,
				"print_hide": 1,
				"description": "The line of the supplier's e-invoice this row bills, counted from 1.",
			},
		],
		"Item": [
			{
				"fieldname": "einvoice_sac_code",
				"label": "Service Accounting Code (SAC)",
				"fieldtype": "Data",
				"insert_after": "customs_tariff_number",
				"description": "For services on UAE e-invoices. Goods use the HS Code above.",
			}
		],
	}

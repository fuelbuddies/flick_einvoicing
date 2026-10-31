"""The E Invoicing Guide: what the app does to invoices and to the books, rendered with this site's
own settings, so the figures in it are the ones its readers will see."""

import frappe
from frappe.utils import cint, formatdate

from flick_einvoicing.einvoice import codes
from flick_einvoicing.einvoice.inbound import placeholder_item

TEMPLATE = "flick_einvoicing/templates/einvoicing_guide.html"
# None of these is a credential: the guide may show them to anyone who reads invoices.
COMPANY_FIELDS = [
	"name",
	"enabled",
	"environment",
	"submission_mode",
	"validate_before_submit",
	"start_date",
	"skip_b2c",
	"discount_style",
	"negative_discount_reason_code",
	"receive_invoices",
	"auto_create_purchase_invoice",
	"purchase_item",
	"purchase_taxes_template",
]


def render() -> str:
	return frappe.render_template(TEMPLATE, context())


def context() -> dict:
	line = frappe.new_doc("Purchase Invoice Item")
	tax = frappe.new_doc("Purchase Taxes and Charges")
	invoice = frappe.new_doc("Purchase Invoice")
	companies = frappe.get_all("E Invoicing Settings", fields=COMPANY_FIELDS, order_by="name")
	for company in companies:
		company.start_date = formatdate(company.start_date) if company.start_date else None
	return {
		"site": frappe._dict(
			rate_decimals=line.precision("rate"),
			amount_decimals=invoice.precision("grand_total"),
			vat_decimals=tax.precision("tax_amount"),
			rounding_method=frappe.get_system_settings("rounding_method") or "Banker's Rounding (legacy)",
			row_wise_tax=cint(frappe.db.get_single_value("Accounts Settings", "round_row_wise_tax")),
			same_rate=cint(frappe.db.get_single_value("Buying Settings", "maintain_same_rate")),
			landed_cost=cint(
				frappe.db.get_single_value("Buying Settings", "set_landed_cost_based_on_purchase_invoice_rate")
			),
			placeholder=placeholder_item(),
		),
		"companies": companies,
		"discount_reason_code": codes.DISCOUNT_REASON_CODE,
	}

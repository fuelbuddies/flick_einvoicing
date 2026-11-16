"""Customer-chosen fields carried on the e-invoice.

PINT-AE has no slot for arbitrary keys (Flick drops unknown ones in silence), so each mapping row
in E Invoicing Settings puts its value into a standard slot built for free information:

- Item Attribute: per line, a name/value pair (cac:AdditionalItemProperty)
- Note: per invoice, a "Label: value" line in cbc:Note
- Document Reference: per invoice, an AdditionalDocumentReference (value as its number)
- Buyer Reference / Accounting Cost / Project Reference: per invoice, the one value of that slot

The value is a template. `{field}` takes a field of the line (for line rows) or of the invoice;
full Jinja works too: `{{ doc.po_no }}-{{ item.item_code }}`, `{% if %}`, `frappe.db.get_value`.
"""

import re

import frappe
from frappe.utils import cstr

LINE, INVOICE = "Line", "Invoice"
ITEM_ATTRIBUTE = "Item Attribute"
NOTE = "Note"
DOCUMENT_REFERENCE = "Document Reference"
BUYER_REFERENCE = "Buyer Reference"
ACCOUNTING_COST = "Accounting Cost"
PROJECT_REFERENCE = "Project Reference"

LINE_TARGETS = (ITEM_ATTRIBUTE,)
SINGLE_TARGETS = (BUYER_REFERENCE, ACCOUNTING_COST, PROJECT_REFERENCE)
NOTE_LIMIT = 1000

INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")
ITEM_DOCTYPES = ("Sales Invoice Item", "Purchase Invoice Item")

# {field} but not {{ ... }} or {% ... %}
_SHORTHAND = re.compile(r"(?<![{%])\{\s*([A-Za-z_]\w*)\s*\}(?!\})")


def rows(settings) -> list:
	return [row for row in settings.get("custom_fields") or [] if row.enabled and cstr(row.template).strip()]


def to_jinja(template: str) -> str:
	return _SHORTHAND.sub(r"{{ \1 }}", cstr(template))


def shorthand_fields(template: str) -> list[str]:
	return _SHORTHAND.findall(cstr(template))


def render(row, inv, item=None) -> str:
	context = {**inv.as_dict(), **(item.as_dict() if item else {}), "doc": inv, "item": item}
	return cstr(frappe.render_template(to_jinja(row.template), context)).strip()


def apply(payload: dict, inv, lines: list[dict], settings, problem) -> None:
	"""Fill the payload's slots from the settings rows. `lines` pairs with inv.items."""
	mappings = rows(settings)
	if not mappings:
		return

	def value_of(row, item=None):
		try:
			return render(row, inv, item)
		except Exception as e:
			problem(f"Custom field row {row.idx} ({row.label}): its template failed: {e}")
			return ""

	note_lines = []
	for row in mappings:
		label = cstr(row.label).strip()
		if row.target == ITEM_ATTRIBUTE:
			for item, line in zip(inv.items, lines):
				value = value_of(row, item)
				if value:
					line["item_attributes"] = {**(line.get("item_attributes") or {}), label: value}
			continue

		value = value_of(row)
		if not value:
			continue
		if row.target == NOTE:
			note_lines.append(f"{label}: {value}" if label else value)
		elif row.target == DOCUMENT_REFERENCE:
			payload["additional_document_references"] = [
				*(payload.get("additional_document_references") or []),
				{"document_identifier": value, "document_description": label or None},
			]
		elif row.target == BUYER_REFERENCE:
			payload["buyer_reference"] = value
		elif row.target == ACCOUNTING_COST:
			payload["accounting_cost"] = value
		elif row.target == PROJECT_REFERENCE:
			payload["other_references"] = {**(payload.get("other_references") or {}), "project_reference": value}

	if note_lines:
		note = "\n".join([cstr(payload.get("note")).strip(), *note_lines]).strip()
		if len(note) > NOTE_LIMIT:
			problem(
				f"The invoice note with its custom fields runs to {len(note)} characters; "
				f"PINT-AE allows {NOTE_LIMIT}. Shorten the remarks or the custom field values."
			)
		payload["note"] = note


def validate_rows(settings) -> None:
	"""Called from E Invoicing Settings.validate: catch typos at save, not as silent drops."""
	from frappe import _
	from frappe.utils.jinja import validate_template

	seen = {}
	for row in settings.get("custom_fields") or []:
		row.label = cstr(row.label).strip()
		if row.level == LINE and row.target not in LINE_TARGETS:
			frappe.throw(_("Custom field row {0}: a line row can only be an Item Attribute.").format(row.idx))
		if row.level == INVOICE and row.target in LINE_TARGETS:
			frappe.throw(_("Custom field row {0}: Item Attribute needs level Line.").format(row.idx))
		if row.target in (ITEM_ATTRIBUTE, DOCUMENT_REFERENCE) and not row.label:
			frappe.throw(_("Custom field row {0}: {1} needs a label.").format(row.idx, row.target))
		if row.enabled and row.target in SINGLE_TARGETS:
			if row.target in seen:
				frappe.throw(
					_("Custom field rows {0} and {1} both fill {2}, which holds one value.").format(
						seen[row.target], row.idx, row.target
					)
				)
			seen[row.target] = row.idx

		validate_template(to_jinja(row.template))
		doctypes = INVOICE_DOCTYPES + (ITEM_DOCTYPES if row.level == LINE else ())
		for field in shorthand_fields(row.template):
			if not any(_has_field(dt, field) for dt in doctypes):
				frappe.throw(
					_("Custom field row {0}: no field {1} on {2}.").format(
						row.idx, frappe.bold(field), _(" or ").join(doctypes)
					)
				)


def _has_field(doctype: str, fieldname: str) -> bool:
	from frappe.model import default_fields

	return fieldname in default_fields or frappe.get_meta(doctype).has_field(fieldname)


@frappe.whitelist()
def preview(company: str, doctype: str, invoice: str) -> list[dict]:
	"""What each enabled row would send for one invoice, from the saved settings."""
	from flick_einvoicing.einvoice.scope import SETTINGS

	if doctype not in INVOICE_DOCTYPES:
		frappe.throw("Choose a Sales or Purchase Invoice.")
	settings = frappe.get_doc(SETTINGS, company)
	settings.check_permission("read")
	inv = frappe.get_doc(doctype, invoice)
	inv.check_permission("read")

	out = []
	for row in rows(settings):
		targets = [(item, f"Row {item.idx} ({item.item_code})") for item in inv.items] if row.level == LINE else [(None, "Invoice")]
		for item, where in targets:
			try:
				value = render(row, inv, item)
			except Exception as e:
				value = f"Error: {e}"
			out.append({"row": row.idx, "label": row.label, "target": row.target, "where": where, "value": value})
	return out

"""Customer data collection: a one-time link a customer opens to fill in its e-invoicing master
data. The questions live in the Customer Data Form single; each link is a Customer Data Request
with its own random key, closed once the customer submits."""

import re
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, get_url, getdate, now_datetime, validate_email_address

from flick_einvoicing.einvoice import codes

FORM = "Customer Data Form"
REQUEST = "Customer Data Request"
ROUTE = "customer-data-form"

PHASES = codes.EINVOICE_PHASES
EMIRATES = "AUH\nDXB\nSHJ\nUAQ\nFUJ\nAJM\nRAK"

# The questions a new site starts with: (question, type, mandatory, choices, help, customer field)
DEFAULT_QUESTIONS = [
	(
		"Full legal name of your company, as registered with the relevant authorities",
		"Text",
		1,
		None,
		None,
		"einvoice_legal_name",
	),
	("UAE e-invoicing implementation phase for your entity", "Select", 1, PHASES, None, "einvoice_phase"),
	("Planned go-live date, if before your mandatory implementation date", "Date", 0, None, None, "einvoice_go_live_date"),
	("E-invoicing TIN", "Text", 1, None, "Used as your Peppol ID for e-invoicing transactions.", "einvoice_peppol_id"),
	(
		"VAT Registration Number (TRN)",
		"Text",
		1,
		None,
		'For a VAT group, the VAT Group Registration Number. Not VAT registered: enter "NA".',
		"tax_id",
	),
	("Address Line 1 (street name), as it should appear on the e-invoice", "Text", 1, None, None, "address.address_line1"),
	("Address Line 2 (city name), as it should appear on the e-invoice", "Text", 1, None, None, "address.city"),
	(
		"Trade licence number of your legal entity",
		"Text",
		1,
		None,
		"Trade licence number preferred. Otherwise the Cabinet decision number, or an Emirates ID or passport number.",
		"einvoice_trade_licence",
	),
	("Authority that issued your trade licence", "Text", 1, None, None, "einvoice_trade_licence_authority"),
	("Emirate where your entity is registered", "Select", 1, EMIRATES, None, "address.emirate"),
	("Is your entity registered as a Free Zone company?", "Yes/No", 1, None, None, "einvoice_free_zone"),
	(
		"Contact email address",
		"Email",
		1,
		None,
		"We will use it only if we need to clarify anything you submitted.",
		"einvoice_contact_email",
	),
	(
		"Supporting documents (VAT certificate, trade licence)",
		"Attachment",
		0,
		None,
		"Optional. PDF or image, up to 5 MB.",
		None,
	),
]

MAX_FILE_BYTES = 5 * 1024 * 1024
FILE_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def ensure_default_form():
	"""Seed the form with the standard questions the first time; never touch it afterwards."""
	if not frappe.db.exists("DocType", FORM):
		return
	form = frappe.get_single(FORM)
	if form.questions:
		return
	for label, fieldtype, reqd, options, description, customer_field in DEFAULT_QUESTIONS:
		form.append(
			"questions",
			{
				"label": label,
				"fieldtype": fieldtype,
				"reqd": reqd,
				"enabled": 1,
				"options": options,
				"description": description,
				"customer_field": customer_field,
			},
		)
	form.title = form.title or "UAE E-Invoicing – Customer Master Data"
	form.intro = form.intro or (
		"Please provide the details below so we can configure your entity for UAE e-invoicing. "
		"This link works once."
	)
	form.flags.ignore_permissions = True
	form.save()


def active_questions(form=None) -> list:
	form = form or frappe.get_single(FORM)
	return [q for q in form.questions if q.enabled]


def form_url(key: str) -> str:
	return get_url(f"/{ROUTE}?key={key}")


@frappe.whitelist()
def create_request(customer: str) -> dict:
	"""A fresh link for the customer. Links still waiting are cancelled, so only the newest works."""
	frappe.has_permission("Customer", "read", customer, throw=True)
	frappe.has_permission(REQUEST, "create", throw=True)
	ensure_default_form()
	form = frappe.get_single(FORM)
	if not active_questions(form):
		frappe.throw(_("Add at least one enabled question to {0} first.").format(FORM))

	for name in frappe.get_all(REQUEST, {"customer": customer, "status": "Pending"}, pluck="name"):
		frappe.db.set_value(REQUEST, name, "status", "Cancelled")

	key = secrets.token_urlsafe(32)
	days = form.link_validity_days or 0
	doc = frappe.get_doc(
		{
			"doctype": REQUEST,
			"customer": customer,
			"key": key,
			"status": "Pending",
			"form_url": form_url(key),
			"expires_on": add_days(now_datetime(), days) if days > 0 else None,
		}
	).insert()
	return {"name": doc.name, "url": doc.form_url, "expires_on": doc.expires_on}


def get_open_request(key: str | None):
	"""The request behind a key, or a reason it cannot be filled in."""
	if not key or not isinstance(key, str) or len(key) > 100:
		return None, _("This link is not valid.")
	name = frappe.db.get_value(REQUEST, {"key": key}, "name")
	if not name:
		return None, _("This link is not valid.")
	doc = frappe.get_doc(REQUEST, name)
	if doc.status != "Pending":
		return None, _("This link has already been used or replaced. Please ask us for a new one.")
	if doc.expires_on and now_datetime() > doc.expires_on:
		return None, _("This link has expired. Please ask us for a new one.")
	return doc, None


def _clean(question, raw) -> str:
	if question.fieldtype == "Attachment":
		if not raw and question.reqd:
			frappe.throw(_("Please attach: {0}").format(question.label))
		return ""
	value = (raw if isinstance(raw, str) else "" if raw is None else str(raw)).strip()[:2000]
	label = question.label
	if not value:
		if question.reqd:
			frappe.throw(_("Please answer: {0}").format(label))
		return ""
	if question.fieldtype == "Email" and not validate_email_address(value):
		frappe.throw(_("{0}: please enter a valid email address.").format(label))
	elif question.fieldtype == "Date":
		try:
			value = str(getdate(value))
		except Exception:
			frappe.throw(_("{0}: please enter a valid date.").format(label))
	elif question.fieldtype == "Select":
		choices = [c.strip() for c in (question.options or "").split("\n") if c.strip()]
		if value not in choices:
			frappe.throw(_("{0}: please pick one of the choices.").format(label))
	elif question.fieldtype == "Yes/No" and value not in ("Yes", "No"):
		frappe.throw(_("{0}: please answer Yes or No.").format(label))
	return value


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def submit(key: str, answers: dict | str) -> str:
	"""The customer's answers. Accepted once per key."""
	doc, error = get_open_request(key)
	if error:
		frappe.throw(error)
	answers = frappe.parse_json(answers) or {}
	if not isinstance(answers, dict):
		frappe.throw(_("Invalid submission."))

	form = frappe.get_single(FORM)
	questions = active_questions(form)
	doc.responses = []
	files = {}
	for q in questions:
		value = _clean(q, answers.get(q.fieldname))
		if q.fieldtype == "Attachment" and answers.get(q.fieldname):
			files[q.fieldname] = _decode_file(q, answers[q.fieldname])
			value = files[q.fieldname][0]
		doc.append(
			"responses",
			{"label": q.label, "fieldname": q.fieldname, "value": value, "customer_field": q.customer_field},
		)
	doc.status = "Submitted"
	doc.submitted_on = now_datetime()
	doc.flags.ignore_permissions = True
	doc.save()

	for row in doc.responses:
		if row.fieldname in files:
			filename, content = files[row.fieldname]
			try:
				f = frappe.get_doc(
					{
						"doctype": "File",
						"file_name": filename,
						"content": content,
						"is_private": 1,
						"attached_to_doctype": REQUEST,
						"attached_to_name": doc.name,
					}
				).insert(ignore_permissions=True)
			except frappe.ValidationError:
				raise
			except Exception:
				# Frappe opens PDFs to check them; a damaged one raises a parser error.
				frappe.throw(_("{0}: the file could not be read.").format(row.label))
			row.db_set("attachment", f.file_url)

	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Customer",
			"reference_name": doc.customer,
			"content": _("Customer data form submitted: {0}").format(
				f'<a href="/app/customer-data-request/{doc.name}">{doc.name}</a>'
			),
		}
	).insert(ignore_permissions=True)
	return form.thank_you or _("Thank you. Your details have been received.")


def _decode_file(question, raw) -> tuple[str, bytes]:
	"""An uploaded file sent as {"name": ..., "data": <base64>}."""
	import base64
	import os

	if not isinstance(raw, dict) or not raw.get("data"):
		frappe.throw(_("{0}: the file could not be read.").format(question.label))
	name = os.path.basename(str(raw.get("name") or "document"))[:120]
	if os.path.splitext(name)[1].lower() not in FILE_TYPES:
		frappe.throw(_("{0}: only PDF or image files are accepted.").format(question.label))
	data = str(raw["data"]).split(",", 1)[-1]
	if len(data) > MAX_FILE_BYTES * 4 // 3 + 4:
		frappe.throw(_("{0}: the file is larger than 5 MB.").format(question.label))
	try:
		content = base64.b64decode(data, validate=True)
	except Exception:
		frappe.throw(_("{0}: the file could not be read.").format(question.label))
	return name, content


def _apply_address(customer, values: dict) -> bool:
	"""Put address answers on the customer's billing address, creating one if it has none."""
	from frappe.contacts.doctype.address.address import get_default_address

	name = get_default_address("Customer", customer.name)
	if name:
		address = frappe.get_doc("Address", name)
	else:
		address = frappe.new_doc("Address")
		address.update(
			{
				"address_title": customer.customer_name,
				"address_type": "Billing",
				"country": "United Arab Emirates",
				"is_primary_address": 1,
			}
		)
		address.append("links", {"link_doctype": "Customer", "link_name": customer.name})
	if "emirate" in values:
		names = {code: name for name, code in codes.EMIRATE_CODES.items()}
		values["emirate"] = names.get(values["emirate"], values["emirate"])
		if not address.meta.has_field("emirate"):
			values.pop("emirate")
	values = {k: v for k, v in values.items() if address.get(k) != v}
	if not values:
		return False
	address.update(values)
	address.save()
	return True


def _customer_value(fieldname: str, value: str) -> str | None:
	if fieldname == "tax_id" and value.strip().upper() in ("NA", "N/A"):
		return None
	if fieldname == "einvoice_free_zone":
		return 1 if value == "Yes" else 0 if value == "No" else None
	if fieldname == "einvoice_peppol_id":
		digits = re.sub(r"\D", "", value)
		if ":" not in value and len(digits) == 10:
			return f"0235:{digits}"
	return value


@frappe.whitelist()
def apply_to_customer(name: str) -> list[str]:
	"""Copy answers whose question names a customer field onto the customer."""
	doc = frappe.get_doc(REQUEST, name)
	doc.check_permission("write")
	if doc.status != "Submitted":
		frappe.throw(_("Only submitted responses can be applied."))
	customer = frappe.get_doc("Customer", doc.customer)
	customer.check_permission("write")

	changed = []
	address_values = {}
	for row in doc.responses:
		if row.customer_field and row.customer_field.startswith("address.") and row.value:
			address_values[row.customer_field.split(".", 1)[1]] = row.value
			continue
		if not row.customer_field or not row.value or not customer.meta.has_field(row.customer_field):
			continue
		value = _customer_value(row.customer_field, row.value)
		if value is not None and customer.get(row.customer_field) != value:
			customer.set(row.customer_field, value)
			changed.append(customer.meta.get_label(row.customer_field))
	if changed:
		customer.save()
	if address_values and _apply_address(customer, address_values):
		changed.append(_("Billing Address"))
	doc.db_set("status", "Applied")
	return changed

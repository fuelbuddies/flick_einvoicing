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
from flick_einvoicing.party_ids import normalize_peppol_id, normalize_trn

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
	if question.customer_field == "tax_id":
		value = normalize_trn(value) or value.strip().upper()
	elif question.customer_field == "einvoice_peppol_id":
		value = normalize_peppol_id(value).split(":", 1)[1]
	elif question.customer_field == "einvoice_go_live_date" and getdate(value) < getdate():
		frappe.throw(_("{0}: the date cannot be in the past.").format(label))
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
	if fieldname == "tax_id":
		return normalize_trn(value)
	if fieldname == "einvoice_free_zone":
		return 1 if value == "Yes" else 0 if value == "No" else None
	if fieldname == "einvoice_peppol_id":
		return normalize_peppol_id(value)
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


# Bulk email ------------------------------------------------------------------------------------

DEFAULT_SUBJECT = "UAE e-invoicing: please confirm your company details – {{ company }}"
DEFAULT_BODY = """<p>Dear {{ customer_name }},</p>
<p>UAE e-invoicing becomes mandatory from 2027. To keep issuing invoices to you without interruption,
we need to confirm the details that will appear on your e-invoices (legal name, TRN, e-invoicing TIN,
address and trade licence).</p>
<p>Please fill in this short form. It takes about five minutes:</p>
<p><a href="{{ link }}">{{ link }}</a></p>
<p>The link is personal to your company and works once.{% if expires_on %} It is valid until {{ expires_on }}.{% endif %}</p>
<p>Thank you,<br>{{ company }}</p>"""


def email_template() -> dict:
	form = frappe.get_single(FORM)
	return {"subject": form.get("email_subject") or DEFAULT_SUBJECT, "body": form.get("email_body") or DEFAULT_BODY}


def contact_emails(customers: list[str]) -> dict[str, tuple[str, str]]:
	"""Each customer's email to send to and where it came from: the E-Invoicing Contact Email,
	else the customer's primary contact (ERPNext's email_id), else any contact linked to it."""
	if not customers:
		return {}
	out = {}
	for c in frappe.get_all(
		"Customer", filters={"name": ["in", customers]}, fields=["name", "einvoice_contact_email", "email_id"]
	):
		for value, source in ((c.einvoice_contact_email, "einvoice"), (c.email_id, "primary")):
			if value and validate_email_address(value.strip()):
				out[c.name] = (value.strip(), source)
				break
	rest = [c for c in customers if c not in out]
	if rest:
		Contact = frappe.qb.DocType("Contact")
		Link = frappe.qb.DocType("Dynamic Link")
		rows = (
			frappe.qb.from_(Contact)
			.join(Link)
			.on(Link.parent == Contact.name)
			.select(Link.link_name, Contact.email_id)
			.where(
				(Link.parenttype == "Contact")
				& (Link.link_doctype == "Customer")
				& (Link.link_name.isin(rest))
				& (Contact.email_id.isnotnull())
				& (Contact.email_id != "")
			)
			.orderby(Contact.is_primary_contact, order=frappe.qb.desc)
			.orderby(Contact.creation)
		).run(as_dict=True)
		for r in rows:
			if r.link_name not in out and validate_email_address(r.email_id.strip()):
				out[r.link_name] = (r.email_id.strip(), "contact")
	return out


def recipient(customer: str) -> str | None:
	"""Where the form goes for one customer; see contact_emails."""
	found = contact_emails([customer]).get(customer)
	return found[0] if found else None


def _pending_request(customer: str) -> dict:
	"""The customer's open link if one still works, so an earlier email keeps working; else a new one."""
	row = frappe.db.get_value(
		REQUEST,
		{"customer": customer, "status": "Pending"},
		["name", "form_url", "expires_on", "key"],
		as_dict=True,
		order_by="creation desc",
	)
	if row and not (row.expires_on and now_datetime() > row.expires_on):
		return {"name": row.name, "url": row.form_url, "expires_on": row.expires_on}
	return create_request(customer)


def _outgoing_account():
	from frappe.email.doctype.email_account.email_account import EmailAccount

	try:
		return EmailAccount.find_outgoing(match_by_doctype=REQUEST)
	except Exception:
		return None


def _check_template(subject: str, body: str, customer: str):
	try:
		return _render(subject, body, customer, "x", None)
	except Exception as e:
		# render_template wraps the error and traceback in HTML; the first line is the useful part.
		text = re.sub(r"<[^>]+>", "\n", str(e)).strip().split("\n")[0]
		frappe.throw(_("The template has an error: {0}").format(text), title=_("Template error"))


def _render(subject: str, body: str, customer: str, link: str, expires_on) -> tuple[str, str]:
	from frappe.utils import format_datetime

	ctx = {
		"customer": customer,
		"customer_name": frappe.db.get_value("Customer", customer, "customer_name") or customer,
		"company": frappe.defaults.get_global_default("company") or "",
		"link": link,
		"expires_on": format_datetime(expires_on, "d MMMM yyyy") if expires_on else "",
	}
	return frappe.render_template(subject, ctx), frappe.render_template(body, ctx)


@frappe.whitelist()
def get_email_template() -> dict:
	frappe.has_permission(REQUEST, "create", throw=True)
	return email_template()


@frappe.whitelist()
def preview_email(customers: list | str, subject: str, body: str) -> dict:
	"""What the email will look like for the first customer, and who it would go to. Makes no link."""
	frappe.has_permission(REQUEST, "create", throw=True)
	customers = frappe.parse_json(customers) if isinstance(customers, str) else customers
	emails = contact_emails(customers)
	recipients = [
		{"customer": c, "email": emails.get(c, (None, None))[0], "source": emails.get(c, (None, None))[1]}
		for c in customers
	]
	first = customers[0] if customers else None
	if not first:
		return {"recipients": [], "subject": "", "body": ""}
	sample = form_url("example-link-made-when-sent")
	days = frappe.get_single(FORM).link_validity_days or 0
	_check_template(subject, body, first)
	rendered = _render(subject, body, first, sample, add_days(now_datetime(), days) if days > 0 else None)
	return {
		"recipients": recipients,
		"customer": first,
		"subject": rendered[0],
		"body": rendered[1],
		"no_email_account": not _outgoing_account(),
	}


@frappe.whitelist()
def send_bulk(customers: list | str, subject: str, body: str, save_template: int = 0) -> dict:
	"""Queue the form email to each customer that has an address; return who was skipped."""
	frappe.has_permission(REQUEST, "create", throw=True)
	customers = frappe.parse_json(customers) if isinstance(customers, str) else customers
	if not customers:
		frappe.throw(_("Select at least one customer."))
	if not _outgoing_account():
		frappe.throw(_("Set up a default outgoing Email Account first (Email Account list)."))
	_check_template(subject, body, customers[0])  # fail here, not in the job
	if int(save_template or 0):
		form = frappe.get_single(FORM)
		form.email_subject, form.email_body = subject, body
		form.save()
	to_send, skipped = [], []
	emails = contact_emails(customers)
	for c in customers:
		frappe.has_permission("Customer", "read", c, throw=True)
		(to_send if c in emails else skipped).append(c)
	if to_send:
		frappe.enqueue(
			"flick_einvoicing.data_form.send_emails",
			queue="long",
			timeout=3600,
			customers=to_send,
			subject=subject,
			body=body,
			user=frappe.session.user,
		)
	return {"queued": len(to_send), "skipped": skipped}


def send_emails(customers: list, subject: str, body: str, user: str):
	frappe.set_user(user)
	sent = failed = 0
	for customer in customers:
		try:
			email = recipient(customer)
			if not email:
				continue
			req = _pending_request(customer)
			subj, html = _render(subject, body, customer, req["url"], req["expires_on"])
			frappe.sendmail(
				recipients=[email],
				subject=subj,
				message=html,
				reference_doctype=REQUEST,
				reference_name=req["name"],
				now=False,
			)
			frappe.db.set_value(
				REQUEST,
				req["name"],
				{
					"sent_on": now_datetime(),
					"sent_to": email,
					"send_count": (frappe.db.get_value(REQUEST, req["name"], "send_count") or 0) + 1,
				},
			)
			frappe.db.commit()
			sent += 1
		except Exception:
			frappe.db.rollback()
			failed += 1
			frappe.log_error(title=f"Data form email to {customer} failed")
	frappe.publish_realtime(
		"msgprint",
		{"message": _("Data form emails queued: {0}. Failed: {1}.").format(sent, failed), "title": _("Data Collection")},
		user=user,
	)


@frappe.whitelist()
def set_contact_email(customer: str, email: str | None = None) -> str:
	"""Set the customer's E-Invoicing Contact Email from the Readiness page."""
	email = (email or "").strip()
	if email and not validate_email_address(email):
		frappe.throw(_("{0} is not a valid email address.").format(frappe.bold(email)))
	doc = frappe.get_doc("Customer", customer)
	doc.check_permission("write")
	doc.einvoice_contact_email = email
	doc.save()
	return email


@frappe.whitelist()
def send_test_email(email: str, customer: str, subject: str, body: str) -> str:
	"""The email as `customer` would get it, sent to `email` instead. Makes no link and touches no
	customer or request: the link in it is a sample that does not open a form."""
	frappe.has_permission(REQUEST, "create", throw=True)
	email = (email or "").strip()
	if not validate_email_address(email):
		frappe.throw(_("{0} is not a valid email address.").format(frappe.bold(email)))
	if not _outgoing_account():
		frappe.throw(_("Set up a default outgoing Email Account first (Email Account list)."))
	_check_template(subject, body, customer)
	days = frappe.get_single(FORM).link_validity_days or 0
	subj, html = _render(
		subject, body, customer, form_url("test-link-not-valid"), add_days(now_datetime(), days) if days > 0 else None
	)
	frappe.sendmail(recipients=[email], subject=f"[TEST] {subj}", message=html, now=True)
	if frappe.are_emails_muted():
		return _("Emails are muted on this site (mute_emails), so the test was not delivered.")
	return _("Test email sent to {0}.").format(email)

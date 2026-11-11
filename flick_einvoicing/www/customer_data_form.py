import frappe

from flick_einvoicing.data_form import active_questions, get_open_request

no_cache = 1


def get_context(context):
	"""The one-time customer data form, opened from a link with ?key=."""
	context.no_cache = 1
	key = frappe.form_dict.get("key")
	doc, error = get_open_request(key)
	context.error = error
	context.key = key if doc else ""
	form = frappe.get_single("Customer Data Form")
	context.form_title = form.title or "Customer Data Form"
	context.intro = form.intro
	context.company = frappe.defaults.get_global_default("company") or ""
	context.questions = [] if error else [
		{
			"fieldname": q.fieldname,
			"label": q.label,
			"fieldtype": q.fieldtype,
			"reqd": q.reqd,
			"description": q.description,
			"choices": [c.strip() for c in (q.options or "").split("\n") if c.strip()],
		}
		for q in active_questions(form)
	]
	context.csrf_token = frappe.sessions.get_csrf_token() if frappe.session.user != "Guest" else ""
	return context

import frappe

no_cache = 1


def get_context(context):
	"""The e-invoicing help, for anyone signed in to the site."""
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/einvoicing-help"
		raise frappe.Redirect

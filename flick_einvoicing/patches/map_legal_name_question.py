import frappe


def execute():
	"""Point the standard legal-name question at the new Customer Legal Name field."""
	if not frappe.db.exists("DocType", "Customer Data Form Field"):
		return
	frappe.db.sql(
		"""update `tabCustomer Data Form Field`
		set customer_field = 'einvoice_legal_name'
		where parent = 'Customer Data Form'
			and label like 'Full legal name of your company%%'
			and ifnull(customer_field, '') = ''"""
	)

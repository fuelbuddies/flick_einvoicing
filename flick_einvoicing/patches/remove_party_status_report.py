import frappe


def execute():
	"""The Party E-Invoicing Status report became the E-Invoicing Readiness page."""
	if frappe.db.exists("Report", "Party E-Invoicing Status"):
		frappe.delete_doc("Report", "Party E-Invoicing Status", force=True, ignore_permissions=True)

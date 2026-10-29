import frappe


def execute():
	"""The app's workspace is now called Flick. Drop the E-Invoicing one it used to install."""
	if frappe.db.get_value("Workspace", "E-Invoicing", "module") == "Flick E-Invoicing":
		frappe.delete_doc("Workspace", "E-Invoicing", force=True, ignore_permissions=True)

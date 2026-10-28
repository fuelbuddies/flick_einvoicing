import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from flick_einvoicing.custom_fields import get_custom_fields

UOM_CODE_LIST = "UN/ECE Recommendation 20"

# The units Flick's UAE docs list. Linking ERPNext UOMs to them is the client's decision
# (for instance whether "Nos" means H87 piece or C62 unit), so nothing is linked here.
UOM_CODES = {
	"H87": "Piece",
	"C62": "One (unit)",
	"KGM": "Kilogram",
	"MTR": "Metre",
	"LTR": "Litre",
	"DAY": "Day",
}


def after_install():
	ensure_doctypes()
	sync_custom_fields()
	ensure_uom_code_list()


def ensure_doctypes():
	"""Frappe syncs an app's doctypes from a cached module map. When that cache predates the
	app (installed with the web server running, say), the sync skips it without a word, and
	the app is "installed" with none of its doctypes. Sync them here if they are missing."""
	if frappe.db.exists("DocType", "E Invoice Submission"):
		return
	from frappe.model.sync import sync_for

	frappe.clear_cache()
	frappe.setup_module_map()
	# Not forced: on a developer-mode bench a forced re-import of an existing workspace deletes
	# its JSON from the app folder.
	sync_for("flick_einvoicing", reset_permissions=True)


def after_migrate():
	sync_custom_fields()
	ensure_uom_code_list()


def before_uninstall():
	for doctype, fields in get_custom_fields().items():
		for field in fields:
			name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": field["fieldname"]})
			if name:
				frappe.delete_doc("Custom Field", name, ignore_permissions=True)
		frappe.clear_cache(doctype=doctype)


def sync_custom_fields():
	create_custom_fields(get_custom_fields(), ignore_validate=True, update=True)


def ensure_uom_code_list():
	if not frappe.db.exists("DocType", "Code List"):
		return  # ERPNext without the EDI module; settings validation explains what is missing

	if not frappe.db.exists("Code List", UOM_CODE_LIST):
		code_list = frappe.new_doc("Code List")
		code_list.title = UOM_CODE_LIST
		code_list.publisher = "UNECE"
		code_list.canonical_uri = "urn:un:unece:uncefact:codelist:standard:UNECE:MeasurementUnitCommonCode"
		code_list.description = (
			"Units of measure for UAE e-invoicing. Link each ERPNext UOM to one code under Applies To."
		)
		code_list.insert(ignore_permissions=True, set_name=UOM_CODE_LIST)

	existing = set(frappe.get_all("Common Code", filters={"code_list": UOM_CODE_LIST}, pluck="common_code"))
	for code, title in UOM_CODES.items():
		if code in existing:
			continue
		frappe.get_doc(
			{"doctype": "Common Code", "code_list": UOM_CODE_LIST, "common_code": code, "title": title}
		).insert(ignore_permissions=True)

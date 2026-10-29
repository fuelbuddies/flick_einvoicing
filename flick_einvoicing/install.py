from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from flick_einvoicing.custom_fields import get_custom_fields
from flick_einvoicing.einvoice import code_lists

UOM_CODE_LIST = "UN/ECE Recommendation 20"
DASHBOARD_BLOCK = "Flick Dashboard"
DASHBOARD_FILES = Path(__file__).with_name("dashboard")
# The roles that may read E Invoice Submission, which the block's figures come from.
DASHBOARD_ROLES = ("System Manager", "Accounts Manager", "Accounts User")


def after_install():
	ensure_doctypes()
	sync_custom_fields()
	ensure_uom_code_list()
	ensure_dashboard()


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
	ensure_dashboard()


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
	"""Every unit code PINT-AE allows, kept in step with the list bundled in the app. Linking
	ERPNext UOMs to codes is the client's decision (whether "Nos" is H87 piece or C62 one, for
	instance), so nothing is linked here."""
	if not frappe.db.exists("DocType", "Code List"):
		return  # ERPNext without the EDI module; settings validation explains what is missing

	source = code_lists.read(code_lists.UNITS)
	details = {
		"description": "Units of measure for UAE e-invoicing: every code PINT-AE allows. "
		"Link each ERPNext UOM you invoice in to one code under Applies To.",
		"version": f"PINT-AE list issued {source['issued']}",
		"url": source["source"],
	}
	if frappe.db.exists("Code List", UOM_CODE_LIST):
		current = frappe.db.get_value("Code List", UOM_CODE_LIST, list(details), as_dict=True)
		changed = {field: value for field, value in details.items() if current.get(field) != value}
		if changed:
			frappe.db.set_value("Code List", UOM_CODE_LIST, changed)
	else:
		code_list = frappe.new_doc("Code List")
		code_list.update(details)
		code_list.title = UOM_CODE_LIST
		code_list.publisher = "UNECE"
		code_list.canonical_uri = "urn:un:unece:uncefact:codelist:standard:UNECE:MeasurementUnitCommonCode"
		code_list.insert(ignore_permissions=True, set_name=UOM_CODE_LIST)

	code_lists.load_units(UOM_CODE_LIST)


def ensure_dashboard():
	"""The Flick workspace's dashboard, a Custom HTML Block kept in step with the files in
	flick_einvoicing/dashboard. Custom HTML Blocks belong to no module, so the app cannot ship
	one as a document; edits made to the block on a site are replaced on the next migrate."""
	content = {
		field: (DASHBOARD_FILES / f"flick_dashboard.{extension}").read_text(encoding="utf-8")
		for field, extension in (("html", "html"), ("style", "css"), ("script", "js"))
	}
	if frappe.db.exists("Custom HTML Block", DASHBOARD_BLOCK):
		block = frappe.get_doc("Custom HTML Block", DASHBOARD_BLOCK)
		roles = {row.role for row in block.roles}
		if all(block.get(field) == value for field, value in content.items()) and roles == set(
			DASHBOARD_ROLES
		):
			return
	else:
		block = frappe.new_doc("Custom HTML Block")
	block.update(content)
	block.private = 0
	block.set("roles", [{"role": role} for role in DASHBOARD_ROLES])
	if block.is_new():
		block.insert(ignore_permissions=True, set_name=DASHBOARD_BLOCK)
	else:
		block.save(ignore_permissions=True)

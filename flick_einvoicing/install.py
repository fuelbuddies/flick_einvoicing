from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils.nestedset import get_root_of

from flick_einvoicing.custom_fields import get_custom_fields
from flick_einvoicing.einvoice import code_lists

UOM_CODE_LIST = "UN/ECE Recommendation 20"
DASHBOARD_BLOCK = "Flick Dashboard"
DASHBOARD_FILES = Path(__file__).with_name("dashboard")
# The roles that may read E Invoice Submission, which the block's figures come from.
DASHBOARD_ROLES = ("System Manager", "Accounts Manager", "Accounts User")


def after_install():
	ensure_doctypes()
	use_commercial_rounding_on_new_site()
	sync_custom_fields()
	ensure_uom_code_list()
	ensure_dashboard()
	ensure_placeholder_item()


def use_commercial_rounding_on_new_site():
	"""Flick rounds half a fil up; ERPNext's default rounds it to even. A site with no accounting yet
	starts on Commercial Rounding so the two agree. A site already keeping books is left alone:
	changing its rounding is a person's call, from the warning in E Invoicing Settings."""
	from flick_einvoicing.flick_e_invoicing.doctype.e_invoicing_settings.e_invoicing_settings import (
		COMMERCIAL,
		uses_bankers_rounding,
	)

	if not uses_bankers_rounding() or frappe.db.exists("GL Entry", {}):
		return
	frappe.db.set_single_value("System Settings", "rounding_method", COMMERCIAL)
	frappe.clear_cache()


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


def after_setup_wizard(args=None):
	"""A site the app was installed on before its setup wizard had no item groups yet."""
	ensure_placeholder_item()


def after_migrate():
	sync_custom_fields()
	ensure_uom_code_list()
	ensure_dashboard()
	ensure_placeholder_item()


def ensure_placeholder_item() -> str | None:
	"""The item a received e-invoice line waits on until someone picks ours, on every site: never
	sold or stocked, and no purchase invoice is submitted while a line is on it. Sites that number
	items themselves give it their own name, so the name is kept as a default."""
	from flick_einvoicing.einvoice.inbound import PLACEHOLDER_CODE, PLACEHOLDER_KEY, placeholder_item

	name = placeholder_item() or frappe.db.get_value("Item", {"item_code": PLACEHOLDER_CODE})
	if name:
		frappe.db.set_default(PLACEHOLDER_KEY, name)
		if frappe.db.get_value("Item", name, "is_sales_item"):
			frappe.db.set_value("Item", name, "is_sales_item", 0)
		return name
	root = get_root_of("Item Group")
	if not root:
		return None  # a new site before its setup wizard, which makes the item groups and then calls this
	frappe.db.savepoint("flick_placeholder")
	try:
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": PLACEHOLDER_CODE,
				"item_name": "Unmatched e-invoice line",
				"description": "Holds a line of a received e-invoice that no item of ours matched, until someone "
				"picks ours. Never sold or stocked; a purchase invoice can't be submitted while a line is on it.",
				# Services where there is such a group, else the root: never a group of the client's goods.
				"item_group": frappe.db.get_value("Item Group", {"name": "Services", "is_group": 0}) or root,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 0,
				"is_purchase_item": 1,
				"include_item_in_manufacturing": 0,
			}
		)
		item.flags.ignore_mandatory = True  # some sites ask items for fields of their own
		item.insert(ignore_permissions=True)
	except Exception:
		# Never stop a migrate over it: unmatched lines then stay lines without an item.
		frappe.db.rollback(save_point="flick_placeholder")
		frappe.log_error("E-invoicing: could not create the item for unmatched lines")
		return None
	frappe.db.set_default(PLACEHOLDER_KEY, item.name)
	return item.name


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

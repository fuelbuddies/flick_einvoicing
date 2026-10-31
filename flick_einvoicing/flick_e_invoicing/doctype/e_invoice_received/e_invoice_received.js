// Copyright (c) 2026, Ashar Shamsudheen and contributors
// For license information, please see license.txt

frappe.ui.form.on("E Invoice Received", {
	refresh(frm) {
		if (frm.doc.notes) {
			frm.dashboard.set_headline_alert(`<div>${frappe.utils.escape_html(frm.doc.notes).replace(/\n/g, "<br>")}</div>`, "orange");
		}
		if (frm.doc.status === "New" && !frm.doc.supplier) {
			// Saving the supplier with the sender's Peppol ID or TRN links this invoice, and any others from it.
			frm.add_custom_button(__("Create Supplier"), () => {
				const supplier = frappe.model.get_new_doc("Supplier");
				Object.assign(supplier, {
					supplier_name: frm.doc.sender_name,
					tax_id: frm.doc.sender_vat,
					einvoice_peppol_id: frm.doc.sender_peppol_id,
				});
				if ((frm.doc.sender_peppol_id || "").startsWith("0235:")) {
					supplier.country = "United Arab Emirates";
				}
				frappe.set_route("Form", "Supplier", supplier.name);
			});
		}
		if (frm.doc.status === "New") {
			frm.add_custom_button(__("Create Purchase Invoice"), () =>
				frappe
					.call({
						method: "flick_einvoicing.api.create_purchase_invoice",
						args: { name: frm.doc.name },
						freeze: true,
					})
					.then(({ message }) => frappe.set_route("Form", "Purchase Invoice", message))
			);
			frm.add_custom_button(__("Ignore"), () =>
				frappe.call({ method: "flick_einvoicing.api.ignore_received", args: { name: frm.doc.name } }).then(() => frm.reload_doc())
			);
		}
		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Purchase Invoice"), () => frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice), __("View"));
		}
		frm.add_custom_button(
			__("How drafts are made"),
			() => {
				frappe.flags.flick_guide_section = "receiving";
				frappe.set_route("Form", "E Invoicing Guide");
			},
			__("View")
		);
	},
});

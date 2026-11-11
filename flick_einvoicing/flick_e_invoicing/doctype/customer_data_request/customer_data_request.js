// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.ui.form.on("Customer Data Request", {
	refresh(frm) {
		if (frm.doc.status === "Pending" && frm.doc.form_url) {
			frm.add_custom_button(__("Copy Link"), () => frappe.utils.copy_to_clipboard(frm.doc.form_url));
		}
		if (frm.doc.status === "Submitted") {
			frm.add_custom_button(__("Apply to Customer"), () =>
				frappe
					.call({ method: "flick_einvoicing.data_form.apply_to_customer", args: { name: frm.doc.name }, freeze: true })
					.then(({ message: changed }) => {
						frappe.show_alert({
							message: changed.length ? __("Updated on {0}: {1}", [frm.doc.customer, changed.join(", ")]) : __("Nothing to update."),
							indicator: "green",
						});
						frm.reload_doc();
					})
			).addClass("btn-primary");
		}
	},
});

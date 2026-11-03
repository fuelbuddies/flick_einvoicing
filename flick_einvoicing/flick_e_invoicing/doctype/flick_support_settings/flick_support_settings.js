// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.ui.form.on("Flick Support Settings", {
	refresh(frm) {
		frm.set_intro(
			__(
				"When on, Flick errors (code lines only, no field values), Flick versions and the tickets people raise here go to the support team, with the reporter's name and email. Nothing else on this site is sent. Turn it off at any time; e-invoicing keeps working."
			)
		);
		if (frm.doc.hub_url && frm.doc.site_key) {
			frm.add_custom_button(__("Test Connection"), () =>
				frappe.xcall("flick_einvoicing.support.test_connection").then((status) => {
					frappe.show_alert(status);
					frm.reload_doc();
				})
			);
		}
	},
});

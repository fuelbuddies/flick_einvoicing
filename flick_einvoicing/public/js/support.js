// "Report Issue" on the forms where Flick's work shows up. Only while support is turned on.
(() => {
	const doctypes = ["Sales Invoice", "POS Invoice", "Purchase Invoice", "E Invoice Submission", "E Invoice Log", "E Invoice Received", "E Invoicing Settings"];
	doctypes.forEach((doctype) =>
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				if (!frappe.boot.flick_support || frm.is_new()) return;
				frm.add_custom_button(__("Report Issue"), () =>
					frappe.new_doc("E Invoice Support Report", {
						route: frappe.get_route_str().slice(0, 140),
						related_doctype: frm.doctype,
						related_name: frm.docname,
					})
				);
			},
		})
	);
})();

// E-invoice status on Sales Invoice and POS Invoice. Loaded for both doctypes, registered once.
frappe.provide("flick_einvoicing");

if (!flick_einvoicing.invoice_form_registered) {
	flick_einvoicing.invoice_form_registered = true;

	const colors = {
		Queued: "blue",
		Rejected: "red",
		Error: "orange",
		Cancelled: "gray",
		Processing: "yellow",
		Completed: "green",
		Failed: "red",
	};

	const handlers = {
		onload(frm) {
			// Cancelling is decided by the invoice's own rules, not by cancelling submissions with it.
			const ignored = frm.ignore_doctypes_on_cancel_all || [];
			if (!ignored.includes("E Invoice Submission")) {
				frm.ignore_doctypes_on_cancel_all = ignored.concat(["E Invoice Submission"]);
			}
		},
		refresh(frm) {
			if (!frm.doc.einvoice_submission) return;
			frm.dashboard.add_indicator(
				__("E-Invoice: {0}", [__(frm.doc.einvoice_status || "")]),
				colors[frm.doc.einvoice_status] || "gray"
			);
			frm.add_custom_button(
				__("E-Invoice Submission"),
				() => frappe.set_route("Form", "E Invoice Submission", frm.doc.einvoice_submission),
				__("View")
			);
		},
	};

	frappe.ui.form.on("Sales Invoice", handlers);
	frappe.ui.form.on("POS Invoice", handlers);
}

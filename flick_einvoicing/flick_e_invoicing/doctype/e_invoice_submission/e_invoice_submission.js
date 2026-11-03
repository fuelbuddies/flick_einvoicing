// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

const EINVOICE_COLORS = {
	Queued: "blue",
	Rejected: "red",
	Error: "orange",
	Cancelled: "gray",
	Processing: "yellow",
	Completed: "green",
	Failed: "red",
};

frappe.ui.form.on("E Invoice Submission", {
	refresh(frm) {
		const doc = frm.doc;
		if (doc.docstatus === 0) {
			// Submitted means Flick accepted it; drafts are sent with Retry, never submitted by hand.
			frm.disable_save();
			frm.dashboard.clear_comment();
		} else {
			// Cancelled with its invoice, or replaced by Resubmit; never on its own.
			frm.page.clear_secondary_action();
		}
		if (doc.next_action) {
			frm.dashboard.set_headline_alert(
				`<div>${frappe.utils.escape_html(doc.next_action)}</div>`,
				EINVOICE_COLORS[doc.status] || "gray"
			);
		}

		const call = (method, message) =>
			frappe
				.call({ method: `flick_einvoicing.api.${method}`, args: { name: doc.name }, freeze: true, freeze_message: message })
				.then(({ message: result }) => {
					if (result && result !== doc.status && result.startsWith && result.startsWith("EINV-")) {
						frappe.set_route("Form", "E Invoice Submission", result);
					} else {
						frm.reload_doc();
					}
				});

		if (doc.docstatus === 0 && ["Queued", "Rejected", "Error"].includes(doc.status)) {
			frm.add_custom_button(__("Retry"), () => call("retry", __("Sending to Flick...")));
		}
		if (doc.docstatus === 0 && doc.status === "Error" && doc.attempt_count) {
			frm.add_custom_button(__("Check on Flick"), () => call("check_on_flick", __("Asking Flick...")));
		}
		if (doc.docstatus === 1) {
			frm.add_custom_button(__("Refresh Status"), () => call("check_on_flick", __("Asking Flick...")));
			const files = [
				["pdf", __("PDF")],
				["xml", __("UBL XML")],
				["DOCUMENT_TDD", __("Tax Data Document (FTA)")],
				["DOCUMENT_SBD_SIGNED", __("Signed Envelope")],
				["DOCUMENT_MLS_RECVD", __("Buyer's Receipt (MLS)")],
			];
			for (const [kind, label] of files) {
				frm.add_custom_button(
					label,
					() => window.open(`/api/method/flick_einvoicing.api.download?name=${encodeURIComponent(doc.name)}&kind=${kind}`),
					__("Download")
				);
			}
		}
		const released =
			doc.docstatus === 1 &&
			doc.reporting_status === "failed" &&
			doc.exchange_status === "failed" &&
			doc.reporting_settled &&
			doc.exchange_settled;
		if (released) {
			frm.add_custom_button(__("Resubmit"), () =>
				frappe.confirm(
					__(
						"Flick released this number because neither reporting nor delivery succeeded. Resubmit sends the invoice again, built from current data. Fix the cause first (see Error). Continue?"
					),
					() => call("resubmit", __("Resubmitting..."))
				)
			);
		}
	},
});

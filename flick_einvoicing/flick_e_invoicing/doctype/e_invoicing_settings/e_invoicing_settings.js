// Copyright (c) 2026, Ashar Shamsudheen and contributors
// For license information, please see license.txt

frappe.ui.form.on("E Invoicing Settings", {
	setup(frm) {
		frm.set_query("template", "tax_treatments", (doc) => ({ filters: { company: doc.company } }));
	},

	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Connection"), () => {
			frappe
				.call({
					method: "flick_einvoicing.api.test_connection",
					args: { company: frm.doc.name },
					freeze: true,
					freeze_message: __("Asking Flick..."),
				})
				.then(({ message }) => show_participants(frm, message));
		});

		if (frm.doc.enabled) {
			frm.add_custom_button(
				__("Sync Events Now"),
				() =>
					frappe
						.call({
							method: "flick_einvoicing.api.sync_events_now",
							args: { company: frm.doc.name },
							freeze: true,
						})
						.then(({ message }) => {
							frappe.show_alert({ message: __("{0} events read", [message]), indicator: "green" });
							frm.reload_doc();
						}),
				__("Actions")
			);
			frm.add_custom_button(
				__("Send Queued Now"),
				() =>
					frappe
						.call("flick_einvoicing.api.send_queued_now")
						.then(() => frappe.show_alert(__("Sending in the background"))),
				__("Actions")
			);
		}
	},
});

function show_participants(frm, result) {
	const rows = (result.participants || [])
		.map((p) => {
			const mine = p.id === frm.doc.participant_id;
			return `<tr${mine ? ' class="text-success"' : ""}>
				<td>${frappe.utils.escape_html(p.name || "")}</td>
				<td><code>${frappe.utils.escape_html(p.id || "")}</code></td>
				<td>${frappe.utils.escape_html(p.peppol_id || "")}</td>
				<td>${frappe.utils.escape_html(p.status || "")}${mine ? " ✓" : ""}</td>
			</tr>`;
		})
		.join("");
	const note = result.participant_found
		? __("Connected. The participant set here is on the list.")
		: __("Connected, but the Participant ID set here is not one of these. Copy the right one in.");
	frappe.msgprint({
		title: __("Flick participants for this key"),
		indicator: result.participant_found ? "green" : "orange",
		message: `<p>${note}</p>
			<table class="table table-bordered table-sm">
				<thead><tr><th>${__("Name")}</th><th>${__("ID")}</th><th>${__("Peppol ID")}</th><th>${__("Status")}</th></tr></thead>
				<tbody>${rows}</tbody>
			</table>`,
		wide: true,
	});
}

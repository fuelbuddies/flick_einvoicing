// Copyright (c) 2026, Ashar Shamsudheen and contributors
// For license information, please see license.txt

frappe.ui.form.on("E Invoicing Settings", {
	setup(frm) {
		frm.set_query("template", "tax_treatments", (doc) => ({ filters: { company: doc.company } }));
		frm.set_query("account", "charge_accounts", (doc) => ({ filters: { company: doc.company, is_group: 0 } }));
		frm.set_query("purchase_taxes_template", (doc) => ({ filters: { company: doc.company } }));
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const call = (method, message) =>
			frappe.call({ method: `flick_einvoicing.api.${method}`, args: { company: frm.doc.name }, freeze: true, freeze_message: message });

		frm.add_custom_button(__("Test Connection"), () =>
			call("test_connection", __("Asking Flick...")).then(({ message }) => show_participants(frm, message))
		);

		if (frm.doc.enabled) {
			frm.add_custom_button(
				__("Sync Events Now"),
				() =>
					call("sync_events_now").then(({ message }) => {
						frappe.show_alert({ message: __("{0} events read", [message]), indicator: "green" });
						frm.reload_doc();
					}),
				__("Actions")
			);
			frm.add_custom_button(
				__("Send Queued Now"),
				() => frappe.call("flick_einvoicing.api.send_queued_now").then(() => frappe.show_alert(__("Sending in the background"))),
				__("Actions")
			);
		}

		if (frm.doc.webhook_subscription_id) {
			frm.add_custom_button(__("Recent Deliveries"), () => call("webhook_deliveries").then(({ message }) => show_deliveries(message)), __("Webhook"));
			frm.add_custom_button(
				__("Rotate Secret"),
				() =>
					frappe.confirm(__("The old secret stops working at once. Rotate now?"), () =>
						call("rotate_webhook_secret").then(() => frappe.show_alert(__("Secret rotated")))
					),
				__("Webhook")
			);
			frm.add_custom_button(
				__("Remove"),
				() => frappe.confirm(__("Stop Flick posting to this site?"), () => call("remove_webhook").then(() => frm.reload_doc())),
				__("Webhook")
			);
		} else {
			frm.add_custom_button(
				__("Register Webhook"),
				() =>
					call("register_webhook", __("Registering with Flick...")).then(({ message }) => {
						frappe.msgprint(__("Flick will post status changes to {0}", [message.endpoint]));
						frm.reload_doc();
					}),
				__("Webhook")
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
		title: __("Flick participants for this credential"),
		indicator: result.participant_found ? "green" : "orange",
		message: `<p>${note}</p>
			<table class="table table-bordered table-sm">
				<thead><tr><th>${__("Name")}</th><th>${__("ID")}</th><th>${__("Peppol ID")}</th><th>${__("Status")}</th></tr></thead>
				<tbody>${rows}</tbody>
			</table>`,
		wide: true,
	});
}

function show_deliveries(deliveries) {
	const rows = (deliveries || [])
		.map(
			(d) => `<tr>
				<td>${frappe.utils.escape_html(d.created_at || "")}</td>
				<td>${frappe.utils.escape_html(d.event_type || "")}</td>
				<td>${frappe.utils.escape_html(d.status || "")}</td>
				<td>${frappe.utils.escape_html(String(d.response_status ?? ""))}</td>
				<td>${frappe.utils.escape_html(d.error || "")}</td>
			</tr>`
		)
		.join("");
	frappe.msgprint({
		title: __("Webhook deliveries"),
		message: rows
			? `<table class="table table-bordered table-sm"><thead><tr><th>${__("When")}</th><th>${__("Event")}</th><th>${__("Status")}</th><th>${__("HTTP")}</th><th>${__("Error")}</th></tr></thead><tbody>${rows}</tbody></table>`
			: __("No deliveries yet."),
		wide: true,
	});
}

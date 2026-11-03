// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.ui.form.on("E Invoicing Settings", {
	setup(frm) {
		frm.set_query("template", "tax_treatments", (doc) => ({ filters: { company: doc.company } }));
		frm.set_query("account", "charge_accounts", (doc) => ({ filters: { company: doc.company, is_group: 0 } }));
		frm.set_query("purchase_taxes_template", (doc) => ({ filters: { company: doc.company } }));
	},

	onload(frm) {
		// Charge reason codes: PINT-AE's UNCL 7161 list, bundled with the app.
		frappe.xcall("flick_einvoicing.api.charge_reasons").then((reasons) => {
			const field = frm.fields_dict.negative_discount_reason_code;
			field.df.options = reasons;
			field.set_data(reasons);
			frm.fields_dict.charge_accounts.grid.update_docfield_property("reason_code", "options", reasons);
		});
	},

	company(frm) {
		show_setup(frm);
	},

	refresh(frm) {
		show_setup(frm);
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

// What this company still needs before e-invoicing can be enabled; nothing once all is done.
function show_setup(frm) {
	if (!frm.doc.company) {
		frm.set_intro(__("Pick the company these settings are for. Each company that issues e-invoices has its own record."), "blue");
		return;
	}
	frappe.xcall("flick_einvoicing.api.setup_overview", { company: frm.doc.company }).then(([row]) => {
		if (!row) {
			frm.set_intro(__("{0} is not a UAE company: its country is not United Arab Emirates.", [frappe.utils.escape_html(frm.doc.company)]), "yellow");
			return;
		}
		const esc = frappe.utils.escape_html;
		const left = row.steps.filter((step) => !step.done);
		if (!left.length && !row.warnings.length) {
			frm.set_intro("");
			return;
		}
		const steps = row.steps
			.map(
				(step) => `<li class="${step.done ? "text-muted" : ""}">
					${step.done ? "&#10003;" : "&#9675;"} ${esc(step.label)}
					${step.detail ? `<div class="small">${esc(step.detail)}</div>` : ""}
				</li>`
			)
			.join("");
		const warnings = row.warnings.map((text) => `<p class="mb-0 mt-2">${esc(text)}</p>`).join("");
		let title;
		if (!left.length) title = __("E-invoicing is set up for {0}", [esc(row.company)]);
		else if (row.enabled) title = __("E-invoicing is on for {0}, with {1} of {2} setup steps left", [esc(row.company), left.length, row.steps.length]);
		else title = __("{0} of {1} steps left before e-invoicing can start for {2}", [left.length, row.steps.length, esc(row.company)]);
		frm.set_intro(`<b>${title}</b><ul class="list-unstyled mb-0 mt-2">${steps}</ul>${warnings}`, left.length ? "blue" : "yellow");
	});
}

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

// The rounding warning's button. Rounding is site-wide, so the change is confirmed first.
window.flick_einvoicing = window.flick_einvoicing || {};
flick_einvoicing.confirm_commercial_rounding = () => {
	frappe.confirm(
		__(
			"Rounding Method is set for the whole site, not only e-invoicing: every new document in every app rounds half up from now on. Documents already saved keep their amounts. Best done before invoicing starts or at the start of a period. Switch to Commercial Rounding?"
		),
		() =>
			frappe.xcall("flick_einvoicing.flick_e_invoicing.doctype.e_invoicing_settings.e_invoicing_settings.use_commercial_rounding").then(() => {
				frappe.hide_msgprint();
				frappe.show_alert({ message: __("Rounding Method is now Commercial Rounding"), indicator: "green" });
			})
	);
};

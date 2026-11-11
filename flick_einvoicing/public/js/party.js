// "Check Peppol" on Customer and Supplier. Loaded per doctype, registered once.
frappe.provide("flick_einvoicing");

if (!flick_einvoicing.party_form_registered) {
	flick_einvoicing.party_form_registered = true;

	const handlers = {
		refresh(frm) {
			if (frm.is_new() || !frm.doc.einvoice_peppol_id) return;
			frm.add_custom_button(
				__("Check Peppol"),
				() =>
					frappe
						.call({
							method: "flick_einvoicing.api.check_peppol",
							args: { doctype: frm.doctype, name: frm.doc.name },
							freeze: true,
						})
						.then(({ message: r }) => {
							const types = (r.supported_document_types || []).map((t) => frappe.utils.escape_html(t.name || t.id || t)).join(", ");
							let text = r.is_registered
								? __("{0} is registered on Peppol{1}.", [frappe.utils.escape_html(r.identifier), r.participant_name ? ` as ${frappe.utils.escape_html(r.participant_name)}` : ""])
								: __("{0} is not registered on Peppol.", [frappe.utils.escape_html(r.identifier || frm.doc.einvoice_peppol_id)]);
							if (types) text += `<br>${__("Receives")}: ${types}`;
							if (r.pre_production) text += `<br><small>${__("Pre-production lookups ask the production network, so test participants always show as not registered.")}</small>`;
							frappe.msgprint({ title: __("Peppol"), message: text, indicator: r.is_registered ? "green" : "orange" });
						}),
				__("E-Invoice")
			);
		},
	};

	frappe.ui.form.on("Customer", handlers);
	// One-time link the customer opens to send its e-invoicing master data. Each click makes a
	// new link and retires any earlier one that was not used.
	frappe.ui.form.on("Customer", {
		refresh(frm) {
			if (frm.is_new()) return;
			frm.add_custom_button(
				__("Data Collection Form"),
				() =>
					frappe
						.call({ method: "flick_einvoicing.data_form.create_request", args: { customer: frm.doc.name }, freeze: true })
						.then(({ message: r }) => {
							const d = new frappe.ui.Dialog({
								title: __("Customer Data Collection Form"),
								fields: [
									{ fieldtype: "Data", fieldname: "url", label: __("Link"), read_only: 1, default: r.url },
									{
										fieldtype: "HTML",
										options: `<p class="text-muted small">${__("Works once. Earlier unused links for this customer no longer work.")}${
											r.expires_on ? " " + __("Expires {0}.", [frappe.datetime.str_to_user(r.expires_on)]) : ""
										} <a href="/app/customer-data-request/${encodeURIComponent(r.name)}">${frappe.utils.escape_html(r.name)}</a></p>`,
									},
								],
								primary_action_label: __("Copy Link"),
								primary_action() {
									frappe.utils.copy_to_clipboard(r.url);
									d.hide();
								},
							});
							d.show();
						}),
				__("E-Invoice")
			);
		},
	});
	frappe.ui.form.on("Supplier", handlers);
}

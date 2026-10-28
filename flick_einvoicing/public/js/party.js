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
	frappe.ui.form.on("Supplier", handlers);
}

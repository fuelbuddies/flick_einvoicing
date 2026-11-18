// Document-level charge mapping for an Account, shown and edited on the account itself.
// The mapping lives in the company's E Invoicing Settings, Document-Level Charges.
frappe.ui.form.on("Account", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.is_group) return;
		frappe.xcall("flick_einvoicing.api.charge_account", { account: frm.doc.name }).then((info) => {
			if (!info || !info.applies) return;
			const row = info.row;
			if (row) {
				const text = `${__("E-invoice charge")}: ${row.reason_code}${row.reason ? " (" + row.reason + ")" : ""}, ${row.tax_category}`;
				frm.dashboard.add_comment(frappe.utils.escape_html(text), "blue", true);
			}
			frm.add_custom_button(row ? __("Edit E-Invoice Charge") : __("Set as E-Invoice Charge"), () => charge_dialog(frm, row || {}), __("E-Invoice"));
			if (row) {
				frm.add_custom_button(
					__("Remove E-Invoice Charge"),
					() =>
						frappe.call({
							method: "flick_einvoicing.api.set_charge_account",
							args: { account: frm.doc.name, remove: 1 },
							freeze: true,
						}).then(() => frm.refresh()),
					__("E-Invoice")
				);
			}
		});
	},
});

function charge_dialog(frm, row) {
	Promise.all([
		frappe.xcall("flick_einvoicing.api.charge_reasons"),
		new Promise((resolve) => frappe.model.with_doctype("E Invoicing Charge Account", resolve)),
	]).then(([reasons]) => {
		const categories = frappe.meta.get_docfield("E Invoicing Charge Account", "tax_category").options;
		const d = new frappe.ui.Dialog({
			title: __("E-Invoice Charge: {0}", [frm.doc.name]),
			fields: [
				{
					fieldname: "reason_code",
					label: __("Reason Code (UNCL 7161)"),
					fieldtype: "Autocomplete",
					options: reasons,
					reqd: 1,
					default: row.reason_code,
				},
				{ fieldname: "reason", label: __("Reason"), fieldtype: "Data", default: row.reason },
				{ fieldname: "tax_category", label: __("VAT Category"), fieldtype: "Select", options: categories, reqd: 1, default: row.tax_category },
			],
			primary_action_label: __("Save"),
			primary_action: (values) =>
				frappe
					.call({ method: "flick_einvoicing.api.set_charge_account", args: { account: frm.doc.name, values }, freeze: true })
					.then(() => {
						d.hide();
						frappe.show_alert({ message: __("Saved to E Invoicing Settings for {0}", [frm.doc.company]), indicator: "green" });
						frm.refresh();
					}),
		});
		d.show();
	});
}

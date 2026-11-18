// PINT-AE tax treatment for a VAT template, shown and edited on the template itself.
// The mapping lives in the company's E Invoicing Settings, Tax Treatments; this reads and writes that row.

["Item Tax Template", "Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"].forEach((doctype) =>
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (frm.is_new() || !frm.doc.company) return;
			flick_tax_treatment.show(frm);
		},
	})
);

const flick_tax_treatment = {
	method: (name) => `flick_einvoicing.api.${name}`,

	show(frm) {
		frappe
			.call({ method: this.method("tax_treatment"), args: { template_type: frm.doctype, template: frm.doc.name } })
			.then(({ message: info }) => {
				if (!info || !info.settings) return;
				const row = info.row;
				frm.dashboard.clear_comment();
				if (row) {
					frm.dashboard.add_comment(`${__("PINT-AE")}: ${frappe.utils.escape_html(row.tax_category)}`, "blue", true);
				} else {
					frm.dashboard.add_comment(__("Not mapped for e-invoicing: invoices using this template will not submit."), "orange", true);
				}
				frm.add_custom_button(__("Set PINT-AE Treatment"), () => this.dialog(frm, row || {}), __("E-Invoice"));
			});
	},

	dialog(frm, row) {
		frappe.model.with_doctype("E Invoicing Tax Treatment", () => {
			const meta = (f) => frappe.meta.get_docfield("E Invoicing Tax Treatment", f);
			const field = (f, extra) => ({
				fieldname: f,
				label: meta(f).label,
				fieldtype: meta(f).fieldtype,
				options: meta(f).options,
				default: row[f],
				...extra,
			});
			const d = new frappe.ui.Dialog({
				title: __("PINT-AE Treatment for {0}", [frm.doc.name]),
				fields: [
					field("tax_category", { reqd: 1 }),
					field("exemption_reason_code", { depends_on: "eval:(doc.tax_category || '').startsWith('E ')" }),
					field("exemption_reason_text", { depends_on: "eval:(doc.tax_category || '').startsWith('E ')" }),
					field("reverse_charge_item_type", { depends_on: "eval:(doc.tax_category || '').startsWith('AE ')" }),
				],
				primary_action_label: __("Save"),
				primary_action: (values) =>
					frappe
						.call({
							method: this.method("set_tax_treatment"),
							args: { template_type: frm.doctype, template: frm.doc.name, values },
							freeze: true,
						})
						.then(() => {
							d.hide();
							frappe.show_alert({ message: __("Saved to E Invoicing Settings for {0}", [frm.doc.company]), indicator: "green" });
							frm.refresh();
						}),
			});
			d.show();
		});
	},
};

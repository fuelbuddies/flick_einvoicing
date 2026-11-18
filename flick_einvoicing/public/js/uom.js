// The PINT-AE unit code an e-invoice sends for this UOM, read from and set in ERPNext's Common Codes.
frappe.ui.form.on("UOM", {
	refresh(frm) {
		if (frm.is_new()) return;
		frappe.xcall("flick_einvoicing.api.uom_code", { uom: frm.doc.name }).then((info) => {
			if (!info || !info.applies) return;
			const esc = frappe.utils.escape_html;
			const found = info.codes;
			if (!found.length) {
				frm.dashboard.add_comment(__("No PINT-AE unit code: invoices with this UOM will not submit."), "orange", true);
			} else if (found.length > 1) {
				frm.dashboard.add_comment(esc(__("More than one unit code linked: {0}. Keep one.", [found.map((c) => c.code).join(", ")])), "orange", true);
			} else if (!found[0].valid) {
				frm.dashboard.add_comment(esc(__("Unit code {0} is not in PINT-AE's list.", [found[0].code])), "orange", true);
			} else {
				frm.dashboard.add_comment(esc(`${__("PINT-AE unit")}: ${found[0].code} (${found[0].name})`), "blue", true);
			}
			frm.add_custom_button(__("Set PINT-AE Unit Code"), () => unit_dialog(frm, found.length === 1 ? found[0].code : ""), __("E-Invoice"));
		});
	},
});

function unit_dialog(frm, current) {
	const d = new frappe.ui.Dialog({
		title: __("PINT-AE Unit Code for {0}", [frm.doc.name]),
		fields: [
			{
				fieldname: "code",
				label: __("Unit Code"),
				fieldtype: "Autocomplete",
				reqd: 1,
				default: current,
				description: __("Type a code or a name, e.g. LTR litre, KGM kilogram, H87 piece."),
				get_query: () => ({ query: "flick_einvoicing.api.unit_codes" }),
			},
		],
		primary_action_label: __("Save"),
		primary_action: ({ code }) =>
			frappe.call({ method: "flick_einvoicing.api.set_uom_code", args: { uom: frm.doc.name, code }, freeze: true }).then(() => {
				d.hide();
				frm.refresh();
			}),
	});
	d.show();
}

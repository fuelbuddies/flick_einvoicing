// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.query_reports["Party E-Invoicing Status"] = {
	filters: [
		{ fieldname: "party_type", label: __("Party Type"), fieldtype: "Select", options: "Customer\nSupplier", default: "Customer", reqd: 1,
			on_change() {
				frappe.query_report.set_filter_value("party", "");
				frappe.query_report.refresh();
			} },
		{ fieldname: "party", label: __("Party"), fieldtype: "Dynamic Link", get_options: () => frappe.query_report.get_filter_value("party_type") },
		{ fieldname: "customer_group", label: __("Customer Group"), fieldtype: "Link", options: "Customer Group",
			depends_on: "eval:doc.party_type=='Customer'" },
		{ fieldname: "supplier_group", label: __("Supplier Group"), fieldtype: "Link", options: "Supplier Group",
			depends_on: "eval:doc.party_type=='Supplier'" },
		{ fieldname: "only_incomplete", label: __("Only Incomplete"), fieldtype: "Check" },
	],

	formatter(value, row, column, data, default_formatter) {
		if (column.fieldname === "action" && data && data.party) {
			const label = data.form_status === "Pending" ? __("New Link") : __("Generate Link");
			return `<button class="btn btn-xs btn-default" data-flick-form="${encodeURIComponent(data.party)}">${label}</button>`;
		}
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data) {
			return `<span class="indicator-pill ${data.status === "Ready" ? "green" : "orange"}">${value}</span>`;
		}
		if (column.fieldname === "form_status" && data && data.form_status) {
			const color = { Pending: "blue", Submitted: "orange", Applied: "green", Cancelled: "gray" }[data.form_status] || "gray";
			return `<span class="indicator-pill ${color}">${value}</span>`;
		}
		return value;
	},

	onload(report) {
		// One delegated handler for the per-row buttons; rows are re-rendered on every refresh.
		$(report.page.wrapper).on("click", "[data-flick-form]", (e) => {
			e.preventDefault();
			e.stopPropagation();
			const customer = decodeURIComponent(e.currentTarget.dataset.flickForm);
			frappe
				.call({ method: "flick_einvoicing.data_form.create_request", args: { customer }, freeze: true })
				.then(({ message: r }) => {
					frappe.utils.copy_to_clipboard(r.url);
					frappe.msgprint({
						title: __("Link for {0}", [frappe.utils.escape_html(customer)]),
						message: `<p>${__("Copied to clipboard. Works once; earlier unused links stop working.")}</p><input class="form-control" readonly value="${frappe.utils.escape_html(r.url)}" onclick="this.select()">`,
					});
					report.refresh();
				});
		});
	},
};

// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.query_reports["E-Invoice Status"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: frappe.datetime.get_today() },
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nQueued\nRejected\nError\nCancelled\nProcessing\nCompleted\nFailed",
		},
		{ fieldname: "needs_attention", label: __("Needs Attention"), fieldtype: "Check" },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "status" && data) {
			const color = { Completed: "green", Processing: "orange", Queued: "blue", Failed: "red", Rejected: "red", Error: "red" }[data.status] || "gray";
			value = `<span class="indicator-pill ${color}">${value}</span>`;
		}
		return value;
	},
};

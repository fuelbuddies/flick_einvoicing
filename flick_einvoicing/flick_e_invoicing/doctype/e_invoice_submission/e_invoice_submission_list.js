frappe.listview_settings["E Invoice Submission"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			Queued: "blue",
			Rejected: "red",
			Error: "orange",
			Cancelled: "gray",
			Processing: "yellow",
			Completed: "green",
			Failed: "red",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};

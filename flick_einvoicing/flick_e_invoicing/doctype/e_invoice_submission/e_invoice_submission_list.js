frappe.listview_settings["E Invoice Submission"] = {
	add_fields: ["status"],
	// Queued, Rejected and Error are drafts: without this Frappe shows them all as "Draft".
	has_indicator_for_draft: 1,
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

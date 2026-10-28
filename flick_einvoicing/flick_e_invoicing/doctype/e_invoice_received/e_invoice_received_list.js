frappe.listview_settings["E Invoice Received"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = { New: "blue", "Purchase Invoice Created": "green", Ignored: "gray" };
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};

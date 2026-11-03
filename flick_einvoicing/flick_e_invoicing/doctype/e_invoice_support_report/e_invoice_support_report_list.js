// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.listview_settings["E Invoice Support Report"] = {
	add_fields: ["send_status", "remote_status"],
	get_indicator(doc) {
		if (doc.remote_status) {
			const closed = ["Resolved", "Closed"].includes(doc.remote_status);
			const color = closed ? "green" : doc.remote_status === "Waiting on Client" ? "orange" : "blue";
			return [__(doc.remote_status), color, `remote_status,=,${doc.remote_status}`];
		}
		const color = doc.send_status === "Queued" ? "gray" : doc.send_status === "Sent" ? "blue" : "red";
		return [__(doc.send_status), color, `send_status,=,${doc.send_status}`];
	},
};

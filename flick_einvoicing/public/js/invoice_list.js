// E-Invoice Status as a pill in the invoice lists. Added to ERPNext's own list settings, never
// replacing them, so its status indicator and buttons stay.
(() => {
	const colors = { Queued: "blue", Rejected: "red", Error: "orange", Cancelled: "gray", Processing: "yellow", Completed: "green", Failed: "red" };
	["Sales Invoice", "POS Invoice", "Purchase Invoice"].forEach((name) => {
		const settings = (frappe.listview_settings[name] = frappe.listview_settings[name] || {});
		settings.add_fields = [...new Set([...(settings.add_fields || []), "einvoice_status"])];
		settings.formatters = settings.formatters || {};
		settings.formatters.einvoice_status = (value) =>
			value ? `<span class="indicator-pill ${colors[value] || "gray"} filterable" data-filter="einvoice_status,=,${value}">${__(value)}</span>` : "";
	});
})();

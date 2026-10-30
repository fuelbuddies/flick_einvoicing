// Copyright (c) 2026, Ashar Shamsudheen and contributors
// For license information, please see license.txt

// One record per company: say so above the list, with where each UAE company stands.
frappe.listview_settings["E Invoicing Settings"] = {
	onload(listview) {
		listview.flick_setup = $('<div class="frappe-card flick-setup mb-4"></div>').prependTo(listview.page.main);
		listview.flick_setup.on("click", "[data-company]", (event) => {
			const { company, action } = event.currentTarget.dataset;
			if (action === "new") frappe.new_doc("E Invoicing Settings", { company });
			else frappe.set_route("Form", "E Invoicing Settings", company);
		});
	},

	refresh(listview) {
		frappe.xcall("flick_einvoicing.api.setup_overview").then((rows) => render_setup(listview.flick_setup, rows));
	},
};

function render_setup($wrapper, rows) {
	const esc = frappe.utils.escape_html;
	const button = (label, action, company) =>
		`<button class="btn btn-xs btn-default" data-action="${action}" data-company="${esc(company)}">${label}</button>`;
	const body = rows
		.map((row) => {
			const left = row.steps.filter((step) => !step.done);
			let state, action;
			if (!row.record) {
				state = `<span class="indicator-pill gray">${__("Not set up")}</span>`;
				action = button(__("Set up"), "new", row.company);
			} else if (row.enabled) {
				state = `<span class="indicator-pill green">${__("Enabled")}</span>
					<span class="text-muted small ml-2">${esc(__(row.environment || ""))}</span>`;
				action = button(__("Open"), "open", row.company);
			} else {
				state = `<span class="indicator-pill orange">${__("{0} of {1} steps left", [left.length, row.steps.length])}</span>
					<span class="text-muted small ml-2">${__("Next: {0}", [esc(left[0].label)])}</span>`;
				action = button(__("Continue"), "open", row.company);
			}
			const warnings = row.warnings.map((text) => `<div class="small mt-1" style="color: var(--text-on-orange)">${esc(text)}</div>`).join("");
			return `<tr>
				<td class="text-nowrap"><b>${esc(row.company)}</b></td>
				<td>${state}${warnings}</td>
				<td class="text-right">${action}</td>
			</tr>`;
		})
		.join("");
	$wrapper.html(`
		<div class="p-3">
			<div class="mb-1"><b>${__("UAE e-invoicing through Flick")}</b></div>
			<p class="text-muted small mb-3">
				${__("Each company that issues e-invoices needs its own record here, named after the company. Only companies whose country is United Arab Emirates are listed.")}
			</p>
			${
				body
					? `<table class="table table-sm mb-3"><tbody>${body}</tbody></table>`
					: `<p class="mb-3">${__("No company on this site has United Arab Emirates as its country.")}</p>`
			}
			<p class="text-muted small mb-0">
				${__("Then, for every company: link the UOMs you invoice in to their unit codes (Code List {0}, Applies To); give customers a Peppol ID, their TRN in Tax ID and a billing address with its emirate; and before going live, open a saved draft invoice and click Check with Flick in its banner: Flick validates it without filing anything.", ["<b>UN/ECE Recommendation 20</b>"])}
			</p>
		</div>
	`);
}

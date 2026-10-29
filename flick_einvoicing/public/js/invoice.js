// E-invoicing on Sales, POS and Purchase Invoices. Loaded per doctype, registered once.
frappe.provide("flick_einvoicing");

if (!flick_einvoicing.invoice_form_registered) {
	flick_einvoicing.invoice_form_registered = true;

	const colors = {
		Queued: "blue",
		Rejected: "red",
		Error: "orange",
		Cancelled: "gray",
		Processing: "yellow",
		Completed: "green",
		Failed: "red",
	};

	const show_check = (result) => {
		if (result.skipped) {
			frappe.msgprint({ title: __("Not e-invoiced"), message: result.skipped, indicator: "blue" });
			return;
		}
		if (result.valid) {
			frappe.msgprint({ title: __("Ready to e-invoice"), message: __("Flick accepts this invoice as it stands."), indicator: "green" });
			return;
		}
		const items = (result.problems || []).map((p) => `<li>${frappe.utils.escape_html(p)}</li>`).join("");
		frappe.msgprint({
			title: result.valid === null ? __("Flick could not check it") : __("Not ready to e-invoice"),
			message: `<p>${__("Checked by {0}:", [result.checked_by])}</p><ul>${items}</ul>`,
			indicator: result.valid === null ? "orange" : "red",
		});
	};

	// What e-invoicing will do with the invoice, or did, and what it still lacks: drafts as last
	// saved, checked by this app only (Check with Flick asks Flick too).
	const show_banner = (frm) => {
		const $messages = $(frm.layout.message);
		$messages.find(".flick-banner").remove();
		if (frm.is_new()) return;
		const { doctype, docname } = frm;
		frappe.xcall("flick_einvoicing.api.invoice_banner", { doctype, name: docname }).then((banner) => {
			if (!banner || frm.docname !== docname) return;
			const esc = frappe.utils.escape_html;
			const lines = (banner.lines || []).map((line) => `<li>${esc(line)}</li>`);
			if (banner.more) lines.push(`<li>${__("and {0} more", [banner.more])}</li>`);
			const $banner = $(`<div class="form-message ${banner.color} flick-banner">
				<div><b>${esc(banner.title)}</b>${banner.action ? ` <a href="#" class="flick-banner-action ml-1" style="text-decoration: underline">${esc(banner.action.label)}</a>` : ""}</div>
				${lines.length ? `<ul class="mb-0 mt-1 pl-4">${lines.join("")}</ul>` : ""}
			</div>`);
			$banner.find(".flick-banner-action").on("click", (event) => {
				event.preventDefault();
				frappe.set_route(...banner.action.route);
			});
			$messages.find(".flick-banner").remove();
			$messages.append($banner).removeClass("hidden");
		});
	};

	const handlers = {
		onload(frm) {
			// Cancelling follows the invoice's own e-invoicing rules, not a cancel-all of submissions.
			const ignored = frm.ignore_doctypes_on_cancel_all || [];
			if (!ignored.includes("E Invoice Submission")) {
				frm.ignore_doctypes_on_cancel_all = ignored.concat(["E Invoice Submission"]);
			}
		},
		refresh(frm) {
			show_banner(frm);
			if (frm.doc.docstatus === 0 && !frm.is_new()) {
				frm.add_custom_button(
					__("Check with Flick"),
					() =>
						frappe
							.call({
								method: "flick_einvoicing.api.check_invoice",
								args: { doctype: frm.doctype, name: frm.doc.name },
								freeze: true,
								freeze_message: __("Validating with Flick..."),
							})
							.then(({ message }) => show_check(message)),
					__("E-Invoice")
				);
			}
			if (!frm.doc.einvoice_submission) return;
			frm.dashboard.add_indicator(
				__("E-Invoice: {0}", [__(frm.doc.einvoice_status || "")]),
				colors[frm.doc.einvoice_status] || "gray"
			);
			frm.add_custom_button(
				__("Submission"),
				() => frappe.set_route("Form", "E Invoice Submission", frm.doc.einvoice_submission),
				__("E-Invoice")
			);
			if (["Processing", "Completed", "Failed"].includes(frm.doc.einvoice_status)) {
				frm.add_custom_button(
					__("Download PDF"),
					() => {
						window.open(
							`/api/method/flick_einvoicing.api.download?name=${encodeURIComponent(frm.doc.einvoice_submission)}&kind=pdf`
						);
					},
					__("E-Invoice")
				);
			}
		},
	};

	frappe.ui.form.on("Sales Invoice", handlers);
	frappe.ui.form.on("POS Invoice", handlers);
	frappe.ui.form.on("Purchase Invoice", handlers);

	// The tax template is what tells the e-invoice each line's VAT treatment, unless every line
	// carries its own Item Tax Template. Opening invoices are never e-invoiced.
	const enabled = {};
	const mark_template_required = (frm) => {
		const company = frm.doc.company;
		if (!company || !frm.fields_dict.taxes_and_charges) return;
		const apply = () => {
			const items = frm.doc.items || [];
			const lines_decide = items.length > 0 && items.every((row) => row.item_tax_template);
			frm.toggle_reqd(
				"taxes_and_charges",
				Boolean(enabled[company]) && frm.doc.is_opening !== "Yes" && !lines_decide
			);
		};
		if (company in enabled) {
			apply();
			return;
		}
		frappe.xcall("flick_einvoicing.api.einvoicing_enabled", { company }).then((on) => {
			enabled[company] = on;
			apply();
		});
	};
	const template_handlers = {
		refresh: mark_template_required,
		company: mark_template_required,
		is_opening: mark_template_required,
	};
	const line_handlers = {
		item_tax_template: mark_template_required,
		items_add: mark_template_required,
		items_remove: mark_template_required,
	};
	frappe.ui.form.on("Sales Invoice", template_handlers);
	frappe.ui.form.on("POS Invoice", template_handlers);
	frappe.ui.form.on("Sales Invoice Item", line_handlers);
	frappe.ui.form.on("POS Invoice Item", line_handlers);
}

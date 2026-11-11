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
			frappe.msgprint({ title: __("E-invoicing not applicable"), message: result.skipped, indicator: "blue" });
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

	// Ours, then Flick's full validation, on the invoice as saved. Nothing is filed.
	const check_with_flick = (frm) => {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the invoice first: the check runs on the invoice as saved."));
			return;
		}
		frappe
			.call({
				method: "flick_einvoicing.api.check_invoice",
				args: { doctype: frm.doctype, name: frm.doc.name },
				freeze: true,
				freeze_message: __("Validating with Flick..."),
			})
			.then(({ message }) => show_check(message));
	};

	// The help (/einvoicing-help), opened in a new tab at one of its sections.
	const HELP_SECTIONS = { receiving: "how-received-invoices-arrive" };
	flick_einvoicing.open_guide = (section) => {
		const anchor = HELP_SECTIONS[section] || section || "";
		window.open(`/einvoicing-help${anchor ? "#" + anchor : ""}`, "_blank", "noopener");
	};

	const run_action = (frm, action) => {
		if (action.check) {
			check_with_flick(frm);
		} else if (action.guide) {
			flick_einvoicing.open_guide(action.guide);
		} else if (action.method) {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the invoice first."));
				return;
			}
			frappe
				.call({ method: action.method, args: { name: frm.doc.name }, freeze: true })
				.then(() => frm.reload_doc());
		} else {
			frappe.set_route(...action.route);
		}
	};

	// Whether the banner's details are open: a per-browser choice, closed when storage is blocked.
	const OPEN_KEY = "flick_einvoicing.banner_open";
	const details_open = () => {
		try {
			return localStorage.getItem(OPEN_KEY) === "1";
		} catch (e) {
			return false;
		}
	};
	const keep_details_open = (open) => {
		try {
			localStorage.setItem(OPEN_KEY, open ? "1" : "0");
		} catch (e) {
			// private window or blocked storage: the banner just starts closed next time
		}
	};

	// What e-invoicing will do with the invoice, or did, and what it still lacks: drafts as last
	// saved, checked by this app only (Check with Flick asks Flick too). One line; the details
	// open under it on request and show on hover while closed.
	const show_banner = (frm) => {
		const $messages = $(frm.layout.message);
		$messages.find(".flick-banner").remove();
		if (frm.is_new()) return;
		const { doctype, docname } = frm;
		frappe.xcall("flick_einvoicing.api.invoice_banner", { doctype, name: docname }).then((banner) => {
			if (!banner || frm.docname !== docname) return;
			const esc = frappe.utils.escape_html;
			const details = [...(banner.lines || [])];
			if (banner.more) details.push(__("and {0} more", [banner.more]));
			const open = details.length > 0 && details_open();
			const actions = banner.actions || (banner.action ? [banner.action] : []);
			const $banner = $(`<div class="form-message ${banner.color} flick-banner" style="padding: 6px 12px">
				<div class="flick-banner-line d-flex align-items-center" style="gap: 12px; min-width: 0">
					<b class="ellipsis">${esc(banner.title)}</b>
					${actions
						.map(
							(action, index) =>
								`<a href="#" class="flick-banner-action text-nowrap" data-index="${index}" style="text-decoration: underline">${esc(action.label)}</a>`
						)
						.join("")}
					${details.length ? `<a href="#" class="flick-banner-toggle text-nowrap ml-auto" role="button"></a>` : ""}
				</div>
				${details.length ? `<ul class="flick-banner-details mb-0 mt-1 pl-4">${details.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
			</div>`);
			const set_open = (value) => {
				$banner.find(".flick-banner-details").prop("hidden", !value);
				$banner
					.find(".flick-banner-toggle")
					.attr("aria-expanded", String(value))
					.text(value ? `${__("Hide details")} ▴` : `${__("Details")} ▾`);
				$banner.find(".flick-banner-line").attr("title", value ? null : details.join("\n"));
			};
			set_open(open);
			$banner.find(".flick-banner-toggle").on("click", (event) => {
				event.preventDefault();
				const value = $banner.find(".flick-banner-details").prop("hidden");
				set_open(value);
				keep_details_open(value);
			});
			$banner.find(".flick-banner-action").on("click", (event) => {
				event.preventDefault();
				run_action(frm, actions[$(event.currentTarget).data("index")]);
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
				frm.add_custom_button(__("Check with Flick"), () => check_with_flick(frm), __("E-Invoice"));
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

	// A buyer abroad whose supply goes outside the UAE: tick Export, as the server does on insert.
	// The shipping address decides; without one, the billing address abroad becomes it. Never unticks.
	const default_export = (frm) => {
		const { customer, shipping_address_name, customer_address } = frm.doc;
		const address = shipping_address_name || customer_address;
		if (frm.doc.docstatus !== 0 || frm.doc.einvoice_is_export || !customer || !address) return;
		Promise.all([
			frappe.db.get_value("Customer", customer, "einvoice_routing"),
			frappe.db.get_value("Address", address, "country"),
		]).then(([routing, country]) => {
			const where = country.message.country;
			const still = (frm.doc.shipping_address_name || frm.doc.customer_address) === address;
			if (routing.message.einvoice_routing !== "Export buyer not on Peppol" || !where || where === "United Arab Emirates" || !still) return;
			if (!frm.doc.shipping_address_name) frm.set_value("shipping_address_name", address);
			frm.set_value("einvoice_is_export", 1);
			frappe.show_alert({ message: __("Export ticked: the buyer is abroad, in {0}.", [where]), indicator: "blue" });
		});
	};
	frappe.ui.form.on("Sales Invoice", { customer: default_export, customer_address: default_export, shipping_address_name: default_export });
	// A customer marked as a free zone company: tick Free Trade Zone Supply with its TRN as the
	// beneficiary, as the server does on insert. Never unticks.
	const default_free_zone = (frm) => {
		const { customer } = frm.doc;
		if (frm.doc.docstatus !== 0 || frm.doc.einvoice_is_ftz || !customer) return;
		frappe.db.get_value("Customer", customer, ["einvoice_free_zone", "tax_id"]).then(({ message: c }) => {
			if (!c || !c.einvoice_free_zone || frm.doc.customer !== customer) return;
			frm.set_value("einvoice_is_ftz", 1);
			const trn = (c.tax_id || "").trim();
			if (!frm.doc.einvoice_fz_beneficiary_id && trn && !["NA", "N/A"].includes(trn.toUpperCase())) frm.set_value("einvoice_fz_beneficiary_id", trn);
			frappe.show_alert({ message: __("Free Trade Zone Supply ticked: {0} is a free zone company.", [customer]), indicator: "blue" });
		});
	};
	frappe.ui.form.on("Sales Invoice", { customer: default_free_zone });
	frappe.ui.form.on("POS Invoice", { customer: default_free_zone });
	frappe.ui.form.on("POS Invoice", handlers);
	frappe.ui.form.on("Purchase Invoice", handlers);

	// On a draft made from a received e-invoice, choosing our item for a line that waits on a
	// placeholder keeps the supplier's price: ERPNext would put in our item's own price, or none.
	frappe.ui.form.on("Purchase Invoice", {
		refresh(frm) {
			frm.flick_prices = null;
			if (frm.doc.docstatus !== 0 || !frm.doc.einvoice_received || frm.is_new()) return;
			const { name } = frm.doc;
			frappe.xcall("flick_einvoicing.api.supplier_prices", { name }).then((info) => {
				if (frm.doc.name !== name) return;
				const items = {};
				(frm.doc.items || []).forEach((row) => (items[row.name] = row.item_code));
				frm.flick_prices = { ...info, items };
			});
		},
	});
	// ERPNext writes the fetched item's details onto the row, then fires price_list_rate, whose
	// own handler (run after ours) puts our item's price in the rate: put the supplier's back after it.
	const KEEP_FOR = 10000;
	frappe.ui.form.on("Purchase Invoice Item", {
		item_code(frm, cdt, cdn) {
			const info = frm.flick_prices;
			const row = locals[cdt][cdn];
			if (!info || !row.einvoice_line_no) return;
			const before = info.items[cdn];
			info.items[cdn] = row.item_code;
			const price = info.prices[String(row.einvoice_line_no)];
			if (price !== undefined && info.placeholders.includes(before) && !info.placeholders.includes(row.item_code)) {
				row.__flick_price = { price, until: Date.now() + KEEP_FOR };
			}
		},
		price_list_rate(frm, cdt, cdn) {
			const row = locals[cdt][cdn];
			const keep = row.__flick_price;
			if (!keep) return;
			delete row.__flick_price;
			if (Date.now() > keep.until) return;
			setTimeout(() => {
				const decimals = precision("rate", row);
				if (row.item_code && flt(row.rate, decimals) !== flt(keep.price, decimals)) {
					frappe.model.set_value(cdt, cdn, "rate", keep.price);
				}
			}, 100);
		},
	});

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

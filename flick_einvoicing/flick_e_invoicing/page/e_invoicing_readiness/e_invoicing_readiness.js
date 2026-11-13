// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

// Which customers and suppliers are ready for e-invoicing, and the customer data collection form:
// a link per customer, or one email to many after previewing and editing the template.
frappe.pages["e-invoicing-readiness"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("E-Invoicing Readiness"), single_column: true });
	new Readiness(page);
};

class Readiness {
	constructor(page) {
		this.page = page;
		this.rows = [];
		this.selected = new Set();
		this.make_filters();
		this.send_btn = page.set_primary_action(__("Send Data Form"), () => this.open_email(), "mail");
		page.set_secondary_action(__("Refresh"), () => this.load(), "refresh");
		page.add_menu_item(__("Customer Data Form Settings"), () => frappe.set_route("Form", "Customer Data Form"));
		this.$body = $(`<div class="flick-readiness"></div>`).appendTo(page.main);
		this.style();
		this.bind();
		this.load();
	}

	make_filters() {
		const p = this.page;
		this.f = {
			party_type: p.add_field({ fieldname: "party_type", label: __("Party Type"), fieldtype: "Select", options: "Customer\nSupplier", default: "Customer",
				change: () => { this.selected.clear(); this.toggle_party_filters(); this.load(); } }),
			customer_group: p.add_field({ fieldname: "customer_group", label: __("Customer Group"), fieldtype: "Link", options: "Customer Group", change: () => this.load() }),
			supplier_group: p.add_field({ fieldname: "supplier_group", label: __("Supplier Group"), fieldtype: "Link", options: "Supplier Group", change: () => this.load() }),
			only_incomplete: p.add_field({ fieldname: "only_incomplete", label: __("Only Incomplete"), fieldtype: "Check", change: () => this.load() }),
			search: p.add_field({ fieldname: "search", label: __("Search"), fieldtype: "Data", change: () => this.render() }),
		};
		this.toggle_party_filters();
	}

	get party_type() { return this.f.party_type.get_value() || "Customer"; }

	toggle_party_filters() {
		const customer = this.party_type === "Customer";
		this.f.customer_group.$wrapper.toggle(customer);
		this.f.supplier_group.$wrapper.toggle(!customer);
		this.send_btn && this.send_btn.toggle(customer);
	}

	load() {
		const filters = {
			customer_group: this.f.customer_group.get_value(),
			supplier_group: this.f.supplier_group.get_value(),
			only_incomplete: this.f.only_incomplete.get_value() ? 1 : 0,
		};
		this.$body.html(`<div class="text-muted p-4">${__("Loading…")}</div>`);
		frappe.call({ method: "flick_einvoicing.readiness.get_rows", args: { party_type: this.party_type, filters } }).then(({ message }) => {
			this.rows = message || [];
			const names = new Set(this.rows.map((r) => r.party));
			[...this.selected].forEach((s) => names.has(s) || this.selected.delete(s));
			this.render();
		});
	}

	visible() {
		const q = (this.f.search.get_value() || "").toLowerCase();
		return q ? this.rows.filter((r) => `${r.party} ${r.party_name} ${r.contact_email || ""}`.toLowerCase().includes(q)) : this.rows;
	}

	render() {
		const customer = this.party_type === "Customer";
		const rows = this.visible();
		const esc = frappe.utils.escape_html;
		const ready = this.rows.filter((r) => r.status === "Ready").length;
		const pill = (text, color) => (text ? `<span class="indicator-pill ${color}">${esc(text)}</span>` : "");
		const form_color = { Pending: "blue", Submitted: "orange", Applied: "green", Cancelled: "gray" };
		const all = rows.length && rows.every((r) => this.selected.has(r.party));

		const body = rows.map((r) => {
			const link = `<a href="/app/${frappe.router.slug(this.party_type)}/${encodeURIComponent(r.party)}">${esc(r.party_name || r.party)}</a>`;
			const cells = [
				customer ? `<td><input type="checkbox" data-pick="${esc(r.party)}" ${this.selected.has(r.party) ? "checked" : ""}></td>` : "",
				`<td>${link}${r.party_name && r.party_name !== r.party ? `<div class="text-muted small">${esc(r.party)}</div>` : ""}</td>`,
				`<td>${pill(__(r.status), r.status === "Ready" ? "green" : "orange")}</td>`,
				`<td class="small">${esc(r.missing || "")}</td>`,
				`<td class="small">${esc(r.peppol_id || "")}</td>`,
				`<td class="small">${esc(r.tax_id || "")}</td>`,
			];
			if (customer) {
				cells.push(
					`<td class="small"><span class="flick-email" data-email="${esc(r.party)}" title="${__("Click to edit")}">${
						r.contact_email ? esc(r.contact_email) : `<span class="text-muted">+ ${__("Add")}</span>`
					}</span>${
						!r.contact_email && r.send_to
							? `<div class="text-muted small" title="${__("Used when no e-invoicing contact email is set")}">${esc(r.send_to)} · ${
									r.send_to_source === "primary" ? __("primary contact") : __("contact")
								}</div>`
							: ""
					}</td>`,
					`<td>${r.form_request ? `<a href="/app/customer-data-request/${encodeURIComponent(r.form_request)}">${pill(__(r.form_status), form_color[r.form_status] || "gray")}</a>` : ""}
						${r.sent_on ? `<div class="text-muted small">${__("Emailed {0}", [frappe.datetime.prettyDate(r.sent_on)])}${r.send_count > 1 ? ` ×${r.send_count}` : ""}</div>` : ""}</td>`,
					`<td><button class="btn btn-xs btn-default" data-link="${esc(r.party)}">${__("Generate Link")}</button></td>`
				);
			} else {
				cells.push(`<td>${r.self_billing ? __("Yes") : ""}</td>`);
			}
			return `<tr>${cells.join("")}</tr>`;
		}).join("");

		const head = [
			customer ? `<th style="width:28px"><input type="checkbox" data-pick-all ${all ? "checked" : ""}></th>` : "",
			`<th>${__(this.party_type)}</th>`, `<th>${__("Status")}</th>`, `<th>${__("Missing / To Check")}</th>`,
			`<th>${__("Peppol ID")}</th>`, `<th>${__("TRN")}</th>`,
			...(customer ? [`<th>${__("Contact Email")}</th>`, `<th>${__("Data Form")}</th>`, `<th></th>`] : [`<th>${__("Self-Billing")}</th>`]),
		].join("");

		this.$body.html(`
			<div class="flick-summary">
				<span><b>${this.rows.length}</b> ${__("parties")}</span>
				<span class="text-success"><b>${ready}</b> ${__("ready")}</span>
				<span class="text-warning"><b>${this.rows.length - ready}</b> ${__("incomplete")}</span>
				${customer ? `<span class="ml-auto"><b>${this.selected.size}</b> ${__("selected")}</span>` : ""}
			</div>
			<div class="flick-table-wrap"><table class="table table-sm flick-table"><thead><tr>${head}</tr></thead>
			<tbody>${body || `<tr><td colspan="10" class="text-muted text-center p-4">${__("Nothing to show.")}</td></tr>`}</tbody></table></div>`);
		this.send_btn.text(this.selected.size ? __("Send Data Form ({0})", [this.selected.size]) : __("Send Data Form"));
	}

	bind() {
		this.$body.on("change", "[data-pick]", (e) => {
			const name = e.currentTarget.dataset.pick;
			e.currentTarget.checked ? this.selected.add(name) : this.selected.delete(name);
			this.render();
		});
		this.$body.on("change", "[data-pick-all]", (e) => {
			this.visible().forEach((r) => (e.currentTarget.checked ? this.selected.add(r.party) : this.selected.delete(r.party)));
			this.render();
		});
		this.$body.on("click", "[data-email]", (e) => this.edit_email($(e.currentTarget), e.currentTarget.dataset.email));
		this.$body.on("click", "[data-link]", (e) => {
			const customer = e.currentTarget.dataset.link;
			frappe.call({ method: "flick_einvoicing.data_form.create_request", args: { customer }, freeze: true }).then(({ message: r }) => {
				frappe.utils.copy_to_clipboard(r.url);
				frappe.msgprint({
					title: __("Link for {0}", [frappe.utils.escape_html(customer)]),
					message: `<p>${__("Copied to clipboard. Works once; earlier unused links stop working.")}</p>
						<input class="form-control" readonly value="${frappe.utils.escape_html(r.url)}" onclick="this.select()">`,
				});
				this.load();
			});
		});
	}

	// Click an email cell to edit it in place; Enter or leaving the field saves, Escape cancels.
	edit_email($cell, customer, done) {
		const row = this.rows.find((r) => r.party === customer) || {};
		const $input = $(`<input type="email" class="form-control form-control-sm" placeholder="name@company.com">`)
			.val(row.contact_email || "");
		$cell.replaceWith($input);
		$input.trigger("focus");
		let finished = false;
		const finish = (save) => {
			if (finished) return;
			finished = true;
			const email = ($input.val() || "").trim();
			if (!save || email === (row.contact_email || "")) return done ? done(false) : this.render();
			frappe
				.call({ method: "flick_einvoicing.data_form.set_contact_email", args: { customer, email } })
				.then(({ message }) => {
					row.contact_email = message;
					frappe.show_alert({ message: __("Contact email saved for {0}", [customer]), indicator: "green" });
					done ? done(true) : this.render();
				})
				.catch(() => (done ? done(false) : this.render()));
		};
		$input.on("keydown", (e) => {
			if (e.key === "Enter") finish(true);
			if (e.key === "Escape") finish(false);
		});
		$input.on("blur", () => finish(true));
	}

	open_email() {
		const customers = [...this.selected];
		if (!customers.length) {
			frappe.msgprint(__("Tick the customers to email first."));
			return;
		}
		frappe.call("flick_einvoicing.data_form.get_email_template").then(({ message: t }) => {
			const d = new frappe.ui.Dialog({
				title: __("Send Data Collection Form to {0} customer(s)", [customers.length]),
				size: "extra-large",
				fields: [
					{ fieldtype: "Data", fieldname: "subject", label: __("Subject"), reqd: 1, default: t.subject },
					{ fieldtype: "Text Editor", fieldname: "body", label: __("Body"), reqd: 1, default: t.body,
						description: __("Placeholders: {0}", ["{{ customer_name }}, {{ company }}, {{ link }}, {{ expires_on }}"]) },
					{ fieldtype: "Check", fieldname: "save_template", label: __("Save as default template"), default: 1 },
					{ fieldtype: "Section Break", label: __("Preview") },
					{ fieldtype: "HTML", fieldname: "preview" },
					{ fieldtype: "Column Break" },
					{ fieldtype: "HTML", fieldname: "recipients" },
				],
				secondary_action_label: __("Preview"),
				secondary_action: () => this.preview(d, customers),
				primary_action_label: __("Confirm & Send"),
				primary_action: (v) => {
					if (!d.previewed) {
						this.preview(d, customers).then(() => frappe.show_alert(__("Check the preview, then click Confirm & Send again.")));
						return;
					}
					frappe.confirm(__("Email the data collection form to {0} customer(s)?", [d.sendable]), () =>
						frappe.call({
							method: "flick_einvoicing.data_form.send_bulk",
							args: { customers, subject: v.subject, body: v.body, save_template: v.save_template },
							freeze: true,
						}).then(({ message: r }) => {
							d.hide();
							let msg = __("{0} email(s) queued. You will get a message when they are on their way.", [r.queued]);
							if (r.skipped.length) msg += `<br><br>${__("Skipped, no email address:")} ${r.skipped.map(frappe.utils.escape_html).join(", ")}`;
							frappe.msgprint({ title: __("Data Collection"), message: msg, indicator: "green" });
							this.selected.clear();
							setTimeout(() => this.load(), 3000);
						})
					);
				},
			});
			d.add_custom_action(__("Send Test"), () => this.send_test(d, customers), "btn-default");
			// Editing the template means the old preview no longer shows what will be sent.
			["subject", "body"].forEach((f) => (d.fields_dict[f].df.onchange = () => (d.previewed = false)));
			d.show();
			this.preview(d, customers);
		});
	}

	// The email exactly as the first selected customer gets it, to an address of your choosing.
	send_test(d, customers) {
		const v = d.get_values(true);
		frappe.prompt(
			[{ fieldtype: "Data", options: "Email", fieldname: "email", label: __("Send test to"), reqd: 1, default: frappe.session.user_email }],
			({ email }) =>
				frappe
					.call({
						method: "flick_einvoicing.data_form.send_test_email",
						args: { email, customer: customers[0], subject: v.subject || "", body: v.body || "" },
						freeze: true,
					})
					.then(({ message }) => frappe.msgprint({ title: __("Test Email"), message, indicator: "blue" })),
			__("Send Test Email (as {0})", [customers[0]]),
			__("Send")
		);
	}

	preview(d, customers) {
		const v = d.get_values(true);
		const esc = frappe.utils.escape_html;
		return frappe.call({
			method: "flick_einvoicing.data_form.preview_email",
			args: { customers, subject: v.subject || "", body: v.body || "" },
		}).then(({ message: p }) => {
			const ok = p.recipients.filter((r) => r.email);
			const missing = p.recipients.filter((r) => !r.email);
			d.sendable = ok.length;
			d.previewed = true;
			d.fields_dict.preview.$wrapper.html(`
				<div class="text-muted small mb-1">${__("As {0} will see it (link is made when sent)", [esc(p.customer)])}</div>
				<div class="flick-mail"><div class="flick-mail-subject">${esc(p.subject)}</div><div class="flick-mail-body">${frappe.dom.remove_script_and_style(p.body)}</div></div>`);
			d.fields_dict.recipients.$wrapper.html(`
				${p.no_email_account ? `<div class="alert alert-danger small">${__("No default outgoing Email Account is set up, so nothing can be sent yet.")} <a href="/app/email-account">${__("Email Accounts")}</a></div>` : ""}
				<div class="small"><b>${__("Will be sent to {0}", [ok.length])}</b>
				<ul class="flick-rcpt">${ok.map((r) => `<li>${esc(r.customer)} <span class="text-muted">&lt;${esc(r.email)}&gt;${r.source === "einvoice" ? "" : ` · ${r.source === "primary" ? __("primary contact") : __("contact")}`}</span></li>`).join("")}</ul>
				${missing.length ? `<b class="text-danger">${__("Skipped, no email ({0})", [missing.length])}</b>
				<ul class="flick-rcpt">${missing.map((r) => `<li>${esc(r.customer)} <a class="flick-add-email" data-email="${esc(r.customer)}">+ ${__("Add email")}</a></li>`).join("")}</ul>
				<div class="text-muted">${__("Add an email here, or set a primary contact on the customer.")}</div>` : ""}</div>`);
			d.fields_dict.recipients.$wrapper.find("[data-email]").on("click", (e) =>
				this.edit_email($(e.currentTarget), e.currentTarget.dataset.email, (saved) => {
					this.render();
					this.preview(d, customers);
				})
			);
			d.get_primary_btn().text(__("Confirm & Send ({0})", [ok.length])).prop("disabled", !ok.length || p.no_email_account);
		});
	}

	style() {
		if (document.getElementById("flick-readiness-style")) return;
		$(`<style id="flick-readiness-style">
			.flick-summary { display:flex; gap:18px; padding:12px 4px; flex-wrap:wrap; }
			.flick-table-wrap { overflow-x:auto; }
			.flick-table th { white-space:nowrap; font-size:var(--text-sm); color:var(--text-muted); }
			.flick-table td { vertical-align:middle; }
			.flick-mail { border:1px solid var(--border-color); border-radius:8px; max-height:420px; overflow:auto; }
			.flick-mail-subject { padding:10px 14px; border-bottom:1px solid var(--border-color); font-weight:600; }
			.flick-mail-body { padding:14px; }
			.flick-email { cursor:pointer; border-bottom:1px dashed var(--border-color); }
			.flick-email:hover, .flick-add-email { color:var(--primary); cursor:pointer; }
			.flick-rcpt { max-height:340px; overflow:auto; padding-left:18px; }
		</style>`).appendTo("head");
	}
}

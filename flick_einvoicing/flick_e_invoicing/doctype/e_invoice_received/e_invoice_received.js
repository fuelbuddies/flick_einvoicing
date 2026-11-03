// Copyright (c) 2026, Techountant and contributors
// For license information, please see license.txt

frappe.ui.form.on("E Invoice Received", {
	refresh(frm) {
		show_review(frm);
		if (frm.doc.status === "New" && !frm.doc.supplier) {
			// Saving the supplier with the sender's Peppol ID or TRN links this invoice, and any others from it.
			frm.add_custom_button(__("Create Supplier"), () => create_supplier(frm));
		}
		if (frm.doc.status === "New") {
			frm.add_custom_button(__("Create Purchase Invoice"), () =>
				frappe
					.call({
						method: "flick_einvoicing.api.create_purchase_invoice",
						args: { name: frm.doc.name },
						freeze: true,
					})
					.then(({ message }) => frappe.set_route("Form", "Purchase Invoice", message))
			);
			frm.add_custom_button(__("Ignore"), () =>
				frappe.call({ method: "flick_einvoicing.api.ignore_received", args: { name: frm.doc.name } }).then(() => frm.reload_doc())
			);
		}
		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Purchase Invoice"), () => frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice), __("View"));
		}
		frm.add_custom_button(
			__("How drafts are made"),
			() => {
				frappe.flags.flick_guide_section = "receiving";
				frappe.set_route("Form", "E Invoicing Guide");
			},
			__("View")
		);
	},
});

function create_supplier(frm) {
	const supplier = frappe.model.get_new_doc("Supplier");
	Object.assign(supplier, {
		supplier_name: frm.doc.sender_name,
		tax_id: frm.doc.sender_vat,
		einvoice_peppol_id: frm.doc.sender_peppol_id,
	});
	if ((frm.doc.sender_peppol_id || "").startsWith("0235:")) {
		supplier.country = "United Arab Emirates";
	}
	frappe.set_route("Form", "Supplier", supplier.name);
}

// The checks Create Purchase Invoice would run into, and the supplier's lines in a table.
function show_review(frm) {
	const $messages = $(frm.layout.message);
	$messages.find(".flick-banner").remove();
	const lines_field = frm.get_field("lines_html");
	if (frm.is_new()) return;
	const { name } = frm.doc;
	frappe.xcall("flick_einvoicing.api.received_review", { name }).then((review) => {
		if (!review || frm.doc.name !== name) return;
		const esc = frappe.utils.escape_html;
		const { banner } = review;
		const actions = banner.actions || [];
		const $banner = $(`<div class="form-message ${banner.color} flick-banner" style="padding: 8px 12px">
			<div class="d-flex align-items-center flex-wrap" style="gap: 12px">
				<b>${esc(banner.title)}</b>
				${actions
					.map((a, i) => `<a href="#" class="flick-banner-action text-nowrap" data-index="${i}" style="text-decoration: underline">${esc(__(a.label))}</a>`)
					.join("")}
			</div>
			${(banner.lines || []).length ? `<ul class="mb-0 mt-1 pl-4">${banner.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
		</div>`);
		$banner.find(".flick-banner-action").on("click", (event) => {
			event.preventDefault();
			const action = actions[$(event.currentTarget).data("index")];
			if (action.create_supplier) create_supplier(frm);
			else if (action.route) frappe.set_route(...action.route);
		});
		$messages.append($banner).removeClass("hidden");

		const money = (value) => format_currency(value, review.currency);
		const rows = review.lines
			.map(
				(l) => `<tr>
				<td class="text-muted">${l.no}</td>
				<td>${esc(l.name)}${l.code ? `<div class="text-muted small">${__("Supplier code")}: ${esc(l.code)}</div>` : ""}</td>
				<td>${l.item ? `<a href="/app/item/${encodeURIComponent(l.item)}">${esc(l.item)}</a>` : `<span class="indicator-pill orange">${__("No match")}</span>`}</td>
				<td class="text-right">${format_number(l.qty)} ${esc(l.uom)}</td>
				<td class="text-right">${money(l.rate)}</td>
				<td class="text-right">${money(l.amount)}</td>
				<td>${esc(l.vat)}</td>
			</tr>`
			)
			.join("");
		const t = review.totals;
		const total_row = (label, value) =>
			value === null || value === undefined ? "" : `<tr><td colspan="5" class="text-right text-muted">${label}</td><td class="text-right"><b>${money(value)}</b></td><td></td></tr>`;
		lines_field.$wrapper.html(`<div class="table-responsive"><table class="table table-bordered table-sm" style="margin: 0">
			<thead><tr class="text-muted">
				<th style="width: 32px">#</th><th>${__("Supplier's line")}</th><th>${__("Our item")}</th>
				<th class="text-right">${__("Qty")}</th><th class="text-right">${__("Rate")}</th><th class="text-right">${__("Amount")}</th><th>${__("VAT")}</th>
			</tr></thead>
			<tbody>${rows || `<tr><td colspan="7" class="text-muted">${__("No lines")}</td></tr>`}</tbody>
			<tfoot>${total_row(__("Net"), t.net)}${total_row(__("VAT"), t.vat)}${total_row(__("Total"), t.total)}</tfoot>
		</table></div>`);
	});
}

// Copyright (c) 2026, Ashar Shamsudheen and contributors
// For license information, please see license.txt

frappe.ui.form.on("E Invoice Support Report", {
	refresh(frm) {
		const enabled = frappe.boot.flick_support;
		if (frm.is_new()) {
			if (!enabled) {
				frm.set_intro(__("Support is turned off. A System Manager can enable it in Flick Support Settings."), "orange");
				frm.disable_save();
			}
			frm.set_df_property("report_type", "options", ["Bug", "Feature Request"]);
			return;
		}
		frm.disable_save();
		render_messages(frm);
		if (!enabled) return;
		if (["Failed", "Not Sent"].includes(frm.doc.send_status)) {
			frm.add_custom_button(__("Send Again"), () =>
				frappe.xcall("flick_einvoicing.support.retry_report", { name: frm.doc.name }).then(() => frm.reload_doc())
			);
		}
		if (frm.doc.remote_ticket) {
			const closed = ["Resolved", "Closed"].includes(frm.doc.remote_status);
			frm.add_custom_button(closed ? __("Reopen") : __("Reply"), () => reply(frm, closed));
		}
	},
});

function reply(frm, reopen) {
	frappe.prompt(
		{ fieldname: "message", fieldtype: "Small Text", label: reopen ? __("What is still wrong?") : __("Your reply"), reqd: 1 },
		({ message }) =>
			frappe
				.xcall("flick_einvoicing.support.reply_to_report", { name: frm.doc.name, message, reopen: reopen ? 1 : 0 })
				.then(() => frm.reload_doc()),
		reopen ? __("Reopen") : __("Reply")
	);
}

function render_messages(frm) {
	const esc = frappe.utils.escape_html;
	const rows = (frm.doc.messages || []).map(
		(m) => `<div style="margin-bottom: var(--margin-md)">
			<div class="text-muted small">${esc(m.sender === "Client" ? m.sender_name || __("You") : m.sender_name || __("Support"))} · ${esc(frappe.datetime.str_to_user(m.sent_at) || "")}</div>
			<div style="white-space: pre-wrap">${esc(m.message)}</div>
		</div>`
	);
	frm.get_field("conversation").$wrapper.html(rows.join("") || `<div class="text-muted">${__("No replies yet.")}</div>`);
}

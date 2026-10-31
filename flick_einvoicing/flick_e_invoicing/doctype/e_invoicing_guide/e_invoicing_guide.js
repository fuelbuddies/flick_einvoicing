// Copyright (c) 2026, Ashar Shamsudheen and contributors
// For license information, please see license.txt

// The guide is rendered on the server with the site's own settings; links in it scroll to a section.
frappe.ui.form.on("E Invoicing Guide", {
	refresh(frm) {
		frm.disable_save();
		const $guide = frm.get_field("guide").$wrapper;
		$guide.html((frm.doc.__onload || {}).guide || "");
		const scroll_to = (section) => {
			const target = $guide.find(`#flick-guide-${section}`)[0];
			if (target) target.scrollIntoView({ block: "start" });
		};
		$guide.off("click.flick").on("click.flick", "[data-flick-section]", (event) => {
			event.preventDefault();
			scroll_to(event.currentTarget.dataset.flickSection);
		});
		const section = frappe.flags.flick_guide_section;
		frappe.flags.flick_guide_section = null;
		if (section) setTimeout(() => scroll_to(section), 200);
	},
});

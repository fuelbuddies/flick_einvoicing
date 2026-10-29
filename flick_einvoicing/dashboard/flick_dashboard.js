// The Flick workspace dashboard. Frappe runs this inside the Custom HTML Block's shadow root,
// passed in as root_element. Kept in the app and loaded into the block on every migrate.
const SUBMISSION = "E Invoice Submission";
const SETTINGS = "E Invoicing Settings";
const PILL = { Queued: "blue", Rejected: "red", Error: "orange", Cancelled: "gray", Processing: "yellow", Completed: "green", Failed: "red" };
const NEEDS_ATTENTION = ["Rejected", "Error", "Failed"];
const PERIODS = [
	[7, __("7 days")],
	[30, __("30 days")],
	[90, __("90 days")],
	[0, __("All time")],
];

const view = root_element.querySelector(".flick");
const $ = (selector) => view.querySelector(selector);
const state = { days: 30, company: "" };

// Elements from untrusted text: strings become text nodes, never markup.
function h(tag, props = {}, ...children) {
	const el = document.createElement(tag);
	for (const [key, value] of Object.entries(props)) {
		if (value === undefined || value === null || value === false) continue;
		if (key === "class") el.className = value;
		else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
		else el.setAttribute(key, value);
	}
	el.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
	return el;
}

const count = (value) => format_number(value || 0, null, 0);
// Rounded down, so 1999 of 2000 never shows as 100%.
const percent = (part, whole) => `${Math.floor((part / whole) * 1000) / 10}%`;
const ago = (value) => (value ? frappe.datetime.prettyDate(value) : __("never"));
const url = (doctype, name) => `/app/${doctype.toLowerCase().replace(/ /g, "-")}${name ? `/${encodeURIComponent(name)}` : ""}`;
const pill = (label, color) => h("span", { class: `indicator-pill ${color}` }, label);

// A real link (so it opens in a new tab), routed inside the desk on a plain click.
function link(label, href, go, cls = "action") {
	return h(
		"a",
		{
			class: cls,
			href,
			onclick: (event) => {
				if (event.button || event.metaKey || event.ctrlKey || event.shiftKey) return;
				event.preventDefault();
				go();
			},
		},
		label
	);
}

// Companies: where each UAE company stands, as of now.
function render_companies(rows) {
	$(".companies-title").textContent = __("Companies");
	if (!rows.length) {
		$(".companies").replaceChildren(h("p", { class: "empty" }, __("No company on this site has United Arab Emirates as its country.")));
		return;
	}
	$(".companies").replaceChildren(...rows.map(company_row));
}

function company_row(row) {
	const open = () => frappe.set_route("Form", SETTINGS, row.company);
	const head = h("div", { class: "company-head" }, h("span", { class: "company-name" }, row.company));
	const meta = h("div", { class: "company-meta" });
	if (!row.record) {
		head.append(pill(__("Not set up"), "gray"));
		meta.append(
			__("Each company that issues e-invoices needs its own E Invoicing Settings record."),
			" ",
			link(__("Set up"), url(SETTINGS, "new"), () => frappe.new_doc(SETTINGS, { company: row.company }))
		);
	} else if (!row.enabled) {
		const left = (row.steps || []).filter((step) => !step.done);
		head.append(pill(__("Disabled"), "gray"));
		if (left.length) meta.append(__("{0} of {1} setup steps left. Next: {2}", [left.length, row.steps.length, left[0].label]), " ");
		meta.append(link(__("Continue setup"), url(SETTINGS, row.company), open));
	} else {
		head.append(pill(__("Enabled"), "green"));
		if (row.updates_late) head.append(pill(__("Updates late"), "orange"));
		const updates = row.status_updates === "Webhook" ? __("status by webhook") : __("status read {0}", [ago(row.events_synced_at)]);
		const sending = row.submission_mode === "Batch" ? __("sent in batches") : __("sent on submit");
		meta.append([__(row.environment || ""), sending, updates, __("last sent {0}", [ago(row.last_sent)])].filter(Boolean).join(" · "));
	}
	const notes = [];
	if (row.updates_late) {
		notes.push(__("Status updates have not been read for over half an hour. Check that the scheduler is running on this site."));
	}
	notes.push(...(row.warnings || []));
	return h("div", { class: "company" }, head, meta, notes.map((note) => h("div", { class: "warning-note" }, note)));
}

// Needs attention: refused, stuck on the way, or failed on a track, whatever the date.
function render_attention({ count: total, rows }) {
	$(".attention-title").textContent = __("Needs attention");
	const show_all = () => frappe.set_route("List", SUBMISSION, { status: ["in", NEEDS_ATTENTION] });
	$(".attention-all").replaceChildren(total > rows.length ? link(__("View all {0}", [count(total)]), url(SUBMISSION), show_all) : "");
	if (!rows.length) {
		$(".attention").replaceChildren(h("p", { class: "empty" }, __("Nothing needs attention.")));
		return;
	}
	$(".attention").replaceChildren(
		...rows.map((row) =>
			link(
				[
					pill(__(row.status), PILL[row.status] || "gray"),
					h("span", { class: "row-title" }, [row.invoice, row.customer || row.supplier].filter(Boolean).join(" · ")),
					h("span", { class: "row-when" }, ago(row.modified)),
					h("span", { class: "row-note" }, row.next_action || ""),
				],
				url(SUBMISSION, row.name),
				() => frappe.set_route("Form", SUBMISSION, row.name),
				"row-link"
			)
		)
	);
}

// Filters: the period and, where several companies e-invoice, the company.
function render_filters(companies) {
	const periods = $(".periods");
	periods.setAttribute("aria-label", __("Period"));
	periods.replaceChildren(
		...PERIODS.map(([days, label]) =>
			h(
				"button",
				{
					type: "button",
					"aria-pressed": String(days === state.days),
					onclick: () => {
						state.days = days;
						load();
					},
				},
				label
			)
		)
	);
	const names = companies.filter((row) => row.record).map((row) => row.company);
	const select = $(".company-filter");
	select.setAttribute("aria-label", __("Company"));
	select.hidden = names.length < 2;
	select.replaceChildren(h("option", { value: "" }, __("All companies")), ...names.map((name) => h("option", { value: name }, name)));
	select.value = names.includes(state.company) ? state.company : "";
	select.onchange = () => {
		state.company = select.value;
		load();
	};
}

// The period: what was sent, what Flick did with it, and how far each track got.
function render_period(statuses, tracks) {
	const sum = (...names) => names.reduce((total, name) => total + (statuses[name] || 0), 0);
	const accepted = sum("Processing", "Completed", "Failed");
	const refused = sum("Rejected");
	const answered = accepted + refused;
	const total = Object.values(statuses).reduce((a, b) => a + b, 0);
	const span = state.days ? __("made in the last {0} days", [state.days]) : __("made so far");

	$(".figures").replaceChildren(
		h(
			"div",
			{ class: "panel hero" },
			h("div", { class: "figure-label" }, __("Accepted by Flick")),
			h("div", { class: "hero-value" }, answered ? percent(accepted, answered) : "–"),
			h(
				"div",
				{ class: "figure-note" },
				answered ? __("{0} of the {1} submissions Flick answered", [count(accepted), count(answered)]) : __("Flick has not answered a submission in this period.")
			)
		),
		tile(__("Submissions"), total, null, span),
		tile(__("Accepted"), accepted, "done", __("{0} completed", [count(statuses.Completed)])),
		tile(__("Refused"), refused, "failed", __("nothing was filed")),
		tile(__("Not sent yet"), sum("Queued", "Error"), "queued", __("{0} queued, {1} being retried", [count(statuses.Queued), count(statuses.Error)])),
		tile(__("Cancelled"), sum("Cancelled"), "quiet", __("before reaching Flick"))
	);
	$(".tracks").replaceChildren(
		track(__("FTA reporting"), __("Reported"), tracks.reporting, accepted),
		track(__("Delivery to the buyer over Peppol"), __("Delivered"), tracks.delivery, accepted)
	);
}

function tile(label, value, tone, note) {
	return h(
		"div",
		{ class: "panel" },
		h("div", { class: "figure-label" }, tone && h("span", { class: `dot tone-${tone}`, "aria-hidden": "true" }), label),
		h("div", { class: "tile-value" }, count(value)),
		h("div", { class: "figure-note" }, note)
	);
}

// One track of the accepted documents as a part-to-whole bar, with every count in the key.
function track(title, done_label, stages, accepted) {
	const running = stages.in_progress + stages.retrying;
	const parts = [
		{ tone: "done", label: done_label, value: stages.done },
		{ tone: "quiet", label: __("Not required"), value: stages.not_required },
		{
			tone: "progress",
			label: __("In progress"),
			value: running,
			note: stages.retrying ? __("{0} failed once, Flick retrying", [count(stages.retrying)]) : null,
		},
		{ tone: "failed", label: __("Failed"), value: stages.failed },
		{ tone: "cancelled", label: __("Cancelled"), value: stages.cancelled },
	].filter((part) => part.tone !== "cancelled" || part.value);
	const segments = accepted
		? parts
				.filter((part) => part.value)
				.map((part) =>
					h("span", {
						class: `segment tone-${part.tone}`,
						style: `flex-grow: ${part.value}`,
						tabindex: "0",
						"aria-label": `${part.label}: ${count(part.value)} (${percent(part.value, accepted)})`,
						"data-value": count(part.value),
						"data-label": part.label,
						"data-share": percent(part.value, accepted),
						onpointerenter: show_tip,
						onpointerleave: hide_tip,
						onfocus: show_tip,
						onblur: hide_tip,
					})
				)
		: [];
	return h(
		"section",
		{ class: "panel" },
		h(
			"div",
			{ class: "panel-head" },
			h("h4", { class: "block-title" }, title),
			h("span", { class: "panel-aside" }, accepted ? __("of {0} accepted", [count(accepted)]) : __("nothing accepted in this period"))
		),
		h("div", { class: "meter", role: "group", "aria-label": title }, segments),
		h(
			"ul",
			{ class: "key-list" },
			parts.map((part) =>
				h(
					"li",
					{},
					h("span", { class: `key tone-${part.tone}`, "aria-hidden": "true" }),
					part.label,
					h("b", {}, count(part.value)),
					part.note && h("span", { class: "key-note" }, part.note)
				)
			)
		)
	);
}

function show_tip(event) {
	const segment = event.currentTarget;
	const tip = $(".tip");
	tip.replaceChildren(h("b", {}, segment.dataset.value), " ", segment.dataset.label, h("span", { class: "tip-share" }, segment.dataset.share));
	tip.hidden = false;
	const box = segment.getBoundingClientRect();
	const frame = view.getBoundingClientRect();
	const half = tip.offsetWidth / 2;
	const centre = box.left - frame.left + box.width / 2;
	tip.style.left = `${Math.min(Math.max(centre, half), frame.width - half)}px`;
	tip.style.top = `${box.top - frame.top}px`;
}

function hide_tip() {
	$(".tip").hidden = true;
}

function render(data) {
	render_companies(data.companies);
	render_attention(data.attention);
	render_filters(data.companies);
	render_period(data.statuses, data.tracks);
}

function load() {
	view.classList.add("loading");
	return frappe
		.xcall("flick_einvoicing.api.dashboard", { days: state.days, company: state.company || null })
		.then(render)
		.catch(() => {}) // the desk has already shown the server's message; keep the last figures
		.finally(() => view.classList.remove("loading"));
}

// Fixed colours for the greys follow the desk's theme, light or dark.
const follow_theme = () => view.classList.toggle("dark", document.documentElement.getAttribute("data-theme") === "dark");
const theme_watch = new MutationObserver(() => (view.isConnected ? follow_theme() : theme_watch.disconnect()));
theme_watch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
follow_theme();

$(".loading-note").textContent = __("Loading...");
load();

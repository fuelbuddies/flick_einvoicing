app_name = "flick_einvoicing"
app_title = "Flick E-Invoicing"
app_publisher = "Ashar Shamsudheen"
app_description = "UAE e-invoicing for ERPNext through Flick (PINT-AE over Peppol)"
app_email = "azr.shamz@gmail.com"
app_license = "Proprietary"
required_apps = ["erpnext"]

# Includes in <head>
# ------------------

doctype_js = {
	"Sales Invoice": "public/js/invoice.js",
	"POS Invoice": "public/js/invoice.js",
	"Purchase Invoice": "public/js/invoice.js",
	"Customer": "public/js/party.js",
	"Supplier": "public/js/party.js",
}

# Installation
# ------------

after_install = "flick_einvoicing.install.after_install"
after_migrate = "flick_einvoicing.install.after_migrate"
before_uninstall = "flick_einvoicing.install.before_uninstall"

# Document Events
# ---------------

_invoice_events = {
	"before_submit": "flick_einvoicing.invoice.before_submit",
	"on_submit": "flick_einvoicing.invoice.on_submit",
	"before_cancel": "flick_einvoicing.invoice.before_cancel",
	"on_cancel": "flick_einvoicing.invoice.on_cancel",
}

doc_events = {
	"Sales Invoice": _invoice_events,
	"POS Invoice": _invoice_events,
	# Self-billed invoices: purchase invoices from suppliers with a self-billing agreement. Those
	# made from received e-invoices remember the supplier's item codes on submit.
	"Purchase Invoice": {
		**_invoice_events,
		"before_submit": [_invoice_events["before_submit"], "flick_einvoicing.einvoice.inbound.check_unmatched_lines"],
		"on_submit": [_invoice_events["on_submit"], "flick_einvoicing.einvoice.inbound.remember_item_codes"],
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		# Batch-mode submissions, plus retries of anything that failed to reach Flick.
		"*/10 * * * *": ["flick_einvoicing.jobs.send_queued"],
		# Status updates from Flick's events feed, and invoices received over Peppol.
		"*/5 * * * *": ["flick_einvoicing.jobs.sync_events"],
	},
	# Safety net for documents whose events never arrived.
	"daily": ["flick_einvoicing.jobs.refresh_stale"],
}

# Log Settings clears E Invoice Log rows older than this many days.
default_log_clearing_doctypes = {"E Invoice Log": 365}

export_python_type_annotations = True

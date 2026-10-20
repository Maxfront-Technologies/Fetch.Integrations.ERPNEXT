from . import __version__ as app_version

app_name = "fetch_einvoice"
app_title = "Fetch Einvoice"
app_publisher = "Maxfront"
app_description = "Send ERPNext Sales Invoices and payments to FETCH for NRS e-invoicing, and receive the IRN back"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = ""
app_license = "MIT"

required_apps = ["erpnext"]

doctype_js = {
	"Sales Invoice": "public/js/sales_invoice.js",
	"Payment Entry": "public/js/payment_entry.js",
}

after_install = "fetch_einvoice.install.setup"
after_migrate = "fetch_einvoice.install.setup"
# run-tests --app runs only this app's before_tests, so ERPNext's setup (company, fixtures) would never happen.
before_tests = "erpnext.setup.utils.before_tests"

doc_events = {
	"Sales Invoice": {
		"before_submit": "fetch_einvoice.events.validate_sales_invoice",
		"on_submit": "fetch_einvoice.events.queue_sales_invoice",
		"on_cancel": "fetch_einvoice.events.cancel_outbox_rows",
	},
	"Payment Entry": {
		"on_submit": "fetch_einvoice.events.queue_payment_entry",
		"on_cancel": "fetch_einvoice.events.cancel_outbox_rows",
	},
}

scheduler_events = {
	"all": ["fetch_einvoice.outbox.sweep"],
}

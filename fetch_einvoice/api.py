"""Whitelisted methods: FETCH's IRN callback, the resend and retry buttons, and the FIRS code sync."""
import json
import time

import frappe
import requests
from frappe import _
from frappe.utils import escape_html, flt

from fetch_einvoice import core, outbox

TRANSMITTED_EVENT = "invoice.transmitted"


class EnvironmentMismatch(frappe.ValidationError):
	http_status_code = 409


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_fetch_irn():
	"""FETCH's signed IRN callback (FETCH.API/docs/erp-irn-callback.md). Any 2xx stops FETCH retrying."""
	# The signature covers the exact bytes, and Frappe HTML-sanitises form_dict for guests.
	raw = frappe.request.get_data()
	settings = frappe.get_single(outbox.SETTINGS)
	secret = settings.get_password("callback_secret", raise_exception=False) or ""
	timestamp = frappe.get_request_header("X-Fetch-Timestamp")
	signature = frappe.get_request_header("X-Fetch-Signature")
	if not core.verify_signature(raw, timestamp, signature, secret, int(time.time())):
		raise frappe.AuthenticationError(_("The FETCH signature is missing, expired or wrong."))

	# Verified from here on. No frappe.set_user, because in v13 it rewrites the shared Guest session object.
	# Nothing below checks permissions: it writes through frappe.db, add_comment and log_error.
	event = json.loads(raw)
	if event.get("eventType") != TRANSMITTED_EVENT:
		frappe.log_error(raw.decode("utf-8", "replace"), "FETCH callback: unknown eventType")
		return
	if event.get("environment") != settings.environment:
		frappe.throw(
			_("This callback is for FETCH {0}, but FETCH Settings is set to {1}.").format(
				event.get("environment"), settings.environment),
			EnvironmentMismatch,
		)
	name = event.get("invoiceNumber")
	if not name or not frappe.db.exists("Sales Invoice", name):
		frappe.throw(_("Sales Invoice {0} not found.").format(name), frappe.DoesNotExistError)

	irn = event.get("irn")
	stored = frappe.db.get_value("Sales Invoice", name, "fetch_irn", for_update=True)
	if stored == irn:
		return
	if stored:
		frappe.log_error(
			"Sales Invoice {0} has IRN {1}; FETCH event {2} sent {3}.".format(name, stored, event.get("eventId"), irn),
			"FETCH callback: IRN mismatch",
		)
		return
	frappe.db.set_value("Sales Invoice", name, {
		"fetch_irn": irn,
		"fetch_invoice_id": event.get("invoiceId"),
		"fetch_status": "Transmitted",
	}, update_modified=False)
	frappe.get_doc("Sales Invoice", name).add_comment(
		"Info", _("FETCH transmitted this invoice to NRS. IRN: {0}").format(escape_html(irn or "")))
	frappe.enqueue("fetch_einvoice.outbox.wake", sales_invoice=name, enqueue_after_commit=True)


def _require(doctype, ptype, name):
	# frappe.has_permission(throw=True) formats its message from doc.doctype, so a name string turns a denial into a 500.
	if not frappe.has_permission(doctype, ptype, name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def resend_invoice(sales_invoice):
	_require("Sales Invoice", "submit", sales_invoice)
	name = frappe.db.get_value(outbox.OUTBOX, {"kind": "Invoice", "sales_invoice": sales_invoice}, "name")
	if not name or not outbox.retry(name):
		frappe.throw(_("{0} has no failed FETCH submission to resend.").format(sales_invoice))


@frappe.whitelist(methods=["POST"])
def retry(name):
	_require(outbox.OUTBOX, "write", name)
	if not outbox.retry(name):
		frappe.throw(_("FETCH Outbox {0} has not failed, so there is nothing to retry.").format(name))


@frappe.whitelist()
def payment_accepted_by_fetch(payment_entry):
	_require("Payment Entry", "read", payment_entry)
	return bool(frappe.db.exists(outbox.OUTBOX, {
		"reference_doctype": "Payment Entry",
		"reference_name": payment_entry,
		"status": "Accepted",
	}))


@frappe.whitelist(methods=["POST"])
def sync_firs_codes():
	frappe.only_for("System Manager")
	if not frappe.db.get_single_value(outbox.SETTINGS, "base_url"):
		frappe.throw(_("Set the Base URL in FETCH Settings first."))
	frappe.enqueue("fetch_einvoice.api.run_firs_code_sync", queue="long")


# (doctype, FETCH path, key of the code in each record, the record's other fields)
# Routes and shapes: FETCH.API Endpoints/Resources/* and Integrations/Firs/Models/ResourceDto.cs.
CODE_LISTS = (
	("FIRS Tax Category", "/api/resources/tax-categories", "code",
		lambda r: {"title": r.get("value"), "tax_rate": flt(str(r.get("percent") or "").rstrip("%"))}),
	("FIRS Quantity Code", "/api/invoice/resources/invoice-quantity-codes", "code",
		lambda r: {"title": r.get("name"), "description": r.get("description")}),
	("FIRS HSN Code", "/api/resources/products-codes", "hscode",
		lambda r: {"description": r.get("description"), "kind": "Product"}),
	("FIRS HSN Code", "/api/resources/services-codes", "code",
		lambda r: {"description": r.get("description") or r.get("name"), "kind": "Service"}),
)


def run_firs_code_sync():
	"""Background job: upsert FETCH's FIRS code lists. Running it again converges to the same records."""
	settings = frappe.get_single(outbox.SETTINGS)
	counts = []
	try:
		for doctype, path, code_key, to_fields in CODE_LISTS:
			records = _code_list(settings, path)
			for record in records:
				code = str(record.get(code_key) or "").strip()
				if code:
					_upsert(doctype, code, to_fields(record))
			frappe.db.commit()
			counts.append("{0}: {1}".format(_(doctype), len(records)))
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "FETCH FIRS code sync")
		_tell(_("FIRS code sync stopped. {0}").format(e))
		return
	_tell(_("FIRS codes synced. {0}").format(", ".join(counts)))


def _code_list(settings, path):
	response = requests.get(
		settings.base_url + path,
		params={"shouldPaginate": "false"},
		headers=outbox.headers(settings),
		timeout=60,
		allow_redirects=False,
	)
	if response.status_code == 401:
		frappe.throw(_(
			"FETCH rejected the API key for {0} (HTTP 401). FETCH does not accept API keys on its code lists yet. "
			"Until it does, enter FIRS codes by hand or with Data Import."
		).format(path))
	if response.status_code != 200:
		frappe.throw(_("FETCH returned HTTP {0} for {1}.").format(response.status_code, path))
	body = response.json()
	# Product and service codes come as a PagedList; shouldPaginate=false puts every code in its items.
	return (body.get("items") or []) if isinstance(body, dict) else body


def _upsert(doctype, code, fields):
	if frappe.db.exists(doctype, code):
		frappe.db.set_value(doctype, code, fields)
	else:
		frappe.get_doc(dict(fields, doctype=doctype, code=code)).insert(ignore_permissions=True)


def _tell(message):
	frappe.publish_realtime("msgprint", message, user=frappe.session.user)

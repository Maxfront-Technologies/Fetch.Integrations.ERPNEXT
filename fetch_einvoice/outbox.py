"""FETCH Outbox: one row per call to FETCH, moved through TRANSITIONS by send, sweep, wake, retry and cancel."""
import json
from collections import namedtuple
from decimal import Decimal

import frappe
import requests
from frappe import _
from frappe.utils import add_to_date, escape_html, getdate, now, now_datetime

from fetch_einvoice import core, gather

OUTBOX = "FETCH Outbox"
SETTINGS = "FETCH Settings"
TIMEOUT_SECONDS = 30
NOT_READY_MINUTES = 5
STALE_MINUTES = 10
RESPONSE_BODY_LIMIT = 10000

# Every status write goes through _transition, which allows only these moves.
TRANSITIONS = {
	# Failed when its payload cannot be built. Cancelled by its document, or when FETCH already counts the invoice as PAID.
	"Pending": ("Sending", "Failed", "Cancelled"),
	# Never Cancelled: once the POST is out, only FETCH's answer settles the row.
	"Sending": ("Accepted", "Failed", "Pending"),
	# Pending on a manual retry. Cancelled when its document is cancelled, so it stops blocking later payments.
	"Failed": ("Pending", "Cancelled"),
}

OUTCOME_STATUS = {
	core.Outcome.ACCEPTED: "Accepted",
	core.Outcome.RETRY: "Pending",
	core.Outcome.FAILED: "Failed",
}

# Sales Invoice fetch_status after an Invoice row's send. Never written over an IRN the callback already stored.
INVOICE_STATUS = {"Accepted": "Submitted", "Failed": "Failed"}


def create(**fields):
	"""Insert a Pending row and send it once the caller's transaction commits."""
	row = frappe.get_doc(dict(fields, doctype=OUTBOX, status="Pending")).insert(ignore_permissions=True)
	_enqueue_send(row.name)
	return row.name


def send(name):
	"""Claim one Pending row, POST it to FETCH outside any transaction, then record the outcome."""
	settings = frappe.get_single(SETTINGS)
	if not settings.enabled:
		return
	claimed = _claim(name)
	frappe.db.commit()
	if not claimed:
		return
	row, payload = claimed
	code, body = _post(settings, KINDS[row.kind].path, payload)
	_record(row, code, body)
	frappe.db.commit()


def sweep():
	"""Scheduler: put rows stuck in Sending back to Pending, then send every due Pending row."""
	if not frappe.db.get_single_value(SETTINGS, "enabled"):
		return
	cutoff = add_to_date(now_datetime(), minutes=-STALE_MINUTES)
	for name in frappe.get_all(OUTBOX, filters={"status": "Sending", "modified": ["<", cutoff]}, pluck="name"):
		_transition(name, "Pending", error=_("Reset after {0} minutes in Sending.").format(STALE_MINUTES))
	frappe.db.commit()
	due = frappe.db.sql_list(
		"""select name from `tabFETCH Outbox`
		where status = 'Pending' and (next_attempt_at is null or next_attempt_at <= %s)
		order by name""",
		now(),
	)
	for name in due:
		_enqueue_send(name)


def wake(sales_invoice):
	"""Send what the IRN of `sales_invoice` unblocked: its Pending payments, oldest first, and its credit notes."""
	rows = frappe.db.sql_list(
		"""select o.name from `tabFETCH Outbox` o
		left join `tabSales Invoice` si on si.name = o.sales_invoice
		where o.status = 'Pending' and (
			(o.kind = 'Payment' and o.sales_invoice = %(si)s)
			or (o.kind = 'Invoice' and si.return_against = %(si)s))
		order by o.name""",
		{"si": sales_invoice},
	)
	for name in rows:
		send(name)


def retry(name):
	"""Failed → Pending with a fresh attempt count. Returns False when the row is not Failed."""
	row = frappe.db.get_value(OUTBOX, name, ["kind", "sales_invoice", "response_code"], as_dict=True)
	if not row:
		return False
	fields = dict(attempts=0, next_attempt_at=None, error=None)
	# FETCH records nothing for a payment it refused with a 4xx, so the retry may recompute it. After timeouts
	# FETCH may have stored it, and only a byte-identical replay is accepted.
	if row.kind == "Payment" and 400 <= (row.response_code or 0) < 500:
		fields["payload"] = None
	if not _transition(name, "Pending", **fields):
		return False
	if row.kind == "Invoice":
		_set_invoice_status(row.sales_invoice, "Pending")
	_enqueue_send(name)
	return True


def cancel(name):
	"""Stop a row that has not reached FETCH. Returns False when its status cannot become Cancelled."""
	return _transition(name, "Cancelled", error=_("Cancelled in ERPNext before FETCH accepted it."))


def headers(settings):
	result = {"X-API-Key": settings.get_password("api_key", raise_exception=False) or ""}
	secret = settings.get_password("api_secret", raise_exception=False)
	if secret:
		result["X-API-Secret"] = secret
	return result


def _transition(name, to, **fields):
	"""Lock the row and move it to `to` if TRANSITIONS allows it, writing `fields` in the same update."""
	status = frappe.db.get_value(OUTBOX, name, "status", for_update=True)
	if to not in TRANSITIONS.get(status, ()):
		return False
	if fields.get("error"):
		fields["error"] = escape_html(fields["error"])
	fields["status"] = to
	frappe.db.set_value(OUTBOX, name, fields)
	return True


def _claim(name):
	"""Pending → Sending with attempts + 1 and the payload stored. Returns (row, payload), or None for no HTTP call."""
	row = frappe.db.get_value(OUTBOX, name, "*", as_dict=True, for_update=True)
	if not row or "Sending" not in TRANSITIONS.get(row.status, ()):
		return None
	if frappe.db.get_value(row.reference_doctype, row.reference_name, "docstatus") == 2:
		cancel(name)
		return None
	kind = KINDS[row.kind]
	waiting = kind.waiting_for(row)
	if waiting:
		# Not ready is not an attempt. The IRN callback wakes it sooner than this.
		frappe.db.set_value(OUTBOX, name, {
			"next_attempt_at": add_to_date(now_datetime(), minutes=NOT_READY_MINUTES),
			"error": escape_html(waiting),
		})
		return None
	row.attempts += 1
	try:
		payload = kind.build(row)
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "FETCH Outbox {0}".format(name))
		if _transition(name, "Failed", attempts=row.attempts, error=str(e) or repr(e)) and row.kind == "Invoice":
			_set_invoice_status(row.sales_invoice, "Failed")
		return None
	if payload is None:
		_transition(name, "Cancelled", error=_("FETCH already counts this invoice as PAID, so there is nothing to send."))
		return None
	_transition(name, "Sending", attempts=row.attempts, payload=json.dumps(payload))
	return row, payload


def _post(settings, path, payload):
	try:
		response = requests.post(
			settings.base_url + path,
			json=payload,
			headers=headers(settings),
			timeout=TIMEOUT_SECONDS,
			allow_redirects=False,
		)
	except requests.RequestException as e:
		return None, str(e)
	return response.status_code, response.text


def _record(row, code, body):
	outcome, message = core.classify_response(row.kind, code, body)
	to = OUTCOME_STATUS[outcome]
	fields = {
		# v13 makes Int columns NOT NULL, so 0 stands for "FETCH never answered".
		"response_code": code or 0,
		"response_body": (body or "")[:RESPONSE_BODY_LIMIT],
		"error": None if outcome is core.Outcome.ACCEPTED else message,
	}
	if outcome is core.Outcome.RETRY:
		delay = core.retry_delay_minutes(row.attempts)
		if delay is None:
			to = "Failed"
		else:
			fields["next_attempt_at"] = add_to_date(now_datetime(), minutes=delay)
	if _transition(row.name, to, **fields) and row.kind == "Invoice" and to in INVOICE_STATUS:
		_set_invoice_status(row.sales_invoice, INVOICE_STATUS[to])


def _set_invoice_status(sales_invoice, status):
	# Callers hold the Outbox row lock, and on_cancel locks the Sales Invoice before the Outbox row. Committing
	# first means no transaction ever waits for one while holding the other.
	frappe.db.commit()
	frappe.db.sql(
		"""update `tabSales Invoice` set fetch_status = %s
		where name = %s and ifnull(fetch_irn, '') = ''""",
		(status, sales_invoice),
	)


def _irn(sales_invoice):
	return frappe.db.get_value("Sales Invoice", sales_invoice, "fetch_irn")


def _invoice_waiting_for(row):
	original = frappe.db.get_value("Sales Invoice", row.sales_invoice, "return_against")
	# An original sent before this app was installed has no row; FETCH matches it by ERP number instead.
	if original and frappe.db.exists(OUTBOX, {"kind": "Invoice", "sales_invoice": original}) and not _irn(original):
		return _("Waiting for {0}, the invoice this return is against, to get its IRN…").format(original)
	return None


def _payment_waiting_for(row):
	if not _irn(row.sales_invoice):
		return _("Waiting for {0} to get its IRN…").format(row.sales_invoice)
	earlier = frappe.db.get_value(OUTBOX, {
		"kind": "Payment",
		"sales_invoice": row.sales_invoice,
		"status": ["in", ("Pending", "Sending", "Failed")],
		"name": ["<", row.name],
	}, "name", order_by="name asc")
	if earlier:
		return _("Waiting for the earlier payment {0}…").format(earlier)
	return None


def _invoice_payload(row):
	si = frappe.get_doc("Sales Invoice", row.sales_invoice)
	# Payments made after submit reach FETCH as payments, so the deposit is what was paid when it was submitted.
	si.outstanding_amount = row.amount
	return core.build_invoice_payload(gather.invoice_from_doc(si))


def _payment_payload(row):
	"""The frozen payload, or on first send the amount and status FETCH will accept. None when nothing is left to pay."""
	if row.payload:
		return json.loads(row.payload)
	entry = frappe.db.get_value(
		"Payment Entry", row.reference_name, ["posting_date", "paid_from_account_currency"], as_dict=True)
	invoice = frappe.db.get_value("Sales Invoice", row.sales_invoice, ["currency", "fetch_irn"], as_dict=True)
	if entry.paid_from_account_currency != invoice.currency:
		frappe.throw(_("Payment Entry {0} is in {1} but {2} is in {3}. FETCH only records payments in the invoice currency.").format(
			row.reference_name, entry.paid_from_account_currency, row.sales_invoice, invoice.currency))
	decision = core.next_payment(_remaining(row.sales_invoice), Decimal(str(row.amount)), bool(row.erp_settled))
	if decision is None:
		return None
	return core.build_payment_payload(invoice.fetch_irn, decision, row.payment_reference, getdate(entry.posting_date))


def _remaining(sales_invoice):
	"""FETCH's balance: its last outstandingBalance, else payable − deposit − credit notes − accepted payments."""
	accepted = frappe.get_all(
		OUTBOX, filters={"kind": "Payment", "sales_invoice": sales_invoice, "status": "Accepted"},
		fields=["payload", "response_body"], order_by="name desc")
	if accepted:
		balance = _json(accepted[0].response_body).get("outstandingBalance")
		if balance is not None:
			return Decimal(str(balance))
	invoice = _json(frappe.db.get_value(OUTBOX, {"kind": "Invoice", "sales_invoice": sales_invoice}, "payload"))
	credits = frappe.db.sql_list(
		"""select o.payload from `tabFETCH Outbox` o join `tabSales Invoice` si on si.name = o.sales_invoice
		where o.kind = 'Invoice' and o.status = 'Accepted' and si.return_against = %s""",
		sales_invoice,
	)
	return (
		core.payable_of(invoice) - core.deposit_of(invoice)
		- sum((core.payable_of(_json(payload)) for payload in credits), Decimal(0))
		- sum((Decimal(str(_json(row.payload)["amount"])) for row in accepted), Decimal(0))
	)


def _json(text):
	try:
		value = json.loads(text or "")
	except ValueError:
		return {}
	return value if isinstance(value, dict) else {}


def _enqueue_send(name):
	frappe.enqueue("fetch_einvoice.outbox.send", name=name, enqueue_after_commit=True)


Kind = namedtuple("Kind", "path waiting_for build")

KINDS = {
	"Invoice": Kind("/api/v1/invoices/transmit", _invoice_waiting_for, _invoice_payload),
	"Payment": Kind("/api/v1/invoices/add-payment", _payment_waiting_for, _payment_payload),
}

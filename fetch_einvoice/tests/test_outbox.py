"""Outbox integration tests: bench --site <site> run-tests --app fetch_einvoice. FETCH is mocked; nothing leaves the bench.

outbox.send commits, so the records these tests create stay on the test site.
"""
import json
import unittest
from unittest.mock import MagicMock, call, patch

import frappe
import requests
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from frappe.utils import add_to_date, get_datetime, now_datetime, nowdate

from fetch_einvoice import outbox

# Item before Customer: the Customer walk reaches Restaurant, whose test records need _Test Customer 1.
test_dependencies = ["Account", "Item", "Customer"]

BASE_URL = "https://fetch.test"
COMPANY = "_Test Company"
CUSTOMER = "_Test FETCH Customer"
ITEM = "_Test FETCH Item"
API_KEY = "test-api-key"
CALLBACK_SECRET = "test-callback-secret"
OUTBOX = outbox.OUTBOX


def configure(enabled):
	settings = frappe.get_single("FETCH Settings")
	settings.update({
		"enabled": enabled,
		"environment": "Sandbox",
		"base_url": BASE_URL,
		"company": COMPANY,
		"api_key": API_KEY,
		"callback_secret": CALLBACK_SECRET,
	})
	settings.save(ignore_permissions=True)
	frappe.db.commit()


def ensure_masters():
	"""A customer, item and address that pass core.invoice_problems. Returns (customer, address)."""
	for doctype, code, fields in (
		("FIRS Tax Category", "STANDARD_VAT", {"title": "Standard VAT", "tax_rate": 7.5}),
		("FIRS HSN Code", "8471.30", {"description": "Portable computers", "kind": "Product"}),
		("FIRS Quantity Code", "EA", {"title": "Each"}),
	):
		if not frappe.db.exists(doctype, code):
			frappe.get_doc(dict(fields, doctype=doctype, code=code)).insert()
	if not frappe.db.exists("Item", ITEM):
		frappe.get_doc({
			"doctype": "Item",
			"item_code": ITEM,
			"item_name": ITEM,
			"description": "Laptop",
			"item_group": "_Test Item Group",
			"stock_uom": "_Test UOM",
			"is_stock_item": 0,
			"firs_hsn_code": "8471.30",
			"firs_tax_category": "STANDARD_VAT",
			"firs_quantity_code": "EA",
		}).insert()
	customer = frappe.db.get_value("Customer", {"customer_name": CUSTOMER})
	if not customer:
		customer = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": CUSTOMER,
			"customer_type": "Company",
			"customer_group": "_Test Customer Group",
			"territory": "_Test Territory",
			"tax_id": "12345678-0001",
			"default_currency": "USD",
			"firs_business_description": "Computer retail",
			"accounts": [{"company": COMPANY, "account": "_Test Receivable USD - _TC"}],
		}).insert().name
	address = frappe.db.get_value(
		"Dynamic Link", {"parenttype": "Address", "link_doctype": "Customer", "link_name": customer}, "parent")
	if not address:
		address = frappe.get_doc({
			"doctype": "Address",
			"address_title": CUSTOMER,
			"address_type": "Billing",
			"address_line1": "1 Marina Road",
			"city": "Lagos",
			"country": "Nigeria",
			"links": [{"link_doctype": "Customer", "link_name": customer}],
		}).insert().name
	return customer, address


def make_invoice():
	"""Submit a USD invoice: 2 x 50.00 plus 7.5% VAT = 107.50. Callers patch frappe.enqueue."""
	customer, address = ensure_masters()
	si = create_sales_invoice(
		company=COMPANY,
		customer=customer,
		debit_to="_Test Receivable USD - _TC",
		currency="USD",
		conversion_rate=50,
		item=ITEM,
		qty=2,
		rate=50,
		do_not_save=1,
	)
	si.disable_rounded_total = 1
	si.customer_address = address
	si.contact_email = "buyer@example.com"
	si.contact_mobile = "0803 123 4567"
	si.append("taxes", {
		"charge_type": "On Net Total",
		"account_head": "_Test Account VAT - _TC",
		"cost_center": "_Test Cost Center - _TC",
		"description": "VAT",
		"rate": 7.5,
	})
	si.insert()
	si.submit()
	return si


def make_payment(si, amount):
	"""Submit a USD Payment Entry of `amount` against `si`. Callers patch frappe.enqueue."""
	pe = get_payment_entry("Sales Invoice", si.name, party_amount=amount, bank_account="_Test Bank USD - _TC", bank_amount=amount)
	# get_payment_entry puts party_amount in the row's outstanding, which v13 refuses once the invoice is partly paid.
	pe.references[0].outstanding_amount = frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")
	pe.reference_no = "1"
	pe.reference_date = nowdate()
	pe.source_exchange_rate = 50
	pe.insert()
	pe.submit()
	return pe


def reply(status, body=""):
	return MagicMock(status_code=status, text=body)


def row_for(kind, reference_name):
	return frappe.db.get_value(OUTBOX, {"kind": kind, "reference_name": reference_name})


def row_state(name):
	return frappe.db.get_value(OUTBOX, name, ["status", "attempts", "response_code", "next_attempt_at"], as_dict=True)


def fetch_status(si):
	return frappe.db.get_value("Sales Invoice", si.name, "fetch_status")


def send_call(name):
	return call("fetch_einvoice.outbox.send", name=name, enqueue_after_commit=True)


class FetchTestCase(unittest.TestCase):
	"""FETCH Settings enabled for the class; frappe.enqueue and requests.post mocked for every test."""

	@classmethod
	def setUpClass(cls):
		configure(enabled=1)

	@classmethod
	def tearDownClass(cls):
		configure(enabled=0)

	def setUp(self):
		for target, attribute in (("frappe.enqueue", "enqueue"), ("fetch_einvoice.outbox.requests.post", "post")):
			patcher = patch(target)
			setattr(self, attribute, patcher.start())
			self.addCleanup(patcher.stop)
		self.post.return_value = reply(202)


class TestOutbox(FetchTestCase):
	def test_accepted_invoice_marks_the_sales_invoice_submitted(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)
		self.assertIn(send_call(row), self.enqueue.call_args_list)
		self.assertEqual(fetch_status(si), "Pending")

		outbox.send(row)

		self.assertEqual(self.post.call_count, 1)
		args, kwargs = self.post.call_args
		self.assertEqual(args[0], BASE_URL + "/api/v1/invoices/transmit")
		self.assertEqual(kwargs["json"]["irn"], si.name)
		self.assertEqual(kwargs["json"]["legalMonetaryTotal"]["payableAmount"], 107.5)
		self.assertEqual(kwargs["headers"]["X-API-Key"], API_KEY)
		self.assertNotIn("X-API-Secret", kwargs["headers"])
		self.assertEqual(kwargs["timeout"], 30)
		self.assertIs(kwargs["allow_redirects"], False)
		state = row_state(row)
		self.assertEqual((state.status, state.attempts, state.response_code), ("Accepted", 1, 202))
		self.assertEqual(fetch_status(si), "Submitted")

	def test_already_exists_from_an_earlier_attempt_counts_as_accepted(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)
		message = "Invoice with invoice number - {0} - already exists.".format(si.name)
		self.post.return_value = reply(400, json.dumps({"message": message}))

		outbox.send(row)

		self.assertEqual(row_state(row).status, "Accepted")
		self.assertEqual(fetch_status(si), "Submitted")

	def test_server_error_goes_back_to_pending_with_a_later_attempt(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)
		self.post.return_value = reply(500, "Internal Server Error")
		before = now_datetime()

		outbox.send(row)

		state = row_state(row)
		self.assertEqual((state.status, state.attempts, state.response_code), ("Pending", 1, 500))
		self.assertGreater(get_datetime(state.next_attempt_at), before)
		self.assertEqual(fetch_status(si), "Pending")

	def test_timeout_goes_back_to_pending_with_a_later_attempt(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)
		self.post.side_effect = requests.Timeout("read timed out")
		before = now_datetime()

		outbox.send(row)

		state = row_state(row)
		self.assertEqual((state.status, state.attempts, state.response_code), ("Pending", 1, 0))
		self.assertGreater(get_datetime(state.next_attempt_at), before)

	def test_a_second_send_while_the_first_waits_on_fetch_makes_no_call(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)

		def second_worker_then_reply(*args, **kwargs):
			outbox.send(row)
			return reply(202)

		self.post.side_effect = second_worker_then_reply
		outbox.send(row)
		outbox.send(row)

		self.assertEqual(self.post.call_count, 1)
		state = row_state(row)
		self.assertEqual((state.status, state.attempts), ("Accepted", 1))

	def test_cancelling_before_send_cancels_the_row_and_sends_nothing(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)

		si.cancel()
		outbox.send(row)

		self.post.assert_not_called()
		self.assertEqual(row_state(row).status, "Cancelled")
		self.assertTrue(frappe.db.exists("Comment", {
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"comment_type": "Info",
			"content": ["like", "%{0}%".format(row)],
		}))

	def test_cancelling_while_the_post_is_out_keeps_fetchs_answer(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)

		def cancel_then_reply(*args, **kwargs):
			si.cancel()
			return reply(202)

		self.post.side_effect = cancel_then_reply
		outbox.send(row)

		self.assertEqual(row_state(row).status, "Accepted")
		self.assertTrue(frappe.db.exists("Comment", {
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"comment_type": "Info",
			"content": ["like", "%{0} was being sent to FETCH%".format(row)],
		}))

	def test_sweep_puts_a_stuck_sending_row_back_and_queues_it(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)
		frappe.db.sql(
			"update `tabFETCH Outbox` set status = 'Sending', modified = %s where name = %s",
			(add_to_date(now_datetime(), minutes=-(outbox.STALE_MINUTES + 1)), row),
		)
		self.enqueue.reset_mock()

		outbox.sweep()

		self.assertEqual(row_state(row).status, "Pending")
		self.assertIn(send_call(row), self.enqueue.call_args_list)

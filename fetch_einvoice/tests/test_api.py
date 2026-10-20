"""IRN callback, resend, payment and FIRS code sync integration tests: bench --site <site> run-tests --app fetch_einvoice."""
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import MagicMock, call, patch

import frappe
from werkzeug.test import EnvironBuilder

from fetch_einvoice import api, outbox
from fetch_einvoice.tests.test_outbox import (
	CALLBACK_SECRET,
	OUTBOX,
	FetchTestCase,
	fetch_status,
	make_due,
	make_invoice,
	make_payment,
	reply,
	row_for,
	row_state,
	send_call,
)

test_dependencies = ["Account", "Item", "Customer"]


def transmitted(si, irn=None, environment="Sandbox"):
	return {
		"eventId": frappe.generate_hash(length=32),
		"eventType": "invoice.transmitted",
		"schemaVersion": 1,
		"environment": environment,
		"invoiceId": "fetch-id-" + si.name,
		"invoiceNumber": si.name,
		"irn": irn or "NRS" + si.name.replace("-", ""),
		"transmissionStatus": "Sent",
	}


def deliver(event, secret=CALLBACK_SECRET, timestamp=None):
	"""POST `event` to receive_fetch_irn, signed the way FETCH's ErpCallbackClient signs it."""
	body = json.dumps(event).encode("utf-8")
	ts = str(int(time.time()) if timestamp is None else timestamp)
	signature = "sha256=" + hmac.new(secret.encode("utf-8"), ts.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
	builder = EnvironBuilder(
		method="POST",
		data=body,
		content_type="application/json",
		headers={"X-Fetch-Timestamp": ts, "X-Fetch-Signature": signature},
	)
	previous = getattr(frappe.local, "request", None)
	frappe.local.request = builder.get_request()
	try:
		return api.receive_fetch_irn()
	finally:
		frappe.local.request = previous


def stored_irn(si):
	return frappe.db.get_value("Sales Invoice", si.name, ["fetch_irn", "fetch_invoice_id", "fetch_status"], as_dict=True)


def info_comments(si):
	return frappe.db.count("Comment", {"reference_doctype": "Sales Invoice", "reference_name": si.name, "comment_type": "Info"})


def payload_sent(post, index):
	return post.call_args_list[index][1]["json"]


class TestIrnCallback(FetchTestCase):
	def accept(self, si):
		self.post.return_value = reply(202)
		outbox.send(row_for("Invoice", si.name))

	def test_signed_callback_stores_the_irn_and_wakes_payments(self):
		si = make_invoice()
		self.accept(si)
		event = transmitted(si)

		self.assertIsNone(deliver(event))

		saved = stored_irn(si)
		self.assertEqual(
			(saved.fetch_irn, saved.fetch_invoice_id, saved.fetch_status),
			(event["irn"], event["invoiceId"], "Transmitted"))
		wake = call("fetch_einvoice.outbox.wake", sales_invoice=si.name, enqueue_after_commit=True)
		self.assertIn(wake, self.enqueue.call_args_list)

		comments = info_comments(si)
		self.enqueue.reset_mock()
		self.assertIsNone(deliver(event))

		self.assertEqual(stored_irn(si), saved)
		self.assertEqual(info_comments(si), comments)
		self.enqueue.assert_not_called()

	def test_callback_that_beats_the_202_leaves_the_invoice_transmitted(self):
		si = make_invoice()
		deliver(transmitted(si))

		self.accept(si)

		self.assertEqual(row_state(row_for("Invoice", si.name)).status, "Accepted")
		self.assertEqual(fetch_status(si), "Transmitted")

	def test_bad_or_expired_signature_is_401_and_changes_nothing(self):
		si = make_invoice()
		with self.assertRaises(frappe.AuthenticationError):
			deliver(transmitted(si), secret="not-the-secret")
		with self.assertRaises(frappe.AuthenticationError):
			deliver(transmitted(si), timestamp=int(time.time()) - 600)

		self.assertFalse(stored_irn(si).fetch_irn)

	def test_callback_for_the_other_environment_is_409_and_changes_nothing(self):
		si = make_invoice()
		with self.assertRaises(api.EnvironmentMismatch) as caught:
			deliver(transmitted(si, environment="Production"))

		self.assertEqual(caught.exception.http_status_code, 409)
		self.assertFalse(stored_irn(si).fetch_irn)

	def test_callback_for_an_unknown_invoice_is_404(self):
		si = make_invoice()
		event = dict(transmitted(si), invoiceNumber="NO-SUCH-INVOICE")
		with self.assertRaises(frappe.DoesNotExistError) as caught:
			deliver(event)

		self.assertEqual(caught.exception.http_status_code, 404)


class TestPayments(FetchTestCase):
	def accept(self, si):
		self.post.return_value = reply(202)
		outbox.send(row_for("Invoice", si.name))

	def test_payments_wait_for_the_irn_then_go_oldest_first_with_a_frozen_retry(self):
		si = make_invoice()
		self.accept(si)
		first, second = make_payment(si, 50), make_payment(si, 57.5)
		first_row, second_row = row_for("Payment", first.name), row_for("Payment", second.name)
		self.post.reset_mock()

		outbox.send(second_row)
		outbox.send(first_row)

		self.post.assert_not_called()
		self.assertEqual([row_state(r).attempts for r in (first_row, second_row)], [0, 0])
		self.assertEqual([row_state(r).status for r in (first_row, second_row)], ["Pending", "Pending"])

		event = transmitted(si)
		deliver(event)
		# FETCH reports 55.00 outstanding after the first payment, not ERPNext's 57.50.
		self.post.side_effect = [
			reply(202, json.dumps({"outstandingBalance": 55.0})),
			reply(503, "Service Unavailable"),
			reply(202, json.dumps({"outstandingBalance": 0})),
		]
		outbox.wake(si.name)

		sent = [payload_sent(self.post, 0), payload_sent(self.post, 1)]
		self.assertEqual([p["paymentReference"] for p in sent], [first.name + "-1", second.name + "-1"])
		self.assertEqual([p["irn"] for p in sent], [event["irn"], event["irn"]])
		self.assertEqual([(p["paymentStatus"], p["amount"]) for p in sent], [("PARTIAL", 50.0), ("PAID", 55.0)])
		self.assertEqual(row_state(first_row).status, "Accepted")
		self.assertEqual(row_state(second_row).status, "Pending")

		# A recomputed payload would now say 40.00, so an unchanged retry proves the first one was frozen.
		frappe.db.set_value(OUTBOX, first_row, "response_body", json.dumps({"outstandingBalance": 40.0}))
		make_due(second_row)
		outbox.send(second_row)

		self.assertEqual(json.dumps(payload_sent(self.post, 2)), json.dumps(sent[1]))
		self.assertEqual(row_state(second_row).status, "Accepted")
		self.assertTrue(api.payment_accepted_by_fetch(second.name))

	def test_a_failed_payment_blocks_later_ones_until_its_entry_is_cancelled(self):
		si = make_invoice()
		self.accept(si)
		deliver(transmitted(si))
		first = make_payment(si, 50)
		first_row = row_for("Payment", first.name)
		self.post.return_value = reply(409, json.dumps({"message": "paymentStatus disagrees with the balance"}))
		outbox.send(first_row)
		self.assertEqual(row_state(first_row).status, "Failed")
		self.assertFalse(api.payment_accepted_by_fetch(first.name))

		second = make_payment(si, 20)
		second_row = row_for("Payment", second.name)
		self.post.reset_mock()
		outbox.send(second_row)
		self.post.assert_not_called()

		first.cancel()
		self.post.return_value = reply(202, json.dumps({"outstandingBalance": 87.5}))
		make_due(second_row)
		outbox.send(second_row)

		self.assertEqual(row_state(first_row).status, "Cancelled")
		self.assertEqual(self.post.call_count, 1)
		sent = payload_sent(self.post, 0)
		self.assertEqual(
			(sent["paymentReference"], sent["paymentStatus"], sent["amount"]), (second.name + "-1", "PARTIAL", 20.0))
		self.assertEqual(row_state(second_row).status, "Accepted")


	def test_payments_of_a_failed_invoice_fail_then_go_once_it_gets_its_irn(self):
		si = make_invoice()
		invoice_row = row_for("Invoice", si.name)
		self.post.return_value = reply(400, json.dumps({"message": "Customer TIN is invalid"}))
		outbox.send(invoice_row)
		pe = make_payment(si, 50)
		payment_row = row_for("Payment", pe.name)
		self.post.reset_mock()

		outbox.send(payment_row)

		self.post.assert_not_called()
		state = row_state(payment_row)
		self.assertEqual((state.status, state.attempts), ("Failed", 0))

		api.resend_invoice(si.name)
		self.post.return_value = reply(202)
		outbox.send(invoice_row)
		deliver(transmitted(si))
		outbox.wake(si.name)

		self.assertEqual(row_state(payment_row).status, "Accepted")
		sent = payload_sent(self.post, -1)
		self.assertEqual((sent["paymentReference"], sent["amount"]), (pe.name + "-1", 50.0))

	def test_a_payment_whose_invoice_was_cancelled_is_not_sent(self):
		si = make_invoice()
		self.accept(si)
		deliver(transmitted(si))
		pe = make_payment(si, 50)
		row = row_for("Payment", pe.name)
		setting = "unlink_payment_on_cancellation_of_invoice"
		self.addCleanup(frappe.db.set_value, "Accounts Settings", None, setting,
			frappe.db.get_single_value("Accounts Settings", setting))
		frappe.db.set_value("Accounts Settings", None, setting, 1)
		# The setting unlinks the payment, so the Payment Entry stays submitted.
		frappe.get_doc("Sales Invoice", si.name).cancel()
		self.post.reset_mock()

		outbox.send(row)

		self.post.assert_not_called()
		self.assertEqual(row_state(row).status, "Cancelled")

	def test_payment_before_the_first_send_is_not_also_a_deposit(self):
		si = make_invoice()
		make_payment(si, 50)

		self.accept(si)

		sent = payload_sent(self.post, 0)
		self.assertEqual((sent["deposit"], sent["paymentStatus"]), (0.0, "PENDING"))

	def test_retrying_a_refused_payment_recomputes_it(self):
		si = make_invoice()
		self.accept(si)
		deliver(transmitted(si))
		pe = make_payment(si, 50)
		row = row_for("Payment", pe.name)
		self.post.return_value = reply(409, json.dumps({"message": "Requested payment status is inconsistent"}))
		outbox.send(row)
		self.assertEqual(row_state(row).status, "Failed")
		frappe.db.set_value(OUTBOX, row, "erp_settled", 1)

		outbox.retry(row)
		self.post.return_value = reply(202, json.dumps({"outstandingBalance": 0}))
		outbox.send(row)

		sent = payload_sent(self.post, -1)
		self.assertEqual((sent["paymentStatus"], sent["amount"]), ("PAID", 107.5))
		self.assertEqual(row_state(row).status, "Accepted")


class TestResend(FetchTestCase):
	def test_rejected_invoice_fails_until_resent(self):
		si = make_invoice()
		row = row_for("Invoice", si.name)
		self.post.return_value = reply(401, "")
		outbox.send(row)
		self.assertEqual(row_state(row).status, "Failed")
		self.assertEqual(fetch_status(si), "Failed")
		self.enqueue.reset_mock()

		api.resend_invoice(si.name)

		state = row_state(row)
		self.assertEqual((state.status, state.attempts), ("Pending", 0))
		self.assertEqual(fetch_status(si), "Pending")
		self.assertIn(send_call(row), self.enqueue.call_args_list)

		self.post.return_value = reply(202)
		outbox.send(row)
		self.assertEqual(row_state(row).status, "Accepted")
		self.assertEqual(fetch_status(si), "Submitted")


FIRS_LISTS = {
	"tax-categories": [
		{"code": "TEST_SYNC_VAT", "value": "Test Sync Value-Added Tax", "percent": "7.5"},
		{"code": "TEST_SYNC_GST", "value": "Test Sync Goods and Services Tax", "percent": "Not Available"},
	],
	"invoice-quantity-codes": [{"code": "TSQ", "name": "test sync box", "description": "A test box."}],
	"hs-codes": [{"hscode": "9999.99", "description": "Test sync goods"}],
}


def firs_get(url, **kwargs):
	return MagicMock(status_code=200, json=lambda: {"code": 200, "data": FIRS_LISTS[url.rsplit("/", 1)[1]]})


class TestFirsCodeSync(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for doctype, name in (("FIRS Tax Category", "TEST_SYNC_VAT"), ("FIRS Tax Category", "TEST_SYNC_GST"),
				("FIRS Quantity Code", "TSQ"), ("FIRS HSN Code", "9999.99")):
			frappe.delete_doc_if_exists(doctype, name)
		patcher = patch("fetch_einvoice.api.requests.get", side_effect=firs_get)
		self.get = patcher.start()
		self.addCleanup(patcher.stop)

	def test_sync_stores_the_firs_labels_as_titles_and_keeps_hand_entered_rates(self):
		frappe.get_doc(dict(doctype="FIRS Tax Category", code="TEST_SYNC_GST", tax_rate=5)).insert()

		api.run_firs_code_sync()

		self.assertEqual(frappe.db.get_value("FIRS Tax Category", "TEST_SYNC_VAT", ["title", "tax_rate"]),
			("Test Sync Value-Added Tax", 7.5))
		self.assertEqual(frappe.db.get_value("FIRS Tax Category", "TEST_SYNC_GST", ["title", "tax_rate"]),
			("Test Sync Goods and Services Tax", 5))
		self.assertEqual(frappe.db.get_value("FIRS Quantity Code", "TSQ", ["title", "description"]),
			("test sync box", "A test box."))
		self.assertEqual(frappe.db.get_value("FIRS HSN Code", "9999.99", ["description", "kind"]),
			("Test sync goods", "Product"))
		for doctype, title in (("FIRS Tax Category", "title"), ("FIRS Quantity Code", "title"),
				("FIRS HSN Code", "description")):
			self.assertEqual(frappe.get_meta(doctype).title_field, title)

	def test_a_second_sync_changes_nothing_and_a_changed_label_is_updated(self):
		api.run_firs_code_sync()
		modified = frappe.db.get_value("FIRS Quantity Code", "TSQ", "modified")

		api.run_firs_code_sync()
		self.assertEqual(frappe.db.get_value("FIRS Quantity Code", "TSQ", "modified"), modified)

		FIRS_LISTS["hs-codes"][0]["description"] = "Test sync goods, renamed"
		self.addCleanup(FIRS_LISTS["hs-codes"][0].update, description="Test sync goods")
		api.run_firs_code_sync()
		self.assertEqual(frappe.db.get_value("FIRS HSN Code", "9999.99", "description"), "Test sync goods, renamed")

	def test_firs_error_stops_the_sync_and_is_logged(self):
		self.get.side_effect = lambda url, **kwargs: MagicMock(status_code=503)
		errors = frappe.db.count("Error Log")

		api.run_firs_code_sync()

		self.assertFalse(frappe.db.exists("FIRS Tax Category", "TEST_SYNC_VAT"))
		self.assertEqual(frappe.db.count("Error Log"), errors + 1)

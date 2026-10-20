"""doc_events for Sales Invoice and Payment Entry (see hooks.py)."""
import frappe
from frappe import _
from frappe.utils import escape_html, flt

from fetch_einvoice import core, gather, outbox


def set_firs_tax_categories(doc, method=None):
	"""validate: a row with an Item Tax Template takes that template's FIRS Tax Category, since the template decides its tax."""
	if not _in_scope(doc):
		return
	for item in doc.items:
		category = item.item_tax_template and frappe.db.get_value("Item Tax Template", item.item_tax_template, "firs_tax_category")
		if category:
			item.firs_tax_category = category


def validate_sales_invoice(doc, method=None):
	"""before_submit: refuse an invoice FETCH would reject, listing every reason at once."""
	if not _in_scope(doc):
		return
	problems = core.invoice_problems(gather.invoice_from_doc(doc))
	if problems:
		frappe.throw(
			"<ul>{0}</ul>".format("".join("<li>{0}</li>".format(escape_html(p)) for p in problems)),
			title=_("FETCH cannot accept this invoice"),
		)


def queue_sales_invoice(doc, method=None):
	"""on_submit: queue the invoice, an amended one included, for FETCH."""
	if not _in_scope(doc):
		return
	# amount keeps outstanding_amount as of submit, so a payment taken before the first send is not also a deposit.
	outbox.create(
		kind="Invoice", reference_doctype=doc.doctype, reference_name=doc.name, sales_invoice=doc.name,
		amount=doc.outstanding_amount)
	# update_modified=False keeps the just-submitted form current, so the user can cancel or amend it at once.
	frappe.db.set_value("Sales Invoice", doc.name, "fetch_status", "Pending", update_modified=False)
	doc.fetch_status = "Pending"


def queue_payment_entry(doc, method=None):
	"""on_submit: queue one payment per invoice reference that FETCH knows about."""
	if doc.payment_type != "Receive" or doc.party_type != "Customer" or not _in_scope(doc):
		return
	for ref in doc.references:
		if ref.reference_doctype != "Sales Invoice" or not flt(ref.allocated_amount):
			continue
		if frappe.db.get_value("Sales Invoice", ref.reference_name, "is_return"):
			continue
		# Invoices from before this app have no row and will never get an IRN from FETCH.
		if not frappe.db.exists(outbox.OUTBOX, {"kind": "Invoice", "sales_invoice": ref.reference_name}):
			continue
		# The controller has already posted GL and updated outstanding_amount in this transaction.
		outstanding = frappe.db.get_value("Sales Invoice", ref.reference_name, "outstanding_amount")
		outbox.create(
			kind="Payment",
			reference_doctype=doc.doctype,
			reference_name=doc.name,
			sales_invoice=ref.reference_name,
			amount=ref.allocated_amount,
			erp_settled=1 if flt(outstanding) <= 0 else 0,
			payment_reference="{0}-{1}".format(doc.name, ref.idx),
		)


def cancel_outbox_rows(doc, method=None):
	"""on_cancel: stop every row of this document that FETCH has not accepted, and say what happened."""
	rows = frappe.get_all(
		outbox.OUTBOX, filters={"reference_doctype": doc.doctype, "reference_name": doc.name}, fields=["name", "status"])
	if not rows:
		return
	cancelled = [row.name for row in rows if outbox.cancel(row.name)]
	accepted = [row.name for row in rows if row.status == "Accepted"]
	in_flight = [row.name for row in rows if row.name not in cancelled and row.status not in ("Accepted", "Cancelled")]
	notes = []
	if cancelled:
		notes.append(_("These FETCH Outbox rows had not reached FETCH and are now cancelled: {0}.").format(
			", ".join(cancelled)))
	if in_flight:
		notes.append(_("{0} was being sent to FETCH during the cancel. Check FETCH Outbox for FETCH's answer.").format(
			", ".join(in_flight)))
	if accepted:
		notes.append(_("FETCH had already accepted {0}. FETCH has no reversal, so the NRS record stays valid.").format(
			", ".join(accepted)))
	if notes:
		doc.add_comment("Info", " ".join(notes))


def _in_scope(doc):
	# ERPNext submits consolidated POS invoices itself, for walk-in customers FETCH cannot accept; refusing them would stop POS closing.
	if doc.get("is_consolidated"):
		return False
	settings = frappe.get_single(outbox.SETTINGS)
	return bool(settings.enabled) and (not settings.company or settings.company == doc.company)

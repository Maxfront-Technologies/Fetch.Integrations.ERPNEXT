"""The one place that reads Frappe records into core.Invoice."""
from decimal import Decimal

import frappe
from frappe.utils import flt, get_time, getdate

from fetch_einvoice import core


def invoice_from_doc(si) -> core.Invoice:
	customer = frappe.db.get_value(
		"Customer", si.customer, ["email_id", "firs_business_description"], as_dict=True) or frappe._dict()
	original_date = frappe.db.get_value("Sales Invoice", si.return_against, "posting_date") if si.return_against else None
	return core.Invoice(
		name=si.name,
		posting_date=getdate(si.posting_date),
		posting_time=get_time(si.posting_time).isoformat(),
		due_date=getdate(si.due_date) if si.due_date else None,
		currency=si.currency,
		remarks=si.remarks or "",
		is_return=bool(si.is_return),
		is_debit_note=bool(si.is_debit_note),
		return_against=si.return_against or None,
		return_against_date=getdate(original_date) if original_date else None,
		grand_total=_money(si.grand_total),
		rounded_total=_money(si.rounded_total) if flt(si.rounded_total) and not si.disable_rounded_total else None,
		outstanding_amount=_money(si.outstanding_amount),
		payment_mode_types=_payment_mode_types(si),
		customer=core.Party(
			name=si.customer_name or si.customer,
			tin=si.tax_id or "",
			email=si.contact_email or customer.email_id or "",
			phone=si.contact_mobile or "",
			business_description=customer.firs_business_description or "",
		),
		address=_address(si.customer_address),
		taxes=tuple(
			core.TaxRow(charge_type=tax.charge_type, rate=_money(tax.rate), included_in_print_rate=bool(tax.included_in_print_rate))
			for tax in si.taxes
		),
		lines=tuple(_line(item) for item in si.items),
	)


def _money(value) -> Decimal:
	return Decimal(str(flt(value)))


def _payment_mode_types(si):
	modes = [row.mode_of_payment for row in si.payment_schedule if row.mode_of_payment]
	if not modes:
		modes = [row.mode_of_payment for row in si.payments if row.mode_of_payment and flt(row.amount)]
	return tuple(frappe.db.get_value("Mode of Payment", mode, "type") or "" for mode in modes)


def _address(name):
	address = frappe.db.get_value(
		"Address", name, ["address_line1", "address_line2", "city", "state", "pincode", "country"], as_dict=True,
	) if name else None
	if not address:
		return None
	return core.Address(
		line1=address.address_line1 or "",
		line2=address.address_line2 or "",
		city=address.city or "",
		state=address.state or "",
		pincode=address.pincode or "",
		country_code=(frappe.db.get_value("Country", address.country, "code") or "") if address.country else "",
	)


def _line(item) -> core.Line:
	tax_rate = frappe.db.get_value("FIRS Tax Category", item.firs_tax_category, "tax_rate") if item.firs_tax_category else None
	return core.Line(
		row_name=item.name,
		item_code=item.item_code or "",
		item_name=item.item_name or "",
		description=item.description or "",
		item_group=item.item_group or "",
		qty=_money(item.qty),
		net_amount=_money(item.net_amount),
		original_row_name=item.sales_invoice_item or None,
		hsn_code=item.firs_hsn_code or None,
		hsn_kind=(frappe.db.get_value("FIRS HSN Code", item.firs_hsn_code, "kind") or None) if item.firs_hsn_code else None,
		tax_category=item.firs_tax_category or None,
		tax_rate=_money(tax_rate) if tax_rate is not None else None,
		quantity_code=item.firs_quantity_code or None,
	)

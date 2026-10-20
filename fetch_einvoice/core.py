"""Frappe-free rules for talking to FETCH: payloads, FETCH's validation, responses, payments, signatures.

Every FETCH rule here cites the FETCH.API source it was ported from (paths under Api.Fetch/Endpoints/).
"""

import copy
import hashlib
import hmac
import html
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import List, Optional, Tuple

COMMERCIAL_INVOICE = "381"  # Invoices/InvoiceTypes.cs:5
CREDIT_NOTE = "380"  # Invoices/InvoiceTypes.cs:6
CURRENCIES = ("USD", "NGN", "GBP", "EUR", "CAD")  # ThirdPartyInvoiceValidator.cs:18-25
PAYMENT_STATUSES = ("PENDING", "PARTIAL", "PAID", "REJECTED")  # ThirdPartyInvoiceValidator.cs:10-16
PAYMENT_MEANS_CODES = ("10", "20", "30", "31", "42", "43", "48", "49", "50", "58", "97", "ZZZ")  # :27-41
PAYMENT_MEANS_BY_MODE_TYPE = {"Cash": "10", "Bank": "30", "Phone": "30"}
OTHER_PAYMENT_MEANS = "97"

# FETCH column limits (Domain/Entities/*Config.cs); longer values fail with a 500 instead of a 400.
NOTE_MAX = 500  # InvoiceConfig.cs:44-45, and CreditNoteService.cs:155 rejects longer credit-note notes
ITEM_NAME_MAX = 100  # SaleItemConfig.cs:15-17
ITEM_DESCRIPTION_MAX = 250  # SaleItemConfig.cs:23-24
PRODUCT_CATEGORY_MAX = 100  # InvoiceLineConfig.cs:21-22

CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
ZERO = Decimal("0")
# ERPNext rounds each tax row once and this payload rounds each line once; each rounding moves the total by at most half a cent.
HALF_CENT = Decimal("0.005")

_PHONE = re.compile(r"^\+\d{8,15}$")  # ThirdPartyInvoiceValidator.cs:305-306
_TAX_CATEGORY = re.compile(r"^[A-Z]+(?:_[A-Z]+)*$")  # ThirdPartyInvoiceValidator.cs:308-309
_LINE_FIELD = re.compile(r"^(?:lineItems|invoiceLine)\[(\d+)\](?:\.(\w+))?$")


@dataclass(frozen=True)
class Party:
	name: str
	tin: str
	email: str
	phone: str
	business_description: str


@dataclass(frozen=True)
class Address:
	line1: str
	line2: str
	city: str
	state: str
	pincode: str
	country_code: str


@dataclass(frozen=True)
class TaxRow:
	charge_type: str
	rate: Decimal
	included_in_print_rate: bool


@dataclass(frozen=True)
class Line:
	row_name: str
	item_code: str
	item_name: str
	description: str
	item_group: str
	qty: Decimal
	net_amount: Decimal
	original_row_name: Optional[str]
	hsn_code: Optional[str]
	hsn_kind: Optional[str]
	tax_category: Optional[str]
	tax_rate: Optional[Decimal]
	quantity_code: Optional[str]


@dataclass(frozen=True)
class Invoice:
	name: str
	posting_date: date
	posting_time: str
	due_date: Optional[date]
	currency: str
	remarks: str
	is_return: bool
	is_debit_note: bool
	return_against: Optional[str]
	return_against_date: Optional[date]
	grand_total: Decimal
	outstanding_amount: Decimal
	payment_mode_types: Tuple[str, ...]
	customer: Party
	address: Optional[Address]
	taxes: Tuple[TaxRow, ...]
	lines: Tuple[Line, ...]
	rounded_total: Optional[Decimal] = None


@dataclass(frozen=True)
class PaymentDecision:
	amount: Decimal
	status: str


class Outcome(Enum):
	ACCEPTED = "Accepted"
	RETRY = "Retry"
	FAILED = "Failed"


RETRY_DELAYS_MINUTES = (1, 5, 15, 60, 240)
MAX_ATTEMPTS = 6


def _money(value) -> Decimal:
	return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _text(value) -> str:
	return (value or "").strip()


def _clip(value: str, limit: int) -> str:
	return value if len(value) <= limit else value[: limit - 1] + "…"


def _plain(value: Optional[str]) -> str:
	return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def _hms(posting_time: str) -> str:
	parts = [int(float(p)) for p in (posting_time or "0").split(".")[0].split(":")]
	parts += [0] * (3 - len(parts))
	return "{:02d}:{:02d}:{:02d}".format(*parts[:3])


def _json_qty(qty: Decimal):
	# FETCH binds quantity to int: 1.5 and even 2.0 fail to deserialise, so never round or truncate here.
	return int(qty) if qty == qty.to_integral_value() else float(qty)


def _line_amounts(line: Line) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
	"""(qty, net, rate, tax) as positive values; gross = net + tax."""
	net = abs(_money(line.net_amount))
	rate = line.tax_rate if line.tax_rate is not None else ZERO
	return abs(line.qty), net, rate, _money(net * rate / 100)


def _row_label(inv: Invoice, index: int) -> str:
	line = inv.lines[index] if index < len(inv.lines) else None
	code = " ({})".format(line.item_code) if line and line.item_code else ""
	return "Item row {}{}".format(index + 1, code)


def build_invoice_payload(inv: Invoice) -> dict:
	"""JSON-ready body for POST /api/v1/invoices/transmit (ThirdPartyInvoiceModels.cs)."""
	credit = inv.is_return
	lines = []
	groups = {}
	total_net = total_tax = ZERO
	for line in inv.lines:
		qty, net, rate, tax = _line_amounts(line)
		# Invoice lines are tax-inclusive (ErpMasterDataUpserter.cs:107-126), but credit notes are reconciled against
		# the original's stored tax-exclusive line values (CreditNoteService.cs:523-524).
		amount = net if credit else net + tax
		unit = (amount / qty).quantize(MICRO, rounding=ROUND_HALF_UP) if qty else ZERO
		category = _text(line.tax_category)
		row = {
			"externalLineReference": _text(line.row_name),
			"hsnCode": _text(line.hsn_code),
			"productCategory": _clip(_text(line.item_group), PRODUCT_CATEGORY_MAX),
			"quantity": _json_qty(qty),
			"invoicedQuantity": _json_qty(qty),
			"unitPrice": float(unit),
			# net_amount already carries item and invoice-level discounts.
			"discountType": "Flat",
			"discountRate": 0,
			"discountValue": 0,
			"discountAmount": 0,
			# FETCH stock-checks Product items (SaleItem.cs:64, InventoryService.cs:162-173) and creates ERP items with no
			# stock, so every goods line would be refused. itemType reaches nothing else, including the NRS document.
			# shortcut: FETCH lists ERPNext goods as services, send line.hsn_kind once FETCH stops stock-checking ERP items.
			"itemType": "Service",
			"feeRate": 0,
			"feeAmount": 0,
			"taxRate": float(rate),
			"taxCategory": category,
			"lineExtensionAmount": float(amount),
			"item": {
				"name": _clip(_text(line.item_name), ITEM_NAME_MAX),
				"description": _clip(_plain(line.description) or _text(line.item_name), ITEM_DESCRIPTION_MAX),
				"sellersItemIdentification": _text(line.item_code),
			},
			"price": {"priceAmount": float(unit), "baseQuantity": 1, "priceUnit": _text(line.quantity_code)},
		}
		if credit and line.original_row_name:
			row["originalExternalLineReference"] = line.original_row_name.strip()
		lines.append(row)
		# Credit-note subtotals must match one allocation each (CreditNoteService.cs:576-600), so only invoices group.
		key = (category, rate) if not credit else len(groups)
		subtotal = groups.setdefault(key, [category, rate, ZERO, ZERO])
		subtotal[2] += net
		subtotal[3] += tax
		total_net += net
		total_tax += tax

	payable = total_net + total_tax
	if credit:
		deposit, status = ZERO, "PENDING"  # CreditNoteService.cs:33-36 and :460
	else:
		outstanding = _money(inv.outstanding_amount)
		if outstanding <= 0:
			deposit, status = payable, "PAID"
		else:
			# ERPNext counts outstanding down from the rounded total, so rounding down must not read as a payment.
			billed = _money(inv.rounded_total) if inv.rounded_total else _money(inv.grand_total)
			deposit = min(payable, max(ZERO, billed - outstanding))
			status = "PARTIAL" if deposit > 0 else "PENDING"

	due = inv.due_date or inv.posting_date
	modes = inv.payment_mode_types
	party = inv.customer
	address = inv.address
	payload = {
		"irn": _text(inv.name),
		"issueDate": inv.posting_date.isoformat(),
		"issueTime": _hms(inv.posting_time),
		"dueDate": due.isoformat(),
		"invoiceTypeCode": CREDIT_NOTE if credit else COMMERCIAL_INVOICE,
		"paymentStatus": status,
		"note": _clip(_text(inv.remarks), NOTE_MAX),
		"documentCurrencyCode": _text(inv.currency).upper(),
		"taxCurrencyCode": _text(inv.currency).upper(),
		"deposit": float(deposit),
		"accountingCustomerParty": {
			"partyName": _text(party.name),
			"tin": _text(party.tin),
			"email": _text(party.email),
			# An unfixable number is still sent as typed, so FETCH's own format rule names it.
			"telephone": normalise_phone(party.phone) or _text(party.phone) or None,
			"businessDescription": _text(party.business_description) or None,
			"postalAddress": None if address is None else {
				"streetName": ", ".join(p for p in (_text(address.line1), _text(address.line2)) if p),
				"cityName": _text(address.city) or _text(address.state),
				"postalZone": _text(address.pincode),
				"country": _text(address.country_code).upper(),
			},
		},
		"paymentMeans": [{
			"paymentMeansCode": PAYMENT_MEANS_BY_MODE_TYPE.get(modes[0], OTHER_PAYMENT_MEANS) if modes else OTHER_PAYMENT_MEANS,
			"paymentDueDate": due.isoformat(),
		}],
		"invoiceLine": lines,
		"taxTotal": [{
			"taxAmount": float(total_tax),
			"taxSubtotal": [
				{"taxableAmount": float(n), "taxAmount": float(t), "taxCategory": {"id": c, "percent": float(r)}}
				for c, r, n, t in groups.values()
			],
		}],
		"legalMonetaryTotal": {
			"lineExtensionAmount": float(total_net),
			"taxExclusiveAmount": float(total_net),
			"taxInclusiveAmount": float(payable),
			"payableAmount": float(payable),
		},
	}
	if credit and inv.return_against:
		payload["billingReference"] = [{
			"invoiceNumber": inv.return_against.strip(),
			"issueDate": inv.return_against_date.isoformat() if inv.return_against_date else None,
		}]
	return payload


def payable_of(payload: dict) -> Decimal:
	return _dec(payload["legalMonetaryTotal"]["payableAmount"])


def deposit_of(payload: dict) -> Decimal:
	return _dec(payload.get("deposit"))


def _dec(value) -> Decimal:
	if value is None:
		return ZERO
	return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _blank(value) -> bool:
	return value is None or not str(value).strip()


def _day(value) -> Optional[date]:
	try:
		return date.fromisoformat(value) if value else None
	except (TypeError, ValueError):
		return None


def _discount_type(value) -> Optional[str]:
	# ThirdPartyInvoiceValidator.cs:273-286
	key = (value or "").strip().lower()
	return "Percent" if key in ("percent", "percentage") else "Flat" if key in ("flat", "amount") else None


def _discount_amount(kind, gross: Decimal, value: Decimal) -> Decimal:
	# InvoiceMath.cs:8-16
	if kind == "Percent" and value > 0:
		return gross * (value / 100)
	if kind == "Flat" and value > 0:
		return value
	return ZERO


def _fetch_total(payload: dict) -> Decimal:
	# ThirdPartyInvoiceValidator.cs:243-271: line prices already include tax.
	total = ZERO
	for line in payload.get("invoiceLine") or []:
		gross = _dec(line.get("quantity")) * _dec(line.get("unitPrice"))
		total += gross - _discount_amount(_discount_type(line.get("discountType")), gross, _dec(line.get("discountValue")))
	for charge in payload.get("allowanceCharge") or []:
		total += _dec(charge.get("amount")) * (1 if charge.get("chargeIndicator") else -1)
	return total


def _normalise_credit(payload: dict) -> dict:
	# CreditNoteService.cs:450-507
	p = copy.deepcopy(payload)
	p["dueDate"] = p.get("issueDate")
	if not p.get("paymentMeans"):
		p["paymentMeans"] = [{"paymentMeansCode": "10", "paymentDueDate": p.get("issueDate")}]
	p["includePaymentLink"] = False
	p["allowPartialPayments"] = False
	p["paymentStatus"] = "PENDING"
	for line in p.get("invoiceLine") or []:
		for field in ("unitPrice", "lineExtensionAmount", "discountAmount", "feeAmount"):
			line[field] = abs(_dec(line.get(field)))
		if _text(line.get("discountType")).lower() == "flat":
			line["discountValue"] = abs(_dec(line.get("discountValue")))
		if (line.get("price") or {}).get("priceAmount") is not None:
			line["price"]["priceAmount"] = abs(_dec(line["price"]["priceAmount"]))
	for charge in p.get("allowanceCharge") or []:
		charge["amount"] = abs(_dec(charge.get("amount")))
	for tax in p.get("taxTotal") or []:
		tax["taxAmount"] = abs(_dec(tax.get("taxAmount")))
		for part in tax.get("taxSubtotal") or []:
			for field in ("taxAmount", "taxableAmount"):
				if part.get(field) is not None:
					part[field] = abs(_dec(part[field]))
	totals = p.get("legalMonetaryTotal")
	if totals is not None:
		for field in ("lineExtensionAmount", "taxExclusiveAmount", "taxInclusiveAmount", "payableAmount"):
			totals[field] = abs(_dec(totals.get(field)))
	return p


def _valid_email(value: str) -> bool:
	# System.ComponentModel.DataAnnotations.EmailAddressAttribute: one '@', neither first nor last.
	at = value.find("@")
	return 0 < at < len(value) - 1 and at == value.rfind("@")


def _validate(p: dict, add) -> None:
	"""ThirdPartyInvoiceValidator.Validate, rule for rule and in the same order (:43-109)."""

	def required(value, field, message):
		if _blank(value):
			add(field, message)

	required(p.get("irn"), "irn", "Invoice reference number (IRN) is required.")
	issue = _day(p.get("issueDate"))
	if issue is None or issue == date.min:
		add("issueDate", "Invoice date is required.")
		issue = None
	due = _day(p.get("dueDate"))
	if due is None:
		add("dueDate", "Due date is required.")
	if issue and due and due < issue:
		add("dueDate", "Due date must be on or after issue date.")
	required(p.get("invoiceTypeCode"), "invoiceTypeCode", "Invoice type code is required.")
	required(p.get("paymentStatus"), "paymentStatus", "Payment status is required.")
	required(p.get("documentCurrencyCode"), "documentCurrencyCode", "Document currency code is required.")
	means = (p.get("paymentMeans") or [None])[0]
	means_code = (means or {}).get("paymentMeansCode")
	required(means_code, "paymentMeansCode", "Payment means code is required.")
	if not _blank(p.get("paymentStatus")) and p["paymentStatus"].upper() not in PAYMENT_STATUSES:
		add("paymentStatus", "Payment status must be one of PENDING, PARTIAL, PAID, or REJECTED.")
	if not _blank(p.get("documentCurrencyCode")) and p["documentCurrencyCode"].upper() not in CURRENCIES:
		add("documentCurrencyCode", "Unsupported currency code.")
	if not _blank(means_code) and means_code.upper() not in PAYMENT_MEANS_CODES:
		add("paymentMeansCode", "Unsupported payment means code.")
	if p.get("deposit") is not None and _dec(p["deposit"]) < 0:
		add("deposit", "Deposit must be zero or greater.")

	_validate_customer(p.get("accountingCustomerParty"), add, required)
	_validate_lines(p.get("invoiceLine"), add, required)
	_validate_references(p, add)

	totals = p.get("legalMonetaryTotal")
	lines = p.get("invoiceLine") or []
	if _text(p.get("invoiceTypeCode")) != CREDIT_NOTE and totals is not None and lines:
		if abs(_fetch_total(p) - _dec(totals.get("payableAmount"))) > CENT:
			add("legalMonetaryTotal", "Total amount does not match the tax-inclusive line item totals.")


def _validate_customer(customer, add, required) -> None:
	# ThirdPartyInvoiceValidator.cs:137-174
	if customer is None:
		add("accountingCustomerParty", "Accounting customer party is required.")
		return
	prefix = "accountingCustomerParty"
	required(customer.get("tin"), prefix + ".tin", "Customer TIN is required.")
	required(customer.get("email"), prefix + ".email", "Customer email is required.")
	required(customer.get("telephone"), prefix + ".telephone", "Customer telephone is required.")
	required(customer.get("partyName"), prefix + ".partyName", "Customer party name is required.")
	if not _blank(customer.get("email")) and not _valid_email(customer["email"]):
		add(prefix + ".email", "Invalid email format.")
	if not _blank(customer.get("telephone")) and not _PHONE.match(customer["telephone"]):
		add(prefix + ".telephone", "Telephone must include international dialing code, for example +2348033456789.")
	address = customer.get("postalAddress")
	if address is None:
		add(prefix + ".postalAddress", "Customer postal address is required.")
		return
	required(address.get("streetName"), prefix + ".postalAddress.streetName", "Customer street name is required.")
	required(address.get("cityName"), prefix + ".postalAddress.cityName", "Customer city is required.")
	required(address.get("country"), prefix + ".postalAddress.country", "Customer country is required.")
	if not _blank(address.get("country")) and len(address["country"]) != 2:
		add(prefix + ".postalAddress.country", "Country must be a two-letter ISO code.")


def _validate_lines(lines, add, required) -> None:
	# ThirdPartyInvoiceValidator.cs:176-241
	if not lines:
		add("lineItems", "At least one line item is required.")
		return
	for index, line in enumerate(lines):
		prefix = "lineItems[{}]".format(index)
		item, price = line.get("item"), line.get("price")
		if item is None or price is None:
			add(prefix, "Item and price objects are required.")
			continue
		required(line.get("productCategory"), prefix + ".productCategory", "Product category is required.")
		required(item.get("name"), prefix + ".name", "Line item name is required.")
		required(item.get("description"), prefix + ".description", "Line item description is required.")
		required(line.get("hsnCode"), prefix + ".hsnCode", "HSN code is required.")
		required(price.get("priceUnit"), prefix + ".priceUnit", "Price unit is required.")
		required(line.get("taxCategory"), prefix + ".taxCategory", "Tax category is required.")
		quantity = line.get("quantity", 0)
		# Not a validator rule: FETCH's JSON binder rejects the whole request when quantity is not an integer.
		if isinstance(quantity, bool) or not isinstance(quantity, int):
			add(prefix + ".quantity", "Quantity must be a whole number.")
		if _dec(quantity) <= 0:
			add(prefix + ".quantity", "Quantity must be greater than zero.")
		price_amount = price.get("priceAmount")
		if (_dec(line.get("invoicedQuantity")) <= 0 or _dec(line.get("lineExtensionAmount")) < 0
				or (price_amount is not None and _dec(price_amount) < 0)
				or _dec(line.get("discountAmount")) < 0 or _dec(line.get("feeAmount")) < 0):
			add(prefix, "Invoiced quantity must be positive and monetary values must be non-negative.")
		if not all(0 <= _dec(line.get(f)) <= 100 for f in ("discountRate", "feeRate")):
			add(prefix, "Percentage rates must be between zero and 100.")
		if _dec(line.get("unitPrice")) < 0:
			add(prefix + ".unitPrice", "Unit price must be zero or greater.")
		if not 0 <= _dec(line.get("taxRate")) <= 100:
			add(prefix + ".taxRate", "Tax rate must be between 0 and 100.")
		if _dec(line.get("discountValue")) < 0:
			add(prefix + ".discountValue", "Discount value must be zero or greater.")
		if not _blank(line.get("discountType")) and _discount_type(line["discountType"]) is None:
			add(prefix + ".discountType", "Discount type must be Percent or Flat.")
		if not _blank(line.get("taxCategory")) and not _TAX_CATEGORY.match(line["taxCategory"]):
			add(prefix + ".taxCategory", "Tax category must use uppercase letters and underscores.")


def _validate_references(p: dict, add) -> None:
	# ThirdPartyInvoiceValidator.cs:111-135
	seen = set()
	credit = _text(p.get("invoiceTypeCode")) == CREDIT_NOTE
	for index, line in enumerate(p.get("invoiceLine") or []):
		prefix = "invoiceLine[{}].".format(index)
		for field in ("externalLineReference", "originalExternalLineReference"):
			value = line.get(field)
			if value is not None and (_blank(value) or len(value.strip()) > 200):
				add(prefix + field, "Reference must contain between 1 and 200 characters.")
		reference = line.get("externalLineReference")
		if reference is not None:
			if reference.strip() in seen:
				add(prefix + "externalLineReference", "ERP row references must be unique within the document.")
			seen.add(reference.strip())
		if not credit and (line.get("originalExternalLineReference") is not None or line.get("originalInvoiceLineId") is not None):
			add(prefix + "originalExternalLineReference", "Original line references are only supported on Credit Notes.")


def _fetch_errors(payload: dict) -> List[Tuple[str, str]]:
	"""(field, message) for every rule FETCH applies before it looks at stored data. FETCH stops at the first."""
	errors = []

	def add(field, message):
		errors.append((field, message))

	if _text(payload.get("invoiceTypeCode")) != CREDIT_NOTE:
		_validate(payload, add)
		return errors
	# CreditNoteService.cs:27-43, then the per-line check at :74-77.
	refs = payload.get("billingReference") or []
	if len(refs) != 1 or (_blank(refs[0].get("irn")) and _blank(refs[0].get("invoiceNumber"))):
		add("billingReference", "A Credit Note requires exactly one original ERP invoiceNumber or NRS irn and issue date.")
	if _dec(payload.get("deposit")) != 0:
		add("deposit", "Credit Notes cannot carry a payment deposit.")
	normalised = _normalise_credit(payload)
	_validate(normalised, add)
	for index, line in enumerate(normalised.get("invoiceLine") or []):
		# Quantities of zero or less are already reported by the validator above.
		if _dec(line.get("quantity")) > 0 and line.get("invoicedQuantity") != line.get("quantity"):
			add("invoiceLine[{}]".format(index), "quantity and invoicedQuantity must be equal and greater than zero.")
	return errors


_ROW_TEXT = {
	"Product category is required.": "Item Group is missing.",
	"Line item name is required.": "Item Name is missing.",
	"Line item description is required.": "Description is missing.",
	"HSN code is required.": "FIRS HSN Code is missing.",
	"Price unit is required.": "FIRS Quantity Code is missing.",
	"Tax category is required.": "FIRS Tax Category is missing.",
	"Quantity must be a whole number.": "quantity {qty} isn't a whole number. FETCH only accepts whole quantities.",
	"Quantity must be greater than zero.": "quantity must be at least 1.",
	"Invoiced quantity must be positive and monetary values must be non-negative.": "quantity must be at least 1.",
	"Tax rate must be between 0 and 100.": "FIRS Tax Category {category} has a rate of {rate}%. It must be between 0 and 100.",
	"Tax category must use uppercase letters and underscores.":
		"FIRS Tax Category code {category} must use only capital letters and underscores, for example STANDARD_VAT.",
	"Reference must contain between 1 and 200 characters.": "its row ID must be 1 to 200 characters long.",
	"ERP row references must be unique within the document.": "its row ID is used by another row too.",
}

_TEXT = {
	"Invoice date is required.": "Posting Date is missing.",
	"Due date must be on or after issue date.": "Due Date {due} is before the Posting Date {issue}.",
	"Unsupported currency code.": "Currency {currency} isn't accepted by FETCH. Use USD, NGN, GBP, EUR or CAD.",
	"Document currency code is required.": "Currency is missing.",
	"Customer TIN is required.": "Tax Id is missing. Set it on the invoice or on the Customer.",
	"Customer email is required.": "Customer email is missing. Set Contact Email on the invoice, or Email Id on the Customer.",
	"Invalid email format.": "Customer email {email} isn't a valid email address.",
	"Customer telephone is required.": "Customer mobile number is missing. Set Mobile No on the invoice's contact.",
	"Telephone must include international dialing code, for example +2348033456789.":
		"Customer mobile number {phone} isn't valid. Use the international format, for example +2348033456789.",
	"Customer party name is required.": "Customer Name is missing.",
	"Customer postal address is required.": "Customer Address is missing. Pick one on the invoice.",
	"Customer street name is required.": "Customer Address has no Address Line 1.",
	"Customer city is required.": "Customer Address has no City.",
	"Customer country is required.": "Customer Address has no Country.",
	"Country must be a two-letter ISO code.":
		"Customer Address country code {country} isn't a two-letter code. Check Code on the Country record.",
	"At least one line item is required.": "The invoice has no items.",
	"Total amount does not match the tax-inclusive line item totals.":
		"FETCH would compute a total of {fetch_total} from quantity × unit price, but the invoice total is {payable}.",
	"A Credit Note requires exactly one original ERP invoiceNumber or NRS irn and issue date.":
		"Return Against is missing. A return must name the invoice it returns.",
}


def _explain(inv: Invoice, payload: dict, field: str, message: str) -> str:
	match = _LINE_FIELD.match(field)
	if match:
		index = int(match.group(1))
		line = (payload.get("invoiceLine") or [{}] * (index + 1))[index]
		text = _ROW_TEXT.get(message, message).format(
			qty=inv.lines[index].qty.normalize() if index < len(inv.lines) else "",
			category=line.get("taxCategory"), rate=line.get("taxRate"))
		return "{}: {}".format(_row_label(inv, index), text)
	party = payload.get("accountingCustomerParty") or {}
	template = _TEXT.get(message)
	if template is None:
		return "FETCH would reject {}: {}".format(field, message)
	return template.format(
		due=payload.get("dueDate"), issue=payload.get("issueDate"), currency=payload.get("documentCurrencyCode"),
		email=party.get("email"), phone=party.get("telephone"),
		country=(party.get("postalAddress") or {}).get("country"),
		fetch_total=_money(_fetch_total(payload)), payable=_money(payable_of(payload)))


def invoice_problems(inv: Invoice) -> List[str]:
	"""Every reason FETCH would reject this invoice, in plain English; [] when it can be sent."""
	problems = []
	if inv.is_debit_note:
		problems.append("Debit notes can't be sent to FETCH, which only accepts invoices and returns (credit notes).")
	if inv.is_return and inv.return_against and inv.return_against_date is None:
		problems.append("The posting date of {}, the invoice this return is against, is unknown.".format(inv.return_against))
	if inv.is_return and inv.return_against_date and inv.posting_date < inv.return_against_date:
		# CreditNoteService.cs:61-64
		problems.append("Posting Date {} is before {}, the date of the invoice this return is against.".format(
			inv.posting_date, inv.return_against_date))
	for index, tax in enumerate(inv.taxes):
		if tax.charge_type != "On Net Total":
			problems.append("Sales Taxes and Charges row {} is “{}”. FETCH needs every tax row to be “On Net Total”.".format(
				index + 1, tax.charge_type))
	for index, line in enumerate(inv.lines):
		if not inv.is_return and (line.qty < 0 or line.net_amount < 0):
			problems.append("{}: the amount is negative. Only a return can have negative amounts.".format(_row_label(inv, index)))
		if line.tax_category and line.tax_rate is None:
			problems.append("{}: FIRS Tax Category {} has no tax rate.".format(_row_label(inv, index), line.tax_category))

	payload = build_invoice_payload(inv)
	for field, message in _fetch_errors(payload):
		text = _explain(inv, payload, field, message)
		if text not in problems:
			problems.append(text)

	payable = _money(payable_of(payload))
	tolerance = max(CENT, HALF_CENT * (len(inv.lines) + len(inv.taxes)))
	if inv.lines and abs(payable - abs(inv.grand_total)) > tolerance:
		problems.append(
			"Grand Total {} doesn't match the items plus tax at their FIRS Tax Category rates ({}). "
			"Check that each item's FIRS Tax Category rate is the rate the invoice charges.".format(abs(inv.grand_total), payable))
	return problems


def normalise_phone(raw: Optional[str]) -> Optional[str]:
	"""+2348012345678 from common Nigerian spellings; None when it cannot be made to match ^\\+\\d{8,15}$."""
	number = re.sub(r"[\s\-().]", "", raw or "")
	if number.startswith("00"):
		number = "+" + number[2:]
	elif number.startswith("234"):
		number = "+" + number
	elif number.startswith("0"):
		number = "+234" + number[1:]
	if number.startswith("+2340"):
		number = "+234" + number[5:]
	return number if _PHONE.match(number) and number.isascii() else None


def next_payment(remaining: Decimal, amount: Decimal, erp_settled: bool) -> Optional[PaymentDecision]:
	"""What to send so FETCH's own PAID rule agrees (InvoicePaymentCaptureCore.cs:45) and amount <= BalanceDue (:267)."""
	remaining, amount = _money(remaining), _money(amount)
	if remaining <= 0:
		return None
	if erp_settled or amount >= remaining:
		return PaymentDecision(remaining, "PAID")
	return PaymentDecision(amount, "PARTIAL")


def build_payment_payload(irn: str, decision: PaymentDecision, reference: str, payment_date: date) -> dict:
	"""Body for POST /api/v1/invoices/add-payment (ErpPaymentStatusModels.cs:5-24)."""
	return {
		"irn": irn,
		"paymentStatus": decision.status,
		"amount": float(_money(decision.amount)),
		"paymentReference": reference,
		"paymentDate": payment_date.isoformat(),
	}


SIGNATURE_WINDOW_SECONDS = 300


def verify_signature(raw_body: bytes, timestamp: Optional[str], signature: Optional[str], secret: str, now: int) -> bool:
	"""Checks X-Fetch-Signature as ErpCallbackClient.Sign (ErpCallbackClient.cs:103-107) makes it."""
	ts = (timestamp or "").strip()
	if not (ts.isdigit() and ts.isascii() and signature and secret):
		return False
	if abs(now - int(ts)) > SIGNATURE_WINDOW_SECONDS:
		return False
	digest = hmac.new(secret.encode("utf-8"), ts.encode("ascii") + b"." + raw_body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(("sha256=" + digest).encode("ascii"), signature.strip().encode("utf-8"))


def _error_text(body: Optional[str]) -> str:
	try:
		data = json.loads(body or "")
	except ValueError:
		data = None
	errors = data.get("errors") if isinstance(data, dict) else None
	if isinstance(errors, list) and errors:
		return "; ".join(
			"{}: {}".format(e.get("field"), e.get("message")) if isinstance(e, dict) else str(e) for e in errors)
	if isinstance(errors, dict) and errors:
		return "; ".join("{}: {}".format(k, " ".join(map(str, v if isinstance(v, list) else [v]))) for k, v in errors.items())
	if isinstance(data, dict) and data.get("message"):
		return str(data["message"])
	return _clip(" ".join((body or "").split()), 300) or "no details"


def classify_response(kind: str, status: Optional[int], body: Optional[str]) -> Tuple[Outcome, str]:
	if status is None:
		return Outcome.RETRY, "FETCH did not respond (timeout or connection error)."
	if status in (200, 202):
		return Outcome.ACCEPTED, "Accepted by FETCH (HTTP {}).".format(status)
	detail = _error_text(body)
	# InvoiceCreationService.cs:55 and CreditNoteService.cs:189: an earlier attempt that timed out was stored.
	if kind == "Invoice" and status == 400 and "already exists" in (body or "").lower():
		return Outcome.ACCEPTED, "FETCH already has this document from an earlier attempt: {}".format(detail)
	# Invoice 409 is only "Return capacity changed" (ThirdPartyInvoiceTransmissionService.cs:79-82).
	if status in (408, 429) or status >= 500 or (kind == "Invoice" and status == 409):
		return Outcome.RETRY, "FETCH returned HTTP {}: {}".format(status, detail)
	return Outcome.FAILED, "FETCH rejected the {} (HTTP {}): {}".format(kind.lower(), status, detail)


def retry_delay_minutes(attempts: int) -> Optional[int]:
	if attempts >= MAX_ATTEMPTS:
		return None
	return RETRY_DELAYS_MINUTES[max(attempts, 1) - 1]

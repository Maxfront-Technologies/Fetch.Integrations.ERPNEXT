import ast
import dataclasses
import json
import os
import unittest
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fetch_einvoice import core

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
GOOD = ("standard", "item_discount", "invoice_discount", "mixed_rates", "full_return", "partial_return", "pos_paid", "advance")
CENT = Decimal("0.01")
ZERO = Decimal("0")
MISMATCH = "Credit Note values do not match the original return allocation."


def _decimal(value):
	return None if value is None else Decimal(value)


def _date(value):
	return None if value is None else date.fromisoformat(value)


def load_invoice(name):
	"""core.Invoice from tests/fixtures/<name>.json, where amounts are strings and dates are YYYY-MM-DD."""
	with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
		raw = json.load(f)
	return core.Invoice(**dict(
		raw,
		posting_date=_date(raw["posting_date"]),
		due_date=_date(raw["due_date"]),
		return_against_date=_date(raw["return_against_date"]),
		grand_total=Decimal(raw["grand_total"]),
		outstanding_amount=Decimal(raw["outstanding_amount"]),
		payment_mode_types=tuple(raw["payment_mode_types"]),
		customer=core.Party(**raw["customer"]),
		address=core.Address(**raw["address"]) if raw["address"] else None,
		taxes=tuple(core.TaxRow(**dict(t, rate=Decimal(t["rate"]))) for t in raw["taxes"]),
		lines=tuple(core.Line(**dict(
			line, qty=Decimal(line["qty"]), net_amount=Decimal(line["net_amount"]), category_rate=_decimal(line["category_rate"]),
			item_tax_rates={account: Decimal(rate) for account, rate in line["item_tax_rates"].items()}))
			for line in raw["lines"]),
	))


def payload(name):
	return core.build_invoice_payload(load_invoice(name))


def replace_line(inv, index, **changes):
	lines = list(inv.lines)
	lines[index] = dataclasses.replace(lines[index], **changes)
	return dataclasses.replace(inv, lines=tuple(lines))


def money(value):
	return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def dec(value):
	return core._dec(value)


def fetch_sources(original):
	"""Per-line values FETCH freezes for an accepted ERP invoice, keyed by externalLineReference.

	ErpMasterDataUpserter.cs:71-101 and :109-126, InvoiceCreationService.cs:175-187,
	InvoiceHelper.ComputeTaxTotal (InvoiceHelper.cs:133-144) and CreditNoteRules.Freeze (CreditNoteRules.cs:129-132).
	"""
	sources = {}
	for line in original["invoiceLine"]:
		qty = line["invoicedQuantity"]
		rate = dec(line["taxRate"])
		gross = money(dec(line["price"]["priceAmount"]) * qty)
		net = money(gross / (1 + rate / 100))
		sources[line["externalLineReference"].strip()] = {
			"quantity": qty, "gross": money(net / qty * qty), "discount": ZERO, "charge": ZERO, "tax": gross - net,
			"rate": rate, "category": line["taxCategory"], "hsn": line["hsnCode"], "sku": line["item"]["sellersItemIdentification"],
		}
	return sources


def fetch_credit(credit, sources, reserved=()):
	"""(first error or None, allocations) from FETCH's ERP credit-note path once the original is loaded.

	CreditNoteService.cs:72-107, CreditNoteRules.Allocate (CreditNoteRules.cs:157-203), CreditNoteService.Reconcile (:509-602).
	"""
	errors = core._fetch_errors(credit)
	if errors:
		return errors[0], []
	request = core._normalise_credit(credit)
	allocations = []
	for line in request["invoiceLine"]:
		reference = line.get("originalExternalLineReference")
		if reference is not None:
			source = sources.get(reference.strip())
			if source is None:
				return ("invoiceLine.originalExternalLineReference", "ERP row reference was not found on the original invoice."), []
		else:
			matches = [s for s in sources.values() if s["sku"] == line["item"]["sellersItemIdentification"]]
			if len(matches) != 1:
				return ("invoiceLine.originalInvoiceLineId", "Return item is missing or ambiguous."), []
			source = matches[0]
		if line["hsnCode"] != source["hsn"] or line["item"]["sellersItemIdentification"] != source["sku"]:
			return ("invoiceLine", "Return item identifiers do not match the selected original line."), []
		quantity = line["quantity"]
		prior = [r for r in reserved if r["source"] is source]
		used = sum(r["quantity"] for r in prior)
		if quantity <= 0 or quantity > source["quantity"] - used:
			return ("invoiceLine", "Return quantity exceeds the remaining uncredited quantity or is not positive."), []
		final = used + quantity == source["quantity"]
		left = {k: source[k] - sum((r[k] for r in prior), ZERO) for k in ("gross", "discount", "charge", "tax")}
		allocation = {"source": source, "quantity": quantity}
		for k in left:
			allocation[k] = left[k] if final else money(source[k] * quantity / source["quantity"])
		if any(allocation[k] < 0 or allocation[k] > left[k] for k in left) or \
				allocation["gross"] - allocation["discount"] > left["gross"] - left["discount"]:
			return ("invoiceLine", "This partial quantity cannot be allocated within the remaining value at currency precision."), []
		allocations.append(allocation)

	def unequal(received, expected):
		return abs(dec(received) - expected) > CENT

	for i, (line, a) in enumerate(zip(request["invoiceLine"], allocations)):
		source, net, field = a["source"], a["gross"] - a["discount"], "invoiceLine[{}].".format(i)
		if unequal(line["unitPrice"], source["gross"] / source["quantity"]):
			return (field + "unitPrice", MISMATCH), []
		if unequal(line["lineExtensionAmount"], net):
			return (field + "lineExtensionAmount", MISMATCH), []
		if line["price"].get("priceAmount") is not None and unequal(line["price"]["priceAmount"], dec(line["unitPrice"])):
			return (field + "price.priceAmount", MISMATCH), []
		if unequal(line["taxRate"], source["rate"]):
			return (field + "taxRate", MISMATCH), []
		if (line.get("taxCategory") or "").lower() != source["category"].lower():
			return (field + "taxCategory", "Tax category must match the original line."), []
		if dec(line.get("feeAmount")) != 0 or dec(line.get("feeRate")) != 0:
			return (field + "feeAmount", "Return original allocated charges through allowanceCharge."), []
		rate, value = dec(line.get("discountRate")), dec(line.get("discountValue"))
		percent = (line.get("discountType") or "").lower() == "percent"
		amount = dec(line.get("discountAmount"))
		discount = amount if amount != 0 else money(a["gross"] * rate / 100) if rate != 0 \
			else money(a["gross"] * value / 100) if percent else value
		if unequal(discount, a["discount"]):
			return (field + "discountAmount", MISMATCH), []

	charge = sum((a["charge"] for a in allocations), ZERO)
	if not request.get("allowanceCharge") and charge != 0:
		return ("allowanceCharge", "Include the allocated original charges in allowanceCharge."), []
	nets = sum((a["gross"] - a["discount"] for a in allocations), ZERO)
	taxes = sum((a["tax"] for a in allocations), ZERO)
	totals = request.get("legalMonetaryTotal")
	if totals is not None:
		for field, expected in (("payableAmount", nets + charge + taxes), ("taxInclusiveAmount", nets + charge + taxes),
				("taxExclusiveAmount", nets + charge), ("lineExtensionAmount", nets)):
			if unequal(totals[field], expected):
				return ("legalMonetaryTotal." + field, MISMATCH), []
	tax_total = request.get("taxTotal") or []
	if tax_total:
		if unequal(sum(dec(t["taxAmount"]) for t in tax_total), taxes):
			return ("taxTotal", MISMATCH), []
		parts = [p for t in tax_total for p in (t.get("taxSubtotal") or [])]
		if parts:
			remaining = list(allocations)
			for part in parts:
				category = part.get("taxCategory") or {}
				match = next((a for a in remaining if part.get("taxableAmount") is not None and part.get("taxAmount") is not None
					and not unequal(part["taxableAmount"], a["gross"] - a["discount"] + a["charge"])
					and not unequal(part["taxAmount"], a["tax"])
					and category.get("percent") is not None and dec(category["percent"]) == a["source"]["rate"]
					and (category.get("id") or "").lower() == a["source"]["category"].lower()), None)
				if match is None:
					return ("taxTotal.taxSubtotal", "Tax subtotals must match the original return allocations."), []
				remaining.remove(match)
			if any(a["tax"] != 0 for a in remaining):
				return ("taxTotal.taxSubtotal", "Tax subtotals omit returned taxed lines."), []
	return None, allocations


def as_return(original, name, rows):
	"""A return of original's rows {row index: quantity returned}, shaped as ERPNext's Make Return leaves it."""
	lines = []
	for index, qty in rows.items():
		line = original.lines[index]
		share = money(line.net_amount * qty / line.qty)
		lines.append(dataclasses.replace(
			line, row_name="{}-{}".format(name, index), qty=-Decimal(qty), net_amount=-share, original_row_name=line.row_name))
	gross = sum(money(abs(l.net_amount) * (1 + core.charged_rate(l, original.taxes) / 100)) for l in lines)
	return dataclasses.replace(
		original, name=name, is_return=True, return_against=original.name, return_against_date=original.posting_date,
		posting_date=original.posting_date, lines=tuple(lines), grand_total=-gross, outstanding_amount=-gross,
		payment_mode_types=())


class InvoicePayloadTests(unittest.TestCase):
	def test_lines_carry_tax_inside_the_price(self):
		p = payload("standard")
		widget, install = p["invoiceLine"]
		self.assertEqual(widget["unitPrice"], 1075.0)
		self.assertEqual(widget["price"]["priceAmount"], 1075.0)
		self.assertEqual(widget["lineExtensionAmount"], 2150.0)
		self.assertEqual(install["lineExtensionAmount"], 1000.0)
		self.assertEqual(p["legalMonetaryTotal"], {
			"lineExtensionAmount": 2930.23, "taxExclusiveAmount": 2930.23, "taxInclusiveAmount": 3150.0, "payableAmount": 3150.0})
		self.assertEqual(p["taxTotal"][0]["taxAmount"], 219.77)
		self.assertEqual(core._fetch_errors(p), [])

	def test_quantities_are_json_integers(self):
		for name in GOOD:
			for line in payload(name)["invoiceLine"]:
				self.assertIs(type(line["quantity"]), int, name)
				self.assertEqual(line["quantity"], line["invoicedQuantity"])
				self.assertGreater(line["quantity"], 0)

	def test_fetch_total_rule_rejects_tax_added_on_top(self):
		p = payload("standard")
		for line in p["invoiceLine"]:
			line["unitPrice"] = line["price"]["priceAmount"] = line["unitPrice"] / 1.075
		self.assertIn(("legalMonetaryTotal", "Total amount does not match the tax-inclusive line item totals."), core._fetch_errors(p))

	def test_header_customer_and_item_fields(self):
		p = payload("standard")
		self.assertEqual(
			{k: p[k] for k in ("irn", "issueDate", "issueTime", "dueDate", "invoiceTypeCode", "paymentStatus", "note",
				"documentCurrencyCode", "taxCurrencyCode", "deposit", "paymentMeans")},
			{"irn": "ACC-SINV-2026-00001", "issueDate": "2026-10-19", "issueTime": "10:15:30", "dueDate": "2026-11-18",
				"invoiceTypeCode": "381", "paymentStatus": "PENDING", "note": "Thank you for your business.",
				"documentCurrencyCode": "NGN", "taxCurrencyCode": "NGN", "deposit": 0.0,
				"paymentMeans": [{"paymentMeansCode": "30", "paymentDueDate": "2026-11-18"}]})
		self.assertNotIn("billingReference", p)
		self.assertEqual(p["accountingCustomerParty"], {
			"partyName": "Adaeze Stores Ltd", "tin": "23456789-0001", "email": "accounts@adaeze.example",
			"telephone": "+2348031234567", "businessDescription": "Retail of household goods",
			"postalAddress": {"streetName": "12 Marina Road, Suite 4", "cityName": "Lagos", "postalZone": "101233", "country": "NG"}})
		widget = p["invoiceLine"][0]
		self.assertEqual(widget["externalLineReference"], "c1a8f0e2d1")
		self.assertNotIn("originalExternalLineReference", widget)
		self.assertEqual(widget["item"], {
			"name": "Steel Widget", "description": "Steel widget, zinc plated & boxed", "sellersItemIdentification": "WIDGET-01"})
		self.assertEqual(widget["price"], {"priceAmount": 1075.0, "baseQuantity": 1, "priceUnit": "EA"})
		self.assertEqual((widget["hsnCode"], widget["taxCategory"], widget["taxRate"]), ("8479.89", "STANDARD_VAT", 7.5))

	def test_no_line_is_sent_as_a_stock_checked_product(self):
		# A Product line with no FETCH stock gets 400 "Insufficient stock for Steel Widget. Available stock is 0."
		for name in GOOD:
			self.assertEqual({l["itemType"] for l in payload(name)["invoiceLine"]}, {"Service"}, name)

	def test_posting_time_without_leading_zero(self):
		inv = dataclasses.replace(load_invoice("standard"), posting_time="9:05:03")
		self.assertEqual(core.build_invoice_payload(inv)["issueTime"], "09:05:03")

	def test_item_discount_is_not_applied_twice(self):
		line = payload("item_discount")["invoiceLine"][0]
		self.assertEqual((line["unitPrice"], line["lineExtensionAmount"]), (967.5, 2902.5))
		self.assertEqual((line["discountType"], line["discountValue"], line["discountAmount"], line["discountRate"]), ("Flat", 0, 0, 0))
		self.assertEqual(payload("item_discount")["paymentMeans"][0]["paymentMeansCode"], "97")

	def test_invoice_level_discount_comes_from_net_amount(self):
		p = payload("invoice_discount")
		self.assertEqual([l["lineExtensionAmount"] for l in p["invoiceLine"]], [967.5, 483.75])
		self.assertEqual(p["legalMonetaryTotal"]["payableAmount"], 1451.25)
		self.assertEqual(p["paymentMeans"][0]["paymentMeansCode"], "30")

	def test_mixed_rates_group_tax_by_category_and_rate(self):
		p = payload("mixed_rates")
		self.assertEqual(p["taxTotal"], [{"taxAmount": 90.0, "taxSubtotal": [
			{"taxableAmount": 1200.0, "taxAmount": 90.0, "taxCategory": {"id": "STANDARD_VAT", "percent": 7.5}},
			{"taxableAmount": 500.0, "taxAmount": 0.0, "taxCategory": {"id": "ZERO_RATED", "percent": 0.0}}]}])
		self.assertEqual(p["legalMonetaryTotal"]["payableAmount"], 1790.0)

	def test_item_tax_template_rate_replaces_the_invoice_rate(self):
		# mixed_rates charges 7.5%, but the manual's Item Tax Template sets VAT - MX to 0%.
		self.assertEqual([(l["taxCategory"], l["taxRate"]) for l in payload("mixed_rates")["invoiceLine"]],
			[("STANDARD_VAT", 7.5), ("ZERO_RATED", 0.0), ("STANDARD_VAT", 7.5)])

	def test_tax_rate_follows_the_invoice_not_the_category(self):
		inv = dataclasses.replace(load_invoice("standard"), taxes=(core.TaxRow("On Net Total", Decimal("5"), False, "VAT - MX"),))
		p = core.build_invoice_payload(inv)
		self.assertEqual([(l["taxCategory"], l["taxRate"]) for l in p["invoiceLine"]], [("STANDARD_VAT", 5.0), ("STANDARD_VAT", 5.0)])
		self.assertEqual(p["taxTotal"][0]["taxSubtotal"][0]["taxCategory"], {"id": "STANDARD_VAT", "percent": 5.0})
		self.assertEqual(p["legalMonetaryTotal"]["payableAmount"], 3076.74)

	def test_on_net_total_rows_add_up_and_templates_replace_one_account(self):
		inv = dataclasses.replace(load_invoice("standard"), taxes=(
			core.TaxRow("On Net Total", Decimal("5"), False, "VAT - MX"), core.TaxRow("On Net Total", Decimal("2.5"), False, "Levy - MX")))
		self.assertEqual([l["taxRate"] for l in core.build_invoice_payload(inv)["invoiceLine"]], [7.5, 7.5])
		self.assertEqual(core.invoice_problems(inv), [])
		no_levy = replace_line(inv, 0, item_tax_rates={"Levy - MX": Decimal("0")})
		self.assertEqual([l["taxRate"] for l in core.build_invoice_payload(no_levy)["invoiceLine"]], [5.0, 7.5])

	def test_pos_paid_in_full(self):
		p = payload("pos_paid")
		self.assertEqual((p["deposit"], p["paymentStatus"], p["paymentMeans"][0]["paymentMeansCode"]), (537.5, "PAID", "10"))
		self.assertEqual(core.deposit_of(p), core.payable_of(p))

	def test_advance_becomes_the_deposit(self):
		p = payload("advance")
		self.assertEqual((p["deposit"], p["paymentStatus"], p["paymentMeans"][0]["paymentMeansCode"]), (4000.0, "PARTIAL", "30"))
		self.assertEqual((core.payable_of(p), core.deposit_of(p)), (Decimal("10750.0"), Decimal("4000.0")))

	def test_deposit_never_exceeds_the_total(self):
		inv = dataclasses.replace(load_invoice("pos_paid"), outstanding_amount=Decimal("-20"))
		p = core.build_invoice_payload(inv)
		self.assertEqual((p["deposit"], p["paymentStatus"]), (537.5, "PAID"))

	def test_unpaid_invoice_rounded_down_is_not_a_payment(self):
		inv = dataclasses.replace(
			load_invoice("standard"), rounded_total=Decimal("3149.60"), outstanding_amount=Decimal("3149.60"))
		p = core.build_invoice_payload(inv)
		self.assertEqual((p["deposit"], p["paymentStatus"]), (0.0, "PENDING"))

	def test_unpaid_invoice_rounded_up_is_not_a_payment(self):
		inv = dataclasses.replace(
			load_invoice("standard"), rounded_total=Decimal("3150.40"), outstanding_amount=Decimal("3150.40"))
		p = core.build_invoice_payload(inv)
		self.assertEqual((p["deposit"], p["paymentStatus"]), (0.0, "PENDING"))

	def test_advance_on_a_rounded_invoice_is_the_deposit(self):
		inv = dataclasses.replace(
			load_invoice("standard"), rounded_total=Decimal("3149.60"), outstanding_amount=Decimal("2149.60"))
		p = core.build_invoice_payload(inv)
		self.assertEqual((p["deposit"], p["paymentStatus"]), (1000.0, "PARTIAL"))

	def test_paid_rounded_invoice_leaves_nothing_in_fetch(self):
		inv = dataclasses.replace(load_invoice("standard"), rounded_total=Decimal("3149.60"), outstanding_amount=ZERO)
		p = core.build_invoice_payload(inv)
		self.assertEqual((core.deposit_of(p), p["paymentStatus"]), (core.payable_of(p), "PAID"))

	def test_every_good_fixture_passes_fetch_rules(self):
		for name in GOOD:
			self.assertEqual(core.invoice_problems(load_invoice(name)), [], name)
			self.assertEqual(core._fetch_errors(payload(name)), [], name)

	def test_payload_is_deterministic_json(self):
		for name in GOOD:
			first = json.dumps(payload(name), sort_keys=True, allow_nan=False)
			self.assertEqual(first, json.dumps(payload(name), sort_keys=True, allow_nan=False))

	def test_long_text_is_clipped_to_fetch_columns(self):
		inv = replace_line(load_invoice("standard"), 0, description="<p>{}</p>".format("x" * 400), item_name="n" * 140)
		inv = dataclasses.replace(inv, remarks="r" * 600)
		p = core.build_invoice_payload(inv)
		item = p["invoiceLine"][0]["item"]
		self.assertEqual((len(item["description"]), len(item["name"]), len(p["note"])), (250, 100, 500))
		self.assertTrue(item["description"].endswith("…"))

	def test_missing_address_is_sent_as_null(self):
		p = core.build_invoice_payload(dataclasses.replace(load_invoice("standard"), address=None))
		self.assertIsNone(p["accountingCustomerParty"]["postalAddress"])


class CreditNoteTests(unittest.TestCase):
	def test_return_is_a_380_against_the_original(self):
		p = payload("full_return")
		self.assertEqual(p["invoiceTypeCode"], "380")
		self.assertEqual(p["billingReference"], [{"invoiceNumber": "ACC-SINV-2026-00001", "issueDate": "2026-10-19"}])
		self.assertEqual((p["deposit"], p["paymentStatus"]), (0.0, "PENDING"))
		self.assertEqual([l["quantity"] for l in p["invoiceLine"]], [2, 1])

	def test_return_lines_point_at_the_original_rows(self):
		original = {l["externalLineReference"] for l in payload("standard")["invoiceLine"]}
		for name in ("full_return", "partial_return"):
			refs = [l["originalExternalLineReference"] for l in payload(name)["invoiceLine"]]
			self.assertTrue(set(refs) <= original, name)
			self.assertNotIn(refs[0], [l["externalLineReference"] for l in payload(name)["invoiceLine"]])

	def test_credit_lines_are_tax_exclusive_with_tax_on_top(self):
		p = payload("full_return")
		self.assertEqual([(l["unitPrice"], l["price"]["priceAmount"], l["lineExtensionAmount"]) for l in p["invoiceLine"]],
			[(1000.0, 1000.0, 2000.0), (930.23, 930.23, 930.23)])
		self.assertEqual(p["legalMonetaryTotal"], {
			"lineExtensionAmount": 2930.23, "taxExclusiveAmount": 2930.23, "taxInclusiveAmount": 3150.0, "payableAmount": 3150.0})

	def test_full_return_reconciles_against_its_original(self):
		error, allocations = fetch_credit(payload("full_return"), fetch_sources(payload("standard")))
		self.assertIsNone(error)
		self.assertEqual(sum(a["gross"] + a["tax"] for a in allocations), Decimal("3150.00"))

	def test_partial_returns_reconcile_up_to_the_final_one(self):
		sources = fetch_sources(payload("standard"))
		error, first = fetch_credit(payload("partial_return"), sources)
		self.assertIsNone(error)
		rest = as_return(load_invoice("standard"), "ACC-SINV-2026-00012", {0: 1, 1: 1})
		error, second = fetch_credit(core.build_invoice_payload(rest), sources, first)
		self.assertIsNone(error)
		again, _ = fetch_credit(core.build_invoice_payload(rest), sources, first + second)
		self.assertEqual(again[0], "invoiceLine")

	def test_three_way_partial_split_reconciles(self):
		original = replace_line(load_invoice("standard"), 1, qty=Decimal("3"), net_amount=Decimal("100.00"))
		original = dataclasses.replace(original, lines=original.lines[1:])
		sources = fetch_sources(core.build_invoice_payload(original))
		reserved = []
		for n in range(3):
			error, allocations = fetch_credit(core.build_invoice_payload(as_return(original, "RET-{}".format(n), {0: 1})), sources, reserved)
			self.assertIsNone(error, n)
			reserved += allocations

	def test_same_rate_rows_need_one_tax_subtotal_each(self):
		original = load_invoice("mixed_rates")
		credit = core.build_invoice_payload(as_return(original, "ACC-SINV-2026-00013", {0: 4, 1: 1, 2: 2}))
		sources = fetch_sources(core.build_invoice_payload(original))
		self.assertEqual(len(credit["taxTotal"][0]["taxSubtotal"]), 3)
		self.assertEqual(fetch_credit(credit, sources)[0], None)
		credit["taxTotal"][0]["taxSubtotal"] = core.build_invoice_payload(original)["taxTotal"][0]["taxSubtotal"]
		self.assertEqual(fetch_credit(credit, sources)[0][0], "taxTotal.taxSubtotal")

	def test_tax_inclusive_credit_lines_are_rejected(self):
		credit = payload("full_return")
		for line in credit["invoiceLine"]:
			line["unitPrice"] = line["price"]["priceAmount"] = round(line["unitPrice"] * 1.075, 6)
		self.assertEqual(fetch_credit(credit, fetch_sources(payload("standard")))[0], ("invoiceLine[0].unitPrice", MISMATCH))

	def test_unknown_original_row_is_rejected(self):
		inv = replace_line(load_invoice("partial_return"), 0, original_row_name="not-on-the-original")
		error, _ = fetch_credit(core.build_invoice_payload(inv), fetch_sources(payload("standard")))
		self.assertEqual(error[0], "invoiceLine.originalExternalLineReference")


class ProblemTests(unittest.TestCase):
	def test_fractional_quantity_is_reported_not_truncated(self):
		inv = load_invoice("fractional_qty")
		self.assertEqual(core.invoice_problems(inv), [
			"Item row 1 (CABLE-05): quantity 1.5 isn't a whole number. FETCH only accepts whole quantities."])
		self.assertEqual(core.build_invoice_payload(inv)["invoiceLine"][0]["quantity"], 1.5)

	def test_bad_phone(self):
		self.assertEqual(core.invoice_problems(load_invoice("bad_phone")), [
			"Customer mobile number 12345 isn't valid. Use the international format, for example +2348033456789."])

	def test_debit_note_is_blocked(self):
		self.assertEqual(core.invoice_problems(load_invoice("debit_note")), [
			"Debit notes can't be sent to FETCH, which only accepts invoices and returns (credit notes)."])

	def test_every_problem_is_listed_at_once(self):
		inv = replace_line(load_invoice("standard"), 0, hsn_code=None, tax_category=None, category_rate=None)
		inv = dataclasses.replace(
			inv, currency="JPY", customer=dataclasses.replace(inv.customer, email="", phone=""), address=None,
			taxes=(core.TaxRow("Actual", Decimal("0"), False, "VAT - MX"),), grand_total=Decimal("2930.23"))
		self.assertEqual(core.invoice_problems(inv), [
			"Sales Taxes and Charges row 1 is “Actual”. FETCH needs every tax row to be “On Net Total”.",
			"Item row 2 (SVC-INSTALL): FIRS Tax Category STANDARD_VAT is 7.5%, but the invoice charges 0% on this item. "
			"Fix the rate in Sales Taxes and Charges or the Item Tax Template, or pick the FIRS Tax Category that matches.",
			"Currency JPY isn't accepted by FETCH. Use USD, NGN, GBP, EUR or CAD.",
			"Customer email is missing. Set Contact Email on the invoice, or Email Id on the Customer.",
			"Customer mobile number is missing. Set Mobile No on the invoice's contact.",
			"Customer Address is missing. Pick one on the invoice.",
			"Item row 1 (WIDGET-01): FIRS HSN Code is missing.",
			"Item row 1 (WIDGET-01): FIRS Tax Category is missing.",
		])

	def test_category_without_rate_is_not_checked(self):
		inv = replace_line(load_invoice("standard"), 1, tax_category="EXEMPTED", category_rate=None)
		self.assertEqual(core.invoice_problems(inv), [])

	def test_category_rate_that_differs_from_the_invoice_is_reported(self):
		# The invoice charges 5%, so its Grand Total matches its items and only the FIRS Tax Category is wrong.
		inv = dataclasses.replace(load_invoice("standard"), grand_total=Decimal("3076.74"),
			taxes=(core.TaxRow("On Net Total", Decimal("5.00"), False, "VAT - MX"),))
		advice = "Fix the rate in Sales Taxes and Charges or the Item Tax Template, or pick the FIRS Tax Category that matches."
		self.assertEqual(core.invoice_problems(inv), [
			"Item row 1 (WIDGET-01): FIRS Tax Category STANDARD_VAT is 7.5%, but the invoice charges 5% on this item. " + advice,
			"Item row 2 (SVC-INSTALL): FIRS Tax Category STANDARD_VAT is 7.5%, but the invoice charges 5% on this item. " + advice])

	def test_lowercase_tax_category_code(self):
		inv = replace_line(load_invoice("standard"), 0, tax_category="vat")
		self.assertEqual(core.invoice_problems(inv), [
			"Item row 1 (WIDGET-01): FIRS Tax Category code vat must use only capital letters and underscores, for example STANDARD_VAT."])

	def test_grand_total_tolerance_allows_rounding_per_line(self):
		# ERPNext rounds the tax once per tax row; the payload rounds each line, so ten 0.10 lines at 7.5% differ by up to 0.03.
		line = load_invoice("standard").lines[0]
		lines = tuple(dataclasses.replace(line, row_name="row-{}".format(i), qty=Decimal("1"), net_amount=Decimal("0.10"))
			for i in range(10))
		inv = dataclasses.replace(load_invoice("standard"), lines=lines, grand_total=Decimal("1.07"), outstanding_amount=Decimal("1.07"))
		self.assertGreater(core.payable_of(core.build_invoice_payload(inv)) - inv.grand_total, CENT)
		self.assertEqual(core.invoice_problems(inv), [])

	def test_grand_total_mismatch_is_reported(self):
		five_percent = dataclasses.replace(load_invoice("standard"), grand_total=Decimal("3076.74"))
		self.assertEqual(core.invoice_problems(five_percent), [
			"Grand Total 3076.74 doesn't match the items plus their tax (3150.00)."])
		two_cents = dataclasses.replace(load_invoice("bad_phone"), customer=load_invoice("standard").customer, grand_total=Decimal("1075.02"))
		self.assertEqual(len(core.invoice_problems(two_cents)), 1)

	def test_negative_row_on_an_invoice(self):
		inv = replace_line(load_invoice("standard"), 1, qty=Decimal("-1"), net_amount=Decimal("-930.23"))
		self.assertIn("Item row 2 (SVC-INSTALL): the amount is negative. Only a return can have negative amounts.",
			core.invoice_problems(inv))

	def test_return_dated_before_its_original(self):
		inv = dataclasses.replace(load_invoice("full_return"), posting_date=date(2026, 10, 18))
		self.assertEqual(core.invoice_problems(inv), [
			"Posting Date 2026-10-18 is before 2026-10-19, the date of the invoice this return is against."])

	def test_return_without_return_against(self):
		inv = dataclasses.replace(load_invoice("partial_return"), return_against=None, return_against_date=None)
		self.assertEqual(core.invoice_problems(inv), ["Return Against is missing. A return must name the invoice it returns."])

	def test_no_three_dot_ellipsis_in_core_strings(self):
		with open(core.__file__, encoding="utf-8") as f:
			nodes = ast.walk(ast.parse(f.read()))
		strings = [getattr(n, "value", getattr(n, "s", None)) for n in nodes if type(n).__name__ in ("Str", "Constant")]
		self.assertEqual([s for s in strings if isinstance(s, str) and "..." in s], [])


class PhoneTests(unittest.TestCase):
	def test_normalise_phone(self):
		cases = {
			"0803 123 4567": "+2348031234567",
			"+234 803 123 4567": "+2348031234567",
			"234-803-123-4567": "+2348031234567",
			"002348031234567": "+2348031234567",
			"+234 (0) 803 123 4567": "+2348031234567",
			"(0803) 123.4567": "+2348031234567",
			"+447911123456": "+447911123456",
			"12345": None,
			"8031234567": None,
			"": None,
			None: None,
			"0803-CALL-NOW": None,
			"08031234567 ext 2": None,
			"+1234567890123456": None,
			"+٢٣٤٨٠٣١٢٣٤٥٦٧": None,
		}
		for raw, expected in cases.items():
			self.assertEqual(core.normalise_phone(raw), expected, raw)


class PaymentTests(unittest.TestCase):
	def test_next_payment_table(self):
		D = Decimal
		self.assertIsNone(core.next_payment(D("0"), D("100"), False))
		self.assertIsNone(core.next_payment(D("-5"), D("100"), True))
		self.assertEqual(core.next_payment(D("500"), D("200"), False), core.PaymentDecision(D("200.00"), "PARTIAL"))
		self.assertEqual(core.next_payment(D("500"), D("500"), False), core.PaymentDecision(D("500.00"), "PAID"))
		self.assertEqual(core.next_payment(D("500"), D("600"), False), core.PaymentDecision(D("500.00"), "PAID"))
		self.assertEqual(core.next_payment(D("500"), D("100"), True), core.PaymentDecision(D("500.00"), "PAID"))

	def test_statuses_agree_with_fetch_paid_rule(self):
		# InvoicePaymentCaptureCore.cs:45 and ErpPaymentUpdateService.cs:86-94, 262-270.
		total, deposit = Decimal("1074.75"), Decimal("74.75")
		paid = Decimal("0")
		for amount, settled in ((Decimal("400"), False), (Decimal("250.50"), False), (Decimal("350"), True)):
			balance = total - deposit - paid
			decision = core.next_payment(balance, amount, settled)
			self.assertLessEqual(decision.amount, balance)
			paid += decision.amount
			self.assertEqual(decision.status, "PAID" if total - deposit - paid <= 0 else "PARTIAL")
		self.assertEqual(paid, total - deposit)
		self.assertIsNone(core.next_payment(total - deposit - paid, Decimal("1"), True))

	def test_payment_payload(self):
		body = core.build_payment_payload(
			"ACCSINV202600001AA11BB2220261019", core.PaymentDecision(Decimal("250.5"), "PARTIAL"), "ACC-PAY-2026-00001-1", date(2026, 10, 21))
		self.assertEqual(body, {"irn": "ACCSINV202600001AA11BB2220261019", "paymentStatus": "PARTIAL", "amount": 250.5,
			"paymentReference": "ACC-PAY-2026-00001-1", "paymentDate": "2026-10-21"})
		self.assertEqual(json.dumps(body), json.dumps(core.build_payment_payload(
			"ACCSINV202600001AA11BB2220261019", core.PaymentDecision(Decimal("250.50"), "PARTIAL"), "ACC-PAY-2026-00001-1", date(2026, 10, 21))))


class SignatureTests(unittest.TestCase):
	# Vectors from .NET 10 HMACSHA256.HashData, the call ErpCallbackClient.Sign (ErpCallbackClient.cs:103-107) makes.
	SECRET = "whsec_7d1f0c9e4b2a"
	TS = "1760954400"
	BODY = ('{"eventId":"7f9c2b1e-5d4a-4c3b-9a8e-1f2d3c4b5a69","eventType":"invoice.transmitted","schemaVersion":1,'
		'"environment":"Sandbox","invoiceId":"3fa85f64-5717-4562-b3fc-2c963f66afa6","invoiceNumber":"ACC-SINV-2026-00001",'
		'"irn":"ACCSINV202600001AA11BB2220261019","transmissionStatus":"Sent","customer":"Adaéze Stores"}').encode("utf-8")
	SIGNATURE = "sha256=6d993501f77e515a56e7eeeaf69a190006962e56b6352819bd8378e368b3fe55"

	def verify(self, body=BODY, ts=TS, signature=SIGNATURE, secret=SECRET, now=1760954400):
		return core.verify_signature(body, ts, signature, secret, now)

	def test_fetch_signature_verifies(self):
		self.assertTrue(self.verify())
		self.assertTrue(core.verify_signature(
			b"{}", self.TS, "sha256=0ffd84554d4b414be91bef110a1a744db4e025c400df6ebaff618ed40aff1456", "s3crét", 1760954400))

	def test_window_is_five_minutes_each_way(self):
		self.assertTrue(self.verify(now=1760954400 + 300))
		self.assertTrue(self.verify(now=1760954400 - 300))
		self.assertFalse(self.verify(now=1760954400 + 301))
		self.assertFalse(self.verify(now=1760954400 - 301))

	def test_anything_altered_fails(self):
		self.assertFalse(self.verify(body=self.BODY.replace(b"Sent", b"Sant")))
		self.assertFalse(self.verify(ts="1760954401", now=1760954401))
		self.assertFalse(self.verify(secret="whsec_other"))
		self.assertFalse(self.verify(signature=self.SIGNATURE[:-1] + "4"))
		self.assertFalse(self.verify(signature=self.SIGNATURE[len("sha256="):]))

	def test_missing_or_malformed_headers_fail(self):
		for ts, signature, secret in ((None, self.SIGNATURE, self.SECRET), ("", self.SIGNATURE, self.SECRET),
				("abc", self.SIGNATURE, self.SECRET), ("-1", self.SIGNATURE, self.SECRET), (self.TS, None, self.SECRET),
				(self.TS, "", self.SECRET), (self.TS, "sha256=é", self.SECRET), (self.TS, self.SIGNATURE, "")):
			self.assertFalse(core.verify_signature(self.BODY, ts, signature, secret, 1760954400), (ts, signature, secret))


class ResponseTests(unittest.TestCase):
	REPEATED = '{"status":"error","errors":[{"field":"invoice","message":"Invoice with invoice number - ERP-TWICE - already exists."}]}'
	CREDIT_REPEATED = '{"status":"error","errors":[{"field":"irn","message":"An invoice with this document number already exists."}]}'

	def outcome(self, kind, status, body=None):
		return core.classify_response(kind, status, body)[0]

	def test_success(self):
		self.assertEqual(core.classify_response("Invoice", 202, ""), (core.Outcome.ACCEPTED, "Accepted by FETCH (HTTP 202)."))
		self.assertEqual(self.outcome("Payment", 200, '{"outstandingBalance":0}'), core.Outcome.ACCEPTED)
		self.assertEqual(self.outcome("Payment", 202, '{"outstandingBalance":10}'), core.Outcome.ACCEPTED)

	def test_duplicate_invoice_means_an_earlier_attempt_landed(self):
		for body in (self.REPEATED, self.CREDIT_REPEATED):
			outcome, message = core.classify_response("Invoice", 400, body)
			self.assertEqual(outcome, core.Outcome.ACCEPTED, body)
			self.assertIn("already exists", message)
		self.assertEqual(self.outcome("Payment", 400, self.REPEATED), core.Outcome.FAILED)

	def test_validation_errors_fail_with_the_fields(self):
		body = json.dumps({"status": "error", "errors": [
			{"field": "legalMonetaryTotal", "message": "Total amount does not match the tax-inclusive line item totals."},
			{"field": "accountingCustomerParty.tin", "message": "Customer TIN is required."}]})
		self.assertEqual(core.classify_response("Invoice", 400, body), (core.Outcome.FAILED,
			"FETCH rejected the invoice (HTTP 400): legalMonetaryTotal: Total amount does not match the tax-inclusive line item totals.; "
			"accountingCustomerParty.tin: Customer TIN is required."))

	def test_permanent_failures(self):
		entitlement = '{"status":"error","errors":[{"field":"entitlement","message":"Transmission limit reached."}]}'
		for kind, status, body in (("Invoice", 401, ""), ("Invoice", 403, entitlement), ("Payment", 404, '{"errors":[{"field":"irn","message":"Invoice was not found."}]}'),
				("Payment", 409, '{"errors":[{"field":"paymentStatus","message":"Requested payment status is inconsistent with the resulting balance. Expected PARTIAL."}]}'),
				("Payment", 400, ""), ("Invoice", 302, ""), ("Invoice", 422, None)):
			self.assertEqual(self.outcome(kind, status, body), core.Outcome.FAILED, (kind, status))

	def test_transient_failures_retry(self):
		capacity = '{"status":"error","errors":[{"field":"invoiceLine","message":"Return capacity changed. Reload the original invoice and retry."}]}'
		queue = '{"status":"error","errors":[{"field":"queue","message":"Credit Note could not be queued. Check its status before retrying."}]}'
		for kind, status, body in (("Invoice", 409, capacity), ("Invoice", 503, queue), ("Invoice", 500, "<html>oops</html>"),
				("Payment", 500, ""), ("Invoice", 429, ""), ("Payment", 408, ""), ("Invoice", None, None), ("Payment", None, None)):
			self.assertEqual(self.outcome(kind, status, body), core.Outcome.RETRY, (kind, status))
		self.assertIn("Return capacity changed", core.classify_response("Invoice", 409, capacity)[1])

	def test_unstructured_bodies_still_explain(self):
		# What FETCH returned for the fractional_qty fixture.
		binder = ('{"statusCode":400,"message":"One or more errors occurred!","errors":{"invoiceLine[0].quantity":'
			'["Either the JSON value is not in a supported format, or is out of bounds for an Int32."]}}')
		self.assertEqual(core.classify_response("Invoice", 400, binder), (core.Outcome.FAILED,
			"FETCH rejected the invoice (HTTP 400): invoiceLine[0].quantity: "
			"Either the JSON value is not in a supported format, or is out of bounds for an Int32."))
		self.assertIn("oops", core.classify_response("Invoice", 502, "<html>oops</html>")[1])

	def test_retry_schedule(self):
		self.assertEqual([core.retry_delay_minutes(n) for n in range(1, 8)], [1, 5, 15, 60, 240, None, None])
		self.assertEqual(core.MAX_ATTEMPTS, len(core.RETRY_DELAYS_MINUTES) + 1)


def pins_line(name, quantity, price):
	"""ErpTransmitPins.Line (ErpTransmitPins.cs:257-287), serialised the way PostAsJsonAsync sends it."""
	return {"quantity": quantity, "invoicedQuantity": quantity, "unitPrice": price, "discountAmount": 0, "discountRate": 0,
		"discountType": None, "discountValue": 0, "taxRate": 7.5, "taxCategory": "STANDARD_VAT", "hsnCode": "HSN-{}".format(len(name)),
		"productCategory": "ERP Goods", "itemType": "Service", "feeRate": 0, "feeAmount": 0, "lineExtensionAmount": 0,
		"externalLineReference": None, "originalExternalLineReference": None, "originalInvoiceLineId": None,
		"item": {"name": name, "description": name + " description", "sellersItemIdentification": "SKU-" + name},
		"price": {"priceAmount": price, "priceUnit": "EA", "baseQuantity": 1}}


def pins_payload(irn, lines):
	"""ErpTransmitPins.Payload (ErpTransmitPins.cs:289-319)."""
	return {"irn": irn, "issueDate": "2026-03-02", "dueDate": "2026-04-01", "invoiceTypeCode": "381", "paymentStatus": "pending",
		"documentCurrencyCode": "NGN", "taxCurrencyCode": "ngn", "deposit": None,
		"paymentMeans": [{"paymentMeansCode": "30", "paymentDueDate": "2026-04-01"}],
		"accountingCustomerParty": {"tin": "ERP-TIN", "email": "erp-buyer@example.com", "telephone": "+2348012340000",
			"partyName": "ERP Buyer", "postalAddress": {"streetName": "1 ERP Road", "cityName": "Lagos", "country": "NG"}},
		"invoiceLine": lines, "legalMonetaryTotal": None}


class FetchValidatorPortTests(unittest.TestCase):
	def test_matches_fetch_snapshot_rule_for_rule(self):
		# The request ErpTransmitPins.Every_validation_rule_reports_its_field_and_message (ErpTransmitPins.cs:23-76) sends.
		bad = pins_line("Bad", 0, 100)
		bad.update({"unitPrice": -1, "taxRate": 101, "discountValue": -1, "discountType": "Half", "taxCategory": "vat",
			"hsnCode": " ", "productCategory": "", "discountRate": 101, "externalLineReference": " ", "originalExternalLineReference": "ORIG"})
		bad["item"].update({"name": "", "description": ""})
		bad["price"]["priceUnit"] = ""
		duplicate, twin = pins_line("Dup", 1, 10), pins_line("Twin", 1, 10)
		duplicate["externalLineReference"] = twin["externalLineReference"] = "ROW-1"
		request = pins_payload(" ", [bad, duplicate, twin])
		request.update({"issueDate": "2026-03-02", "dueDate": "2026-03-01", "invoiceTypeCode": "", "paymentStatus": "LATE",
			"documentCurrencyCode": "JPY", "paymentMeans": [{"paymentMeansCode": "77", "paymentDueDate": "2026-03-02"}], "deposit": -5,
			"accountingCustomerParty": {"tin": "", "email": "not-an-email", "telephone": "080", "partyName": "",
				"postalAddress": {"streetName": None, "cityName": None, "postalZone": None, "country": "NGA"}},
			"legalMonetaryTotal": {"lineExtensionAmount": 0, "taxExclusiveAmount": 0, "taxInclusiveAmount": 0, "payableAmount": 999}})
		with open(os.path.join(FIXTURES, "fetch-validation-errors.json"), encoding="utf-8") as f:
			expected = [(e["field"], e["message"]) for e in json.load(f)["errors"]]
		self.assertEqual(core._fetch_errors(request), expected)

	def test_matches_fetch_snapshot_for_missing_objects(self):
		# ErpTransmitPins.Missing_customer_lines_and_line_objects_are_reported, erp-validation-missing-objects.txt
		no_customer = pins_payload("ERP-NO-CUSTOMER", None)
		no_customer["accountingCustomerParty"] = None
		self.assertEqual(core._fetch_errors(no_customer), [
			("accountingCustomerParty", "Accounting customer party is required."), ("lineItems", "At least one line item is required.")])
		no_objects = pins_payload("ERP-NO-OBJECTS", [pins_line("Bare", 1, 10)])
		no_objects["invoiceLine"][0]["item"] = None
		no_objects["accountingCustomerParty"]["postalAddress"] = None
		self.assertEqual(core._fetch_errors(no_objects), [
			("accountingCustomerParty.postalAddress", "Customer postal address is required."),
			("lineItems[0]", "Item and price objects are required.")])

	def test_valid_pins_payload_passes(self):
		self.assertEqual(core._fetch_errors(pins_payload("ERP-OK", [pins_line("Widget", 2, 215)])), [])

	def test_signed_credit_note_is_normalised_before_validation(self):
		# CreditNoteHttpTests.Payload(credit: true) (CreditNoteHttpTests.cs:236-281) gets a 202 with negative amounts.
		credit = payload("full_return")
		for line in credit["invoiceLine"]:
			line["unitPrice"] = line["price"]["priceAmount"] = -line["unitPrice"]
			line["lineExtensionAmount"] = -line["lineExtensionAmount"]
		credit["legalMonetaryTotal"] = {k: -v for k, v in credit["legalMonetaryTotal"].items()}
		credit.update({"paymentStatus": "SETTLED", "dueDate": "2026-01-01", "paymentMeans": []})
		self.assertEqual(core._fetch_errors(credit), [])
		self.assertEqual(fetch_credit(credit, fetch_sources(payload("standard")))[0], None)

	def test_credit_note_checks(self):
		credit = payload("full_return")
		credit["billingReference"] = []
		credit["deposit"] = 10
		credit["invoiceLine"][0]["invoicedQuantity"] = 3
		self.assertEqual([f for f, _ in core._fetch_errors(credit)], ["billingReference", "deposit", "invoiceLine[0]"])
		credit = payload("full_return")
		credit["legalMonetaryTotal"]["payableAmount"] = 1
		self.assertEqual(core._fetch_errors(credit), [])


if __name__ == "__main__":
	unittest.main()

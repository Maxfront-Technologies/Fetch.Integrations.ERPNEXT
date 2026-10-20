from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# Sales Invoice fetch fields: no_copy so an amended invoice starts clean, and allow_on_submit=0 so a stale
# form cannot blank them. The app writes them only with frappe.db.set_value or frappe.db.sql.
FETCH_FIELD = dict(read_only=1, no_copy=1, allow_on_submit=0)

CUSTOM_FIELDS = {
	"Item": [
		dict(fieldname="firs_section", label="FIRS e-Invoicing", fieldtype="Section Break", collapsible=1,
			insert_after="customs_tariff_number"),
		dict(fieldname="firs_hsn_code", label="FIRS HSN Code", fieldtype="Link", options="FIRS HSN Code",
			insert_after="firs_section"),
		dict(fieldname="firs_tax_category", label="FIRS Tax Category", fieldtype="Link", options="FIRS Tax Category",
			insert_after="firs_hsn_code"),
		dict(fieldname="firs_quantity_code", label="FIRS Quantity Code", fieldtype="Link", options="FIRS Quantity Code",
			insert_after="firs_tax_category"),
	],
	# fetch_if_empty keeps a value typed on the line instead of re-fetching it from the Item on every save.
	"Sales Invoice Item": [
		dict(fieldname="firs_hsn_code", label="FIRS HSN Code", fieldtype="Link", options="FIRS HSN Code",
			fetch_from="item_code.firs_hsn_code", fetch_if_empty=1, insert_after="brand"),
		dict(fieldname="firs_tax_category", label="FIRS Tax Category", fieldtype="Link", options="FIRS Tax Category",
			fetch_from="item_code.firs_tax_category", fetch_if_empty=1, insert_after="firs_hsn_code"),
		dict(fieldname="firs_quantity_code", label="FIRS Quantity Code", fieldtype="Link", options="FIRS Quantity Code",
			fetch_from="item_code.firs_quantity_code", fetch_if_empty=1, insert_after="firs_tax_category"),
	],
	"Item Tax Template": [
		dict(fieldname="firs_tax_category", label="FIRS Tax Category", fieldtype="Link", options="FIRS Tax Category",
			insert_after="title"),
	],
	"Customer": [
		dict(fieldname="firs_business_description", label="FIRS Business Description", fieldtype="Small Text",
			insert_after="tax_id"),
	],
	"Sales Invoice": [
		dict(fieldname="fetch_section", label="FETCH e-Invoicing", fieldtype="Section Break", collapsible=1,
			insert_after="amended_from"),
		dict(fieldname="fetch_status", label="FETCH Status", fieldtype="Select",
			options="\nPending\nSubmitted\nTransmitted\nFailed", in_standard_filter=1, print_hide=1,
			insert_after="fetch_section", **FETCH_FIELD),
		dict(fieldname="fetch_irn", label="FETCH IRN", fieldtype="Data", search_index=1,
			insert_after="fetch_status", **FETCH_FIELD),
		dict(fieldname="fetch_invoice_id", label="FETCH Invoice ID", fieldtype="Data", print_hide=1,
			insert_after="fetch_irn", **FETCH_FIELD),
	],
}


def setup():
	create_custom_fields(CUSTOM_FIELDS)

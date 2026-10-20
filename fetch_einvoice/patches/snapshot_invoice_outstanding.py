import frappe


def execute():
	"""Invoice rows queued before the submit-time snapshot have amount 0, which would now read as fully paid. Use today's outstanding."""
	frappe.db.sql(
		"""update `tabFETCH Outbox` o join `tabSales Invoice` si on si.name = o.sales_invoice
		set o.amount = si.outstanding_amount
		where o.kind = 'Invoice' and o.status in ('Pending', 'Failed')"""
	)

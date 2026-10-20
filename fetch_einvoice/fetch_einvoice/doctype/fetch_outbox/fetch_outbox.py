# Copyright (c) 2026, Maxfront and contributors
# For license information, please see license.txt

# Rows change status only through fetch_einvoice.outbox, never through this form.
from frappe.model.document import Document


class FETCHOutbox(Document):
	pass

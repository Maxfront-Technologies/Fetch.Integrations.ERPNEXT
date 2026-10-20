# Copyright (c) 2026, Maxfront and contributors
# For license information, please see license.txt

from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

CALLBACK_METHOD = "fetch_einvoice.api.receive_fetch_irn"


class FETCHSettings(Document):
	def onload(self):
		self.callback_url = get_url("/api/method/" + CALLBACK_METHOD)

	def validate(self):
		if not self.base_url:
			return
		self.base_url = self.base_url.strip().rstrip("/")
		url = urlparse(self.base_url)
		if url.scheme != "https" or not url.netloc:
			frappe.throw(_("Base URL must be an https:// address, for example https://api.example.com."))

// Copyright (c) 2026, Maxfront and contributors
// For license information, please see license.txt

frappe.ui.form.on("FETCH Outbox", {
	refresh(frm) {
		if (frm.doc.status !== "Failed") return;
		frm.add_custom_button(__("Retry"), () => {
			frappe.xcall("fetch_einvoice.api.retry", { name: frm.doc.name }).then(() => frm.reload_doc());
		});
	},
});

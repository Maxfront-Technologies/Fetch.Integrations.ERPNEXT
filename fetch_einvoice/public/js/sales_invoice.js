frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		const status = frm.doc.fetch_status;
		if (frm.doc.docstatus !== 1 || !status) return;

		const color = { Pending: "orange", Submitted: "blue", Transmitted: "green", Failed: "red" }[status];
		let headline = __("FETCH: {0}", [__(status)]);
		if (frm.doc.fetch_irn) {
			headline += " · " + __("IRN {0}", [frappe.utils.escape_html(frm.doc.fetch_irn)]);
		}
		frm.dashboard.set_headline_alert(headline, color);

		if (status === "Failed" && frm.perm[0] && frm.perm[0].submit) {
			frm.add_custom_button(__("Resend to FETCH"), () => {
				frappe.call({
					method: "fetch_einvoice.api.resend_invoice",
					args: { sales_invoice: frm.doc.name },
					freeze: true,
					freeze_message: __("Queueing for FETCH…"),
				}).then(() => frm.reload_doc());
			});
		}
	},

	before_cancel(frm) {
		// Read the status from the server: the callback may have stored an IRN after this form loaded.
		return frappe.db.get_value("Sales Invoice", frm.doc.name, "fetch_status").then((r) => {
			const status = r.message && r.message.fetch_status;
			if (status !== "Submitted" && status !== "Transmitted") return;
			return new Promise((resolve) => {
				frappe.confirm(
					__("FETCH has already accepted this invoice. Cancelling it here does not cancel it in NRS, where the document stays valid. Issue a credit note if one is needed. Cancel anyway?"),
					() => resolve(),
					() => {
						frappe.validated = false;
						resolve();
					}
				);
			});
		});
	},
});

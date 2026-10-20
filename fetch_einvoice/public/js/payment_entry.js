frappe.ui.form.on("Payment Entry", {
	before_cancel(frm) {
		return frappe
			.xcall("fetch_einvoice.api.payment_accepted_by_fetch", { payment_entry: frm.doc.name })
			.then((accepted) => {
				if (!accepted) return;
				return new Promise((resolve) => {
					frappe.confirm(
						__("FETCH has already recorded this payment, and FETCH has no payment reversal. Cancelling it here leaves the payment in FETCH. Cancel anyway?"),
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

// Copyright (c) 2026, Maxfront and contributors
// For license information, please see license.txt

frappe.ui.form.on("FETCH Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Sync FIRS Codes"), () => {
			frappe.xcall("fetch_einvoice.api.sync_firs_codes").then(() => {
				frappe.show_alert({
					message: __("Syncing FIRS codes in the background…"),
					indicator: "blue",
				});
			});
		});
	},
});

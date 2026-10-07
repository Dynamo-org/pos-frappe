// restaurant_pos - Desk additions (loaded on every Desk page via hooks.py app_include_js).
//
// 1. Ticket P8.5-14: "Back to Back Office". The Back Office links into Desk for a full record and remembers,
//    in sessionStorage, where to come back to (never in the URL: Desk would read an unknown query parameter
//    as a list filter). This shows a small bar with a way back until it is used or dismissed.
// 2. Ticket P4-28: the Staff PIN form. A PIN is write-only - Desk can neither show nor set one - so the form
//    shows only the state of the PIN and offers "Request PIN reset", which flags the person to choose a new PIN
//    on their own device (the Hub hashes it; Frappe never sees the PIN).

(function () {
	const RETURN_KEY = "back-office.return";

	function readReturnPoint() {
		try {
			const path = sessionStorage.getItem(RETURN_KEY);
			// only ever a path inside the Back Office - never an arbitrary URL
			return path && path.startsWith("/back-office") ? path : null;
		} catch (e) {
			return null;
		}
	}

	function clearReturnPoint() {
		try {
			sessionStorage.removeItem(RETURN_KEY);
		} catch (e) {
			/* storage blocked: nothing to clear */
		}
	}

	function renderReturnBar() {
		const existing = document.getElementById("bo-return-bar");
		const path = readReturnPoint();
		if (!path) {
			if (existing) existing.remove();
			return;
		}
		if (existing) return;
		const bar = document.createElement("div");
		bar.id = "bo-return-bar";
		bar.setAttribute("role", "navigation");
		bar.setAttribute("aria-label", "Back Office");

		const back = document.createElement("a");
		back.href = path;
		back.className = "bo-return-link";
		back.textContent = "← Back to Back Office";
		back.addEventListener("click", clearReturnPoint);

		const close = document.createElement("button");
		close.type = "button";
		close.className = "bo-return-dismiss";
		close.setAttribute("aria-label", "Dismiss");
		close.textContent = "×";
		close.addEventListener("click", function () {
			clearReturnPoint();
			bar.remove();
		});

		bar.appendChild(back);
		bar.appendChild(close);
		document.body.appendChild(bar);
	}

	$(document).ready(function () {
		renderReturnBar();
		if (frappe.router && frappe.router.on) frappe.router.on("change", renderReturnBar);
	});

	frappe.ui.form.on("Staff PIN", {
		refresh: function (frm) {
			if (frm.is_new()) return;
			// pin_hash is a Password field: Desk gets a placeholder when one is set, never the value.
			const hasPin = !!frm.doc.pin_hash;
			const pending = !!frm.doc.pin_reset_requested_at;
			frm.dashboard.clear_headline();
			frm.dashboard.set_headline_alert(
				pending
					? __("A PIN reset is waiting - this person will choose a new PIN on their device.")
					: hasPin
						? __("PIN is set. It is never shown here.")
						: __("No PIN yet - this person sets it on their device after a reset is requested."),
				pending ? "orange" : hasPin ? "green" : "blue"
			);
			frm.add_custom_button(
				__("Request PIN reset"),
				function () {
					frappe.confirm(__("Ask {0} to choose a new PIN on their device?", [frm.doc.full_name]), function () {
						frappe.call({
							method: "restaurant_pos.restaurant_pos.api.staff.request_pin_reset",
							args: { staff: frm.doc.name },
							freeze: true,
							callback: function () {
								frappe.show_alert({ message: __("PIN reset requested"), indicator: "green" });
								frm.reload_doc();
							},
						});
					});
				},
				__("Actions")
			);
		},
	});
	// 3. Ticket P1-14: Hub credentials. A Hub is paired with ERPNext by a one-time code, never by a static API key.
	//    The code is shown ONCE, here, and only its hash is kept; the person at the Hub's server runs
	//    `node dist/cli/pair-hub.js --frappe-url <this site> --code <code>`.
	frappe.listview_settings["Hub Credential"] = {
		add_fields: ["status"],
		get_indicator: function (doc) {
			const colours = { "Awaiting pairing": "orange", Active: "green", Revoked: "gray" };
			return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
		},
		onload: function (listview) {
			listview.page.add_inner_button(__("Pair a Hub..."), function () {
				const dialog = new frappe.ui.Dialog({
					title: __("Pair a Hub with this site"),
					fields: [
						{ fieldname: "label", fieldtype: "Data", label: __("What is it for?"), reqd: 1, description: __("e.g. Cloud Hub server 1, or the Edge box at Koramangala") },
						{
							fieldname: "outlet",
							fieldtype: "Link",
							options: "Outlet",
							label: __("Outlet (Edge box only)"),
							description: __("Leave empty for the Cloud Hub: one credential for the whole site. Set it for an Edge box: the credential can then only ever reach that outlet."),
						},
					],
					primary_action_label: __("Generate pairing code"),
					primary_action: function (values) {
						frappe.call({
							method: "restaurant_pos.restaurant_pos.api.hub_credentials.generate_hub_pairing_code",
							args: { label: values.label, outlet: values.outlet || null },
							freeze: true,
							callback: function (r) {
								dialog.hide();
								const out = r.message;
								frappe.msgprint({
									title: __("Pairing code - shown only once"),
									indicator: "green",
									message:
										"<p style='font-size:20px;letter-spacing:1px;font-family:monospace'><b>" + frappe.utils.escape_html(out.code) + "</b></p>" +
										"<p>" + __("Valid until {0}. Scope: {1}.", [frappe.utils.escape_html(out.expires_at), frappe.utils.escape_html(out.scope)]) + "</p>" +
										"<p>" + __("On the Hub's server run:") + "</p>" +
										"<pre>node dist/cli/pair-hub.js --frappe-url " + frappe.utils.escape_html(window.location.origin) + " --code " + frappe.utils.escape_html(out.code) + "</pre>",
								});
								listview.refresh();
							},
						});
					},
				});
				dialog.show();
			}).addClass("btn-primary");
		},
	};

	frappe.ui.form.on("Hub Credential", {
		refresh: function (frm) {
			if (frm.is_new() || frm.doc.status === "Revoked") return;
			frm.add_custom_button(
				__("Revoke"),
				function () {
					frappe.confirm(
						__("Revoke {0}? The Hub using it stops being able to talk to this site immediately, and it cannot be reinstated - it would have to be paired again with a new code.", [frm.doc.label]),
						function () {
							frappe.call({
								method: "restaurant_pos.restaurant_pos.api.hub_credentials.revoke_hub_credential",
								args: { name: frm.doc.name },
								freeze: true,
								callback: function () {
									frappe.show_alert({ message: __("Revoked"), indicator: "green" });
									frm.reload_doc();
								},
							});
						}
					);
				},
				__("Actions")
			);
		},
	});

})();

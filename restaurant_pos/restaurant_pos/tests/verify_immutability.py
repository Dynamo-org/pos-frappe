# Tickets P3-29 + P3-05 (ERPNext half) verification. Run against the dev bench:
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_immutability.run
# Throwaway users and rows are removed at the end (via the server-side purge flag).

import json
import uuid

import frappe
from frappe.utils import now_datetime

from restaurant_pos.restaurant_pos.api import audit
from restaurant_pos.restaurant_pos.api.access_rights import save_access_matrix
from restaurant_pos.restaurant_pos.api.v1.sync import push_void_log

failures = []


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def refused(fn):
	"""True if fn() raises a permission/validation refusal (and leaves nothing half-written)."""
	try:
		fn()
	except (frappe.PermissionError, frappe.ValidationError):
		frappe.db.rollback()
		return True
	except Exception as err:  # any other refusal still counts, but report which
		frappe.db.rollback()
		print("   (refused with", type(err).__name__, str(err)[:80], ")")
		return True
	return False


def make_user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=1, ignore_permissions=True)
	frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": "P3029", "send_welcome_email": 0, "roles": [{"role": r} for r in roles]}
	).insert(ignore_permissions=True)
	return email


def run():
	frappe.set_user("Administrator")
	staff = frappe.get_all("Staff PIN", pluck="name", limit=2)
	owner = make_user("p3029-owner@example.test", ["Owner/Manager"])
	sysmgr = make_user("p3029-sysmgr@example.test", ["System Manager"])
	waiter = make_user("p3029-waiter@example.test", ["Server/Waiter"])

	hub_uuid = f"p3029-{uuid.uuid4()}"
	result = push_void_log(
		json.dumps(
			{
				"hub_uuid": hub_uuid,
				"outlet": "KOR",
				"order_id": "p3029-order",
				"line_id": "p3029-line",
				"amount_paise": 12345,
				"reason": "original reason",
				"requested_by_staff_id": staff[0],
				"approved_by_staff_id": staff[1],
				"device_credential_id": None,
				"occurred_at": now_datetime().isoformat() + "Z",
			}
		)
	)
	name = result["name"]
	check(frappe.db.get_value("Void Log", name, "docstatus") == 1, "the Hub's insert-then-submit still works (row exists, submitted)")

	for who in ("Administrator", owner, sysmgr):
		frappe.set_user(who)
		label = "Administrator" if who == "Administrator" else who.split("@")[0]

		def edit():
			doc = frappe.get_doc("Void Log", name)
			doc.reason = "tampered"
			doc.save(ignore_permissions=True)  # even with permissions skipped, the guard must refuse

		def edit_after_submit():
			doc = frappe.get_doc("Void Log", name)
			doc.reason = "tampered"
			doc.flags.ignore_permissions = True
			doc.save()

		def cancel():
			frappe.get_doc("Void Log", name).cancel()

		def delete():
			frappe.delete_doc("Void Log", name, force=1, ignore_permissions=True)

		check(refused(edit), f"{label}: editing a Void Log is refused")
		check(refused(cancel), f"{label}: cancelling a Void Log is refused")
		check(refused(delete), f"{label}: deleting a Void Log is refused")
		frappe.set_user("Administrator")
		check(frappe.db.get_value("Void Log", name, "reason") == "original reason", f"{label}: the row is unchanged afterwards")

	# Permissions themselves: nobody - System Manager included - holds write/create/delete.
	for who in (owner, sysmgr):
		frappe.set_user(who)
		for ptype in ("write", "create", "delete"):
			check(not frappe.has_permission("Void Log", ptype), f"{who.split('@')[0]} has no '{ptype}' permission on Void Log")
		frappe.set_user("Administrator")

	# Hub-originated Sales Invoices: cancel/delete/edit refused; hand-made ones are not ours to lock.
	invoice = frappe.db.get_value("Sales Invoice", {"hub_uuid": ["is", "set"], "docstatus": 1}, "name")
	if invoice:
		frappe.set_user(sysmgr)
		check(refused(lambda: frappe.get_doc("Sales Invoice", invoice).cancel()), "System Manager: cancelling a Hub-synced Sales Invoice is refused")
		check(refused(lambda: frappe.delete_doc("Sales Invoice", invoice, force=1, ignore_permissions=True)), "System Manager: deleting a Hub-synced Sales Invoice is refused")

		def edit_invoice():
			doc = frappe.get_doc("Sales Invoice", invoice)
			doc.remarks = "tampered"
			doc.flags.ignore_permissions = True
			doc.save()

		check(refused(edit_invoice), "System Manager: editing a Hub-synced Sales Invoice is refused")
		frappe.set_user("Administrator")
		check(frappe.db.get_value("Sales Invoice", invoice, "docstatus") == 1, "the Sales Invoice is still submitted")
	else:
		check(False, "a Hub-synced Sales Invoice exists to test against")
	manual = frappe.new_doc("Sales Invoice")
	from restaurant_pos.restaurant_pos.guards.immutability import _guarded

	check(not _guarded(manual), "an invoice with no hub_uuid (entered by hand in Desk) is not locked")

	# Corrections: a new, linked entry; Owner/Manager and System Manager may; a waiter may not.
	frappe.set_user(waiter)
	check(refused(lambda: audit.record_correction("Void Log", name, "x")), "a waiter cannot record a correction")
	frappe.set_user(owner)
	check(refused(lambda: audit.record_correction("Void Log", name, "  ")), "a correction without a reason is refused")
	corrected = audit.record_correction("Void Log", name, "wrong amount keyed")
	frappe.set_user("Administrator")
	row = frappe.get_doc("Void Log", corrected["name"])
	check(row.correction_of == name and row.correction_reason == "wrong amount keyed", "the correction is a NEW row linked back to the original")
	check(row.amount == -12345 and row.name != name, "its amount is negated so the log nets out, and it is a different record")
	check(frappe.db.get_value("Void Log", name, "reason") == "original reason", "the original is untouched by its correction")
	frappe.set_user(sysmgr)
	check(refused(lambda: audit.record_correction("Void Log", corrected["name"], "again")), "a correction cannot itself be corrected")
	check(refused(lambda: audit.record_correction("Item", "x", "no")), "only audit doctypes can be corrected this way")
	frappe.set_user("Administrator")

	# Access rights matrix cannot grant write on an audit record.
	check(
		refused(lambda: save_access_matrix([{"role": "Cashier", "doctype": "Void Log", "ptype": "write", "value": True}])),
		"the Access rights matrix refuses to grant write on Void Log",
	)

	# Cleanup: the deliberate server-side purge, never reachable from Desk/API.
	frappe.flags.allow_audit_purge = True
	for n in (corrected["name"], name):
		frappe.get_doc("Void Log", n).cancel() if frappe.db.get_value("Void Log", n, "docstatus") == 1 else None
		frappe.delete_doc("Void Log", n, force=1, ignore_permissions=True)
	frappe.flags.allow_audit_purge = False
	for u in (owner, sysmgr, waiter):
		frappe.delete_doc("User", u, force=1, ignore_permissions=True)
	frappe.db.commit()
	check(not frappe.db.exists("Void Log", name), "test rows removed via the server-side purge flag")

	print("ALL PASS" if not failures else f"{len(failures)} FAILED")

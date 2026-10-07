# Ticket P4-25 (ERPNext half): consent records arrive through the bulk endpoint, are idempotent, are
# append-only for every role, and a withdrawal is a NEW row.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_guest_consent.run

import json
import uuid

import frappe
from frappe.utils import now_datetime

from restaurant_pos.restaurant_pos.api.v1.sync import push_batch

failures = []


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def payload(granted, hub_uuid=None):
	return {
		"hub_uuid": hub_uuid or f"gc-{uuid.uuid4()}",
		"outlet": "KOR",
		"order_id": "gc-order",
		"phone": "+919876500001",
		"purpose": "whatsapp_updates",
		"granted": granted,
		"text_version": "2026-10-v1",
		"consent_text": "Send me WhatsApp messages about my order.",
		"captured_at": now_datetime().isoformat() + "Z",
	}


def run():
	frappe.set_user("Administrator")
	grant = payload(True)
	out = push_batch("guest_consent", json.dumps([grant]))["results"][0]
	check(out["status"] == "ok" and not out["already_existed"], "a consent grant syncs into ERPNext")
	row = frappe.get_doc("Guest Consent", out["name"])
	check(row.docstatus == 1 and row.granted == 1 and row.purpose == "whatsapp_updates" and row.phone == "+919876500001", "it is stored submitted, with the number and purpose")
	check("WhatsApp" in row.consent_text and row.text_version == "2026-10-v1", "the exact wording shown to the guest and its version are stored with it")

	again = push_batch("guest_consent", json.dumps([grant]))["results"][0]
	check(again["status"] == "ok" and again["already_existed"] and again["name"] == out["name"], "a retried push does not create a second row")

	sysmgr = "gc-sysmgr@example.test"
	if frappe.db.exists("User", sysmgr):
		frappe.delete_doc("User", sysmgr, force=1, ignore_permissions=True)
	frappe.get_doc({"doctype": "User", "email": sysmgr, "first_name": "GC", "send_welcome_email": 0, "roles": [{"role": "System Manager"}]}).insert(ignore_permissions=True)
	frappe.set_user(sysmgr)

	def refused(fn):
		try:
			fn()
		except (frappe.PermissionError, frappe.ValidationError):
			frappe.db.rollback()
			return True
		return False

	def edit():
		d = frappe.get_doc("Guest Consent", out["name"])
		d.granted = 0
		d.flags.ignore_permissions = True
		d.save()

	check(refused(edit), "even System Manager cannot edit a consent record (flipping it would forge a consent)")
	check(refused(lambda: frappe.delete_doc("Guest Consent", out["name"], force=1, ignore_permissions=True)), "...or delete it")
	frappe.set_user("Administrator")
	check(frappe.db.get_value("Guest Consent", out["name"], "granted") == 1, "the record is unchanged")

	withdraw = push_batch("guest_consent", json.dumps([payload(False)]))["results"][0]
	rows = frappe.get_all("Guest Consent", filters={"phone": "+919876500001", "purpose": "whatsapp_updates"}, fields=["name", "granted", "captured_at"], order_by="captured_at asc, creation asc")
	check(withdraw["status"] == "ok" and len(rows) == 2 and rows[0].granted == 1 and rows[-1].granted == 0, "a withdrawal is a NEW row: the history shows grant then withdrawal, the latest is the current state")

	bad = push_batch("guest_consent", json.dumps([{"hub_uuid": f"gc-{uuid.uuid4()}", "outlet": "KOR"}, payload(True)]))["results"]
	check(bad[0]["status"] == "error" and bad[1]["status"] == "ok", "a malformed record fails alone; its neighbour still posts")

	frappe.flags.allow_audit_purge = True
	for name in frappe.get_all("Guest Consent", filters={"phone": "+919876500001"}, pluck="name"):
		d = frappe.get_doc("Guest Consent", name)
		if d.docstatus == 1:
			d.cancel()
		frappe.delete_doc("Guest Consent", name, force=1, ignore_permissions=True)
	frappe.flags.allow_audit_purge = False
	frappe.delete_doc("User", sysmgr, force=1, ignore_permissions=True)
	frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")

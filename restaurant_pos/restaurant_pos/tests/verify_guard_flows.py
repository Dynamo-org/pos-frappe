# Tickets P3-29/P3-05 regression: the guards must not break the Hub's own legitimate write flows -
# bill -> refund (credit note). (Shift open/close are not exercised here: the dev outlet has a real
# open shift that must not be disturbed.) Runs the
# real whitelisted push functions, then removes everything it made through the server-side purge flag.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_guard_flows.run

import json
import uuid

import frappe
from frappe.utils import now_datetime

from restaurant_pos.restaurant_pos.api.v1.sync import (
	push_pos_invoice,
	push_refund,
)

failures = []


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def purge_residue():
	"""Removes anything a previous (crashed) run left behind, via the server-side purge flag."""
	frappe.flags.allow_audit_purge = True
	for name in frappe.get_all("Refund Log", filters={"reason": "guard regression"}, pluck="name"):
		doc = frappe.get_doc("Refund Log", name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Refund Log", name, force=1, ignore_permissions=True)
	originals = frappe.get_all("Sales Invoice", filters={"hub_invoice_number": ["like", "GF/%"]}, pluck="name")
	# NEVER query with an empty list here: `return_against in [""]` matches every ordinary invoice.
	credit_notes = frappe.get_all("Sales Invoice", filters={"return_against": ["in", originals]}, pluck="name") if originals else []
	for name in [*credit_notes, *originals]:
		doc = frappe.get_doc("Sales Invoice", name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)
	frappe.flags.allow_audit_purge = False
	frappe.db.commit()


def run():
	frappe.set_user("Administrator")
	purge_residue()
	staff = frappe.get_all("Staff PIN", pluck="name", limit=2)
	item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1, "is_stock_item": 0}, "name") or frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name")
	made = {"Sales Invoice": [], "Refund Log": []}
	now = now_datetime().isoformat() + "Z"

	inv_payload = {
		"hub_uuid": f"gf-{uuid.uuid4()}",
		"outlet": "KOR",
		# the open shift's own business date (a dev shift left open overnight is yesterday's)
		"business_date": str(frappe.db.get_value("POS Opening Entry", {"status": "Open", "pos_profile": frappe.db.get_value("Outlet", "KOR", "pos_profile")}, "period_start_date") .date()) if frappe.db.exists("POS Opening Entry", {"status": "Open"}) else str(frappe.utils.nowdate()),
		"expected_total_paise": 10500,
		"hub_invoice_number": "GF/26-27/00001",
		"lines": [{"item_code": item, "qty": 1, "unit_price_paise": 10000, "item_tax_template": None, "modifiers": []}],
		"mode": "cash",
		"verification_status": "auto-confirmed",
		"gateway_transaction_id": None,
		"device_credential_id": None,
		"staff_id": staff[0],
		"order_id": "gf-order",
		"customer_gstin": None,
		"customer_business_name": None,
	}
	first = push_pos_invoice(json.dumps(inv_payload))
	if first.get("status") == "mismatch":
		# Learn the real total from the message, then resend with it (the Hub always knows it).
		import re

		total = re.search(r"(\d+(?:\.\d+)?)", first["message"].split("Frappe")[-1])
		print("   mismatch message:", first["message"])
		for dl in frappe.get_all("Sync Dead Letter", filters={"hub_uuid": ["like", "gf-%"]}, pluck="name"):
			frappe.delete_doc("Sync Dead Letter", dl, force=1, ignore_permissions=True)
		frappe.db.commit()
		check(False, "the bill total matched")
		return
	check(first["status"] == "ok" and first.get("pos_invoice"), "bill synced as a Sales Invoice")
	made["Sales Invoice"].append(first["pos_invoice"])

	refund = push_refund(
		json.dumps(
			{
				"hub_uuid": f"gf-{uuid.uuid4()}",
				"outlet": "KOR",
				"order_id": "gf-order",
				"sales_invoice": first["pos_invoice"],
				"amount_paise": 5000,
				"reason": "guard regression",
				"requested_by_staff_id": staff[0],
				"approved_by_staff_id": staff[1],
				"device_credential_id": None,
				"occurred_at": now,
				"business_date": inv_payload["business_date"],
			}
		)
	)
	check(refund["status"] == "ok" and refund.get("credit_note"), "refund synced as a Credit Note (a NEW linked document - the original is not edited)")
	made["Refund Log"].append(refund["name"])
	made["Sales Invoice"].append(refund["credit_note"])

	# Shift entries: the existing Hub-created POS Opening Entry (if any) cannot be cancelled or deleted.
	opening = frappe.db.get_value("POS Opening Entry", {"hub_uuid": ["is", "set"], "docstatus": 1}, "name")
	if opening:
		def cancel_opening():
			frappe.get_doc("POS Opening Entry", opening).cancel()

		try:
			cancel_opening()
			check(False, "cancelling a Hub-synced POS Opening Entry is refused")
		except (frappe.PermissionError, frappe.ValidationError):
			# (ERPNext's own "unconsolidated invoices" check may fire first; the guard is asserted next.)
			frappe.db.rollback()
			check(True, "cancelling a Hub-synced POS Opening Entry is refused")
		from restaurant_pos.restaurant_pos.guards.immutability import block_cancel

		try:
			block_cancel(frappe.get_doc("POS Opening Entry", opening))
			check(False, "the audit guard itself refuses a Hub-synced POS Opening Entry")
		except frappe.PermissionError:
			check(True, "the audit guard itself refuses a Hub-synced POS Opening Entry")

	# Everything above went through even though the same doctypes now refuse edits/cancels/deletes.
	check(frappe.db.get_value("Sales Invoice", first["pos_invoice"], "docstatus") == 1, "original invoice still submitted after its refund")

	# Cleanup via the purge flag, newest dependents first.
	frappe.flags.allow_audit_purge = True
	order = ["Refund Log", "Sales Invoice"]
	for doctype in order:
		for name in reversed(made[doctype]):
			if not frappe.db.exists(doctype, name):
				continue
			doc = frappe.get_doc(doctype, name)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
	frappe.flags.allow_audit_purge = False
	for dl in frappe.get_all("Sync Dead Letter", filters={"hub_uuid": ["like", "gf-%"]}, pluck="name"):
		frappe.delete_doc("Sync Dead Letter", dl, force=1, ignore_permissions=True)
	frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")

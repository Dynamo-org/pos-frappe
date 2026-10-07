# Ticket P4-35 (ERPNext half): the Floor Permission API - read defaults, change a cell, the change is in what
# pull_changes sends to the Hub, critical actions can't be taken from Owner/Manager, every change is audit
# logged, and the Python defaults match the ones the Hub ships (packages/api-types floor-permissions.ts).
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_floor_permissions.run

import json
import os
import re

import frappe

from restaurant_pos.restaurant_pos.api import access_rights
from restaurant_pos.restaurant_pos.api.v1.sync import pull_changes

failures = []


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def ts_definitions():
	"""Parse FLOOR_ACTIONS out of the TypeScript source - the Hub's copy of the defaults."""
	path = os.path.realpath(os.path.join(frappe.get_app_path("restaurant_pos"), "..", "..", "packages", "api-types", "src", "floor-permissions.ts"))
	text = open(path, encoding="utf-8").read()
	out = {}
	for key, label, critical, defaults in re.findall(r'(\w+): \{ label: "([^"]+)", critical: (true|false), defaults: \[([^\]]*)\] \}', text):
		out[key] = {"label": label, "critical": critical == "true", "defaults": [d.strip().strip('"') for d in defaults.split(",") if d.strip()]}
	return out


def run():
	frappe.set_user("Administrator")
	for name in frappe.get_all("Floor Permission", pluck="name"):
		frappe.delete_doc("Floor Permission", name, force=1, ignore_permissions=True)
	frappe.db.commit()
	try:
		view = access_rights.get_floor_permissions()
		ts = ts_definitions()
		label = {"waiter": "Waiter", "kitchen": "Kitchen", "cashier": "Cashier", "supervisor": "Supervisor", "owner_manager": "Owner/Manager"}
		py = {a["key"]: a for a in view["actions"]}
		check(set(ts) == set(py) and len(ts) == 6, f"the action list is the same on both sides ({sorted(ts)})")
		check(
			all(py[k]["label"] == ts[k]["label"] and py[k]["critical"] == ts[k]["critical"] for k in ts),
			"labels and 'critical' flags match the Hub's copy",
		)
		check(
			all({r for r, allowed in ((r, view["matrix"][r][k]) for r in view["roles"]) if allowed} == {label[d] for d in ts[k]["defaults"]} for k in ts),
			"the built-in defaults match the Hub's copy exactly (so an untouched outlet behaves as before)",
		)
		check(view["roles"] == ["Waiter", "Kitchen", "Cashier", "Supervisor", "Owner/Manager"], "the five staff roles, in order")

		out = access_rights.save_floor_permissions(json.dumps([{"staff_role": "Cashier", "action": "approve_void", "allowed": True}, {"staff_role": "Supervisor", "action": "approve_void", "allowed": False}]))
		check(out["applied"] == 2, "two cells changed")
		m = access_rights.get_floor_permissions()["matrix"]
		check(m["Cashier"]["approve_void"] is True and m["Supervisor"]["approve_void"] is False and m["Supervisor"]["approve_refund"] is True, "the matrix reflects the change and nothing else moved")
		rows = pull_changes("KOR", None)["floor_permissions"]
		check({(r["staff_role"], r["action"], r["allowed"]) for r in rows} == {("Cashier", "approve_void", True), ("Supervisor", "approve_void", False)}, "what pull_changes sends the Hub is exactly the explicit rows")
		again = access_rights.save_floor_permissions(json.dumps([{"staff_role": "Cashier", "action": "approve_void", "allowed": True}]))
		check(again["applied"] == 0, "saving the same value again changes nothing")
		log = frappe.get_all("Activity Log", filters={"reference_doctype": "Floor Permission"}, fields=["content"], order_by="creation desc", limit=1)
		check(log and "Cashier" in log[0].content and "Approve a void" in log[0].content, "the change is audit-logged naming the role and the action")

		def refused(changes):
			try:
				access_rights.save_floor_permissions(json.dumps(changes))
			except frappe.ValidationError:
				frappe.db.rollback()
				return True
			return False

		before = frappe.db.count("Floor Permission")
		check(refused([{"staff_role": "Owner/Manager", "action": "open_close_shift", "allowed": False}]), "Owner/Manager cannot lose a critical action (nobody could open a shift)")
		check(refused([{"staff_role": "Waiter", "action": "teleport", "allowed": True}]) and refused([{"staff_role": "Janitor", "action": "approve_void", "allowed": True}]), "an unknown action or role is refused")
		check(refused([{"staff_role": "Cashier", "action": "approve_refund", "allowed": True}, {"staff_role": "Owner/Manager", "action": "approve_void", "allowed": False}]), "one bad cell refuses the whole change")
		check(frappe.db.count("Floor Permission") == before and frappe.db.get_value("Floor Permission", "Cashier-approve_refund", "allowed") is None, "...and nothing was written")
		check(access_rights.save_floor_permissions(json.dumps([{"staff_role": "Owner/Manager", "action": "approve_guest_orders", "allowed": False}]))["applied"] == 1, "a non-critical action may be switched off even for Owner/Manager")
	finally:
		for name in frappe.get_all("Floor Permission", pluck="name"):
			frappe.delete_doc("Floor Permission", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Activity Log", filters={"reference_doctype": "Floor Permission"}, pluck="name"):
			frappe.delete_doc("Activity Log", name, force=1, ignore_permissions=True)
		frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")

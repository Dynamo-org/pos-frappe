# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P8.5-08 (Back Office Access rights) + the buildable slice of P4-29. Back Office/Desk
# scope only — a change here does NOT reach the floor (Security & Access §6: the Hub deliberately
# keeps its own small, hardcoded set of checks and "should not" replicate ERP-style permission
# granularity). Making a change here actually affect void/refund/shift/PIN-unlock checks is real,
# separate, deferred scope — tracked as new ticket P4-35, not built here.
#
# Reuses Frappe's own Role/Custom DocPerm system directly (frappe.permissions' own
# add_permission/update_permission_property — the same functions Desk's native Role Permission
# Manager calls) rather than a hand-rolled permission store, matching every other Back Office
# ticket's own "Frappe's engine, not a parallel one" principle.

import frappe
from frappe.permissions import add_permission, update_permission_property

from restaurant_pos.restaurant_pos.setup.access_rights_setup import BASE_ROLES, ensure_complete_custom_perms
from restaurant_pos.restaurant_pos.api.scope import require_org_wide

# A curated, restaurant-relevant subset — not Frappe's full doctype list (hundreds of built-in
# ERPNext doctypes would make this matrix meaningless for a restaurant owner).
MATRIX_DOCTYPES = [
	"Outlet",
	"Floor",
	"Restaurant Table",
	"Station",
	"Printer",
	"Device",
	"Staff PIN",
	"Item",
	"Item Price",
	"Item Group",
	"Modifier Group",
	"Pairing Code",
	"Void Log",
	"Refund Log",
	"Availability Log",
	"KOT",
	"Guest Consent",
	"Sync Dead Letter",
]
PTYPES = ["read", "write", "create", "delete"]
# Append-only records can only ever be READ, whoever asks (guards/immutability.py) - the matrix refuses to grant more.
from restaurant_pos.restaurant_pos.guards.immutability import APPEND_ONLY_DOCTYPES as _APPEND_ONLY, AUDIT_DOCTYPES as _AUDIT

AUDIT_DOCTYPES = (*_AUDIT, *_APPEND_ONLY)


def _require_manage_permission():
	if not frappe.has_permission("Role", "write"):
		frappe.throw("Not permitted to manage access rights", frappe.PermissionError)
	# Roles and the permission matrix apply to every branch: a branch-scoped manager must not be able to
	# grant themselves (or anyone) more, or change what a role can do everywhere.
	require_org_wide("Managing access rights")


@frappe.whitelist()
def get_access_matrix() -> dict:
	_require_manage_permission()
	matrix = {role: {doctype: {ptype: False for ptype in PTYPES} for doctype in MATRIX_DOCTYPES} for role in BASE_ROLES}
	rows = frappe.get_all(
		"Custom DocPerm",
		filters={"parent": ["in", MATRIX_DOCTYPES], "role": ["in", BASE_ROLES], "permlevel": 0},
		fields=["parent", "role", *PTYPES],
	)
	for row in rows:
		for ptype in PTYPES:
			matrix[row.role][row.parent][ptype] = bool(row.get(ptype))

	role_user_counts = {role: frappe.db.count("Has Role", {"role": role, "parenttype": "User"}) for role in [*BASE_ROLES, "System Manager"]}

	return {"roles": BASE_ROLES, "doctypes": MATRIX_DOCTYPES, "ptypes": PTYPES, "matrix": matrix, "role_user_counts": role_user_counts}


def _set_permission(doctype: str, role: str, ptype: str, value: bool):
	ensure_complete_custom_perms(doctype)
	existing = frappe.db.get_value("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0})
	if not existing:
		if not value:
			return
		add_permission(doctype, role, permlevel=0, ptype=ptype)
	else:
		update_permission_property(doctype, role, 0, ptype, value=1 if value else 0)


@frappe.whitelist()
def save_access_matrix(changes: list | str) -> dict:
	"""`changes` is `[{role, doctype, ptype, value}, ...]` — only the cells actually toggled,
	not the whole matrix, so the audit trail names exactly what changed."""
	_require_manage_permission()
	changes = frappe.parse_json(changes) if isinstance(changes, str) else changes

	applied = []
	for change in changes:
		role, doctype, ptype, value = change["role"], change["doctype"], change["ptype"], bool(change["value"])
		if role not in BASE_ROLES or doctype not in MATRIX_DOCTYPES or ptype not in PTYPES:
			frappe.throw("Unknown role, doctype or permission type", frappe.ValidationError)
		if doctype in AUDIT_DOCTYPES and ptype != "read" and value:
			# Ticket P3-29: audit records are read-only for every role; the guards refuse the write
			# anyway, but a switch that can be turned on and does nothing would only mislead.
			frappe.throw(f"{doctype} is an audit record: it can only ever be read, never {ptype}d", frappe.ValidationError)
		_set_permission(doctype, role, ptype, value)
		applied.append(f"{role} · {doctype} · {ptype} -> {'on' if value else 'off'}")

	if applied:
		frappe.get_doc(
			{
				"doctype": "Activity Log",
				"subject": "Back Office access rights changed",
				"content": "; ".join(applied),
				"reference_doctype": "Role",
				"status": "Success",
			}
		).insert(ignore_permissions=True)

	frappe.db.commit()
	return {"applied": len(applied)}


@frappe.whitelist()
def list_role_users() -> list:
	_require_manage_permission()
	users = frappe.get_list("User", filters={"enabled": 1, "user_type": "System User"}, fields=["name", "full_name"], order_by="full_name asc")
	for user in users:
		user["roles"] = [r.role for r in frappe.get_doc("User", user.name).roles if r.role in [*BASE_ROLES, "System Manager"]]
	return users


@frappe.whitelist()
def assign_user_role(user: str, role: str, action: str) -> dict:
	"""`action` is "add" or "remove". Assigning a person to a Frappe role for Back Office/Desk
	login — a separate thing from the Staff screen's (P8.5-06) on-floor Staff PIN assignments."""
	_require_manage_permission()
	if role not in BASE_ROLES:
		frappe.throw("Unknown role", frappe.ValidationError)
	doc = frappe.get_doc("User", user)
	has_role = any(r.role == role for r in doc.roles)
	if action == "add" and not has_role:
		doc.append("roles", {"role": role})
		doc.save(ignore_permissions=True)
	elif action == "remove" and has_role:
		doc.roles = [r for r in doc.roles if r.role != role]
		doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"name": doc.name}


# ---------------------------------------------------------------------------------------------------------
# Ticket P4-35: FLOOR permissions - what a staff role may do ON THE FLOOR, enforced by the Hub.
#
# Security & Access section 6 deliberately keeps the Hub's on-floor checks few and simple ("full ERP-style
# permission granularity should not be replicated there"). So this is NOT the doctype matrix above: it is a
# short, fixed list of floor actions x the five staff roles, edited here, synced down to every Hub on its
# next pull (pull_changes carries every row), and read by the Hub in place of the role strings that used to
# be hard-coded in its routes. A role/action with no row keeps the built-in default below, which is
# exactly the behaviour the Hub had before this existed - so an outlet that never touches this screen
# behaves identically. The Hub holds a copy of these defaults (packages/api-types floor-permissions.ts);
# the two lists are kept in step by tests/verify_floor_permissions.py.

FLOOR_ROLES = ["Waiter", "Kitchen", "Cashier", "Supervisor", "Owner/Manager"]

FLOOR_ACTIONS = {
	"approve_void": {
		"label": "Approve a void",
		"description": "Be the second person who approves cancelling an item that was already sent to the kitchen.",
		"defaults": ["Supervisor", "Owner/Manager"],
		"critical": True,
	},
	"approve_refund": {
		"label": "Approve a refund",
		"description": "Be the second person who approves a refund on a paid bill.",
		"defaults": ["Supervisor", "Owner/Manager"],
		"critical": True,
	},
	"open_close_shift": {
		"label": "Open and close the shift",
		"description": "Start the day's shift with the opening cash and close it with the counted cash.",
		"defaults": ["Supervisor", "Owner/Manager"],
		"critical": True,
	},
	"unlock_staff_pin": {
		"label": "Unlock a locked PIN",
		"description": "Unlock a staff member who has been locked out after too many wrong PINs.",
		"defaults": ["Supervisor", "Owner/Manager"],
		"critical": True,
	},
	"approve_guest_orders": {
		"label": "Approve guest-app orders",
		"description": "Confirm a table's first QR order before it reaches the bill (when the outlet requires it).",
		"defaults": ["Waiter", "Supervisor", "Owner/Manager"],
		"critical": False,
	},
	"view_sync_issues": {
		"label": "See and replay sync problems",
		"description": "Review records ERPNext rejected and replay them once fixed.",
		"defaults": ["Supervisor", "Owner/Manager"],
		"critical": False,
	},
}


def _floor_matrix() -> dict:
	matrix = {role: {action: role in meta["defaults"] for action, meta in FLOOR_ACTIONS.items()} for role in FLOOR_ROLES}
	for row in frappe.get_all("Floor Permission", fields=["staff_role", "action", "allowed"]):
		if row.staff_role in matrix and row.action in matrix[row.staff_role]:
			matrix[row.staff_role][row.action] = bool(row.allowed)
	return matrix


@frappe.whitelist()
def get_floor_permissions() -> dict:
	_require_manage_permission()
	return {
		"roles": FLOOR_ROLES,
		"actions": [{"key": key, "label": m["label"], "description": m["description"], "critical": m["critical"]} for key, m in FLOOR_ACTIONS.items()],
		"matrix": _floor_matrix(),
	}


@frappe.whitelist()
def save_floor_permissions(changes: list | str) -> dict:
	"""`changes` is `[{staff_role, action, allowed}, ...]` - only the cells actually toggled. Refused (and
	nothing written) if any cell is unknown, or if it would take a critical action away from Owner/Manager:
	that would leave an outlet with nobody able to open a shift or approve a refund."""
	_require_manage_permission()
	changes = frappe.parse_json(changes) if isinstance(changes, str) else changes

	for c in changes:
		role, action = c.get("staff_role"), c.get("action")
		if role not in FLOOR_ROLES or action not in FLOOR_ACTIONS:
			frappe.throw("Unknown role or floor action", frappe.ValidationError)
		if role == "Owner/Manager" and FLOOR_ACTIONS[action]["critical"] and not c.get("allowed"):
			frappe.throw(f"Owner/Manager must always be able to: {FLOOR_ACTIONS[action]['label']}", frappe.ValidationError)

	current = _floor_matrix()
	applied = []
	for c in changes:
		role, action, allowed = c["staff_role"], c["action"], bool(c["allowed"])
		if current[role][action] == allowed:
			continue
		name = f"{role}-{action}"
		if frappe.db.exists("Floor Permission", name):
			frappe.db.set_value("Floor Permission", name, "allowed", 1 if allowed else 0)
		else:
			frappe.get_doc({"doctype": "Floor Permission", "staff_role": role, "action": action, "allowed": 1 if allowed else 0}).insert(ignore_permissions=True)
		applied.append(f"{role} · {FLOOR_ACTIONS[action]['label']} -> {'on' if allowed else 'off'}")

	if applied:
		frappe.get_doc(
			{
				"doctype": "Activity Log",
				"subject": "Back Office floor permissions changed",
				"content": "; ".join(applied),
				"reference_doctype": "Floor Permission",
				"status": "Success",
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"applied": len(applied)}


def floor_permission_rows() -> list:
	"""What pull_changes sends to the Hub: every explicit row (defaults are the Hub's own)."""
	return frappe.get_all("Floor Permission", fields=["staff_role", "action", "allowed"])

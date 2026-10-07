# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P8.5-08 (Back Office Access rights): Security & Access §6 already names these five base
# roles ("Base roles: Owner/Manager (full access), Shift Supervisor..., Server/Waiter..., Kitchen
# Staff..., Cashier...") — creating them as real Frappe Role records is completing that already-
# documented mechanism, not inventing a new policy. Deciding WHO gets which role, and the finer
# Owner/Area-manager/Branch-manager split, stays decision P0-12's own call — untouched here.

import frappe

BASE_ROLES = ["Owner/Manager", "Shift Supervisor", "Server/Waiter", "Kitchen Staff", "Cashier"]
# Ticket P8.5-13: the account the Hub signs in as. Not a Back Office role (never in the Access rights matrix);
# it exists so the Hub-facing sync methods can refuse every other logged-in user.
HUB_SERVICE_ROLE = "Hub Service"


def ensure_complete_custom_perms(doctype: str) -> None:
	"""Once a doctype has ANY Custom DocPerm row, Frappe uses the custom rows INSTEAD of the doctype's own
	(standard) permission rows. A custom set that is only partly there - e.g. one role's row added by hand
	while the standard higher-permlevel rows were never copied across - fails Frappe's own validation
	("Permission at level 0 must be set before higher levels") the next time anything is toggled, which is
	exactly how the Access rights screen broke for Outlet. This makes the custom set a complete copy of
	the standard one before any change is made to it; idempotent, and it never alters an existing row."""
	std_rows = frappe.get_all("DocPerm", filters={"parent": doctype}, fields=["*"])
	have = {(r.role, r.permlevel, r.if_owner or 0) for r in frappe.get_all("Custom DocPerm", filters={"parent": doctype}, fields=["role", "permlevel", "if_owner"])}
	skip = {"name", "creation", "modified", "modified_by", "owner", "docstatus", "idx", "parent", "parenttype", "parentfield"}
	# level 0 first, so higher levels always have their base
	for row in sorted(std_rows, key=lambda r: r.permlevel or 0):
		if (row.role, row.permlevel, row.if_owner or 0) in have:
			continue
		values = {k: v for k, v in row.items() if k not in skip}
		frappe.get_doc({**values, "doctype": "Custom DocPerm", "parent": doctype, "parenttype": "DocType", "parentfield": "permissions"}).insert(ignore_permissions=True)
	frappe.clear_cache(doctype=doctype)


# Working default, not a decision: security & access decision P0-12 (the finer Owner / Area-manager / Branch-manager
# split) is still pending, so what an Owner/Manager may do out of the box is a starting point the Access rights
# matrix then owns.
OWNER_DEFAULTS_FLAG = "restaurant_pos_owner_defaults_v1"
OWNER_LOOKUP_DOCTYPES = ("Branch", "POS Profile", "Price List", "Warehouse")


def seed_owner_defaults():
	"""Gives Owner/Manager the permissions the Back Office needs to work at all: full access to the restaurant records
	in the Access rights matrix (read-only on the audit records, which can only ever be read - guards/immutability.py).
	Without it a restaurant owner who is not Administrator opens the Back Office and every screen is refused (403) until a
	System Manager has ticked the matrix by hand. Runs ONCE per site: afterwards the matrix is the only authority, so
	a permission an administrator switches off is never switched back on by the next migrate."""
	from restaurant_pos.restaurant_pos.api.access_rights import AUDIT_DOCTYPES, MATRIX_DOCTYPES, PTYPES, _set_permission

	# ERPNext's own masters the Outlet setup form offers in its drop-downs (which Branch / POS Profile / Price List /
	# Warehouse an outlet maps to). READ only, and not part of the Access rights matrix, so this step is idempotent
	# rather than once-only: it only fills in a role that has no row at all. ensure_complete_custom_perms first, because
	# adding one custom row to an ERPNext doctype would otherwise replace its standard permission rows.
	from frappe.permissions import add_permission

	for doctype in OWNER_LOOKUP_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		ensure_complete_custom_perms(doctype)
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": "Owner/Manager", "permlevel": 0}):
			add_permission(doctype, "Owner/Manager", 0)

	if frappe.db.get_default(OWNER_DEFAULTS_FLAG):
		frappe.db.commit()
		return

	for doctype in MATRIX_DOCTYPES:
		for ptype in PTYPES:
			if doctype in AUDIT_DOCTYPES and ptype != "read":
				continue
			_set_permission(doctype, "Owner/Manager", ptype, True)
	frappe.db.set_default(OWNER_DEFAULTS_FLAG, "1")
	frappe.db.commit()


def ensure_base_roles():
	for role_name in BASE_ROLES:
		if frappe.db.exists("Role", role_name):
			continue
		frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(ignore_permissions=True)
	if not frappe.db.exists("Role", HUB_SERVICE_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": HUB_SERVICE_ROLE, "desk_access": 0}).insert(ignore_permissions=True)
	# The Hub account can see only outlets it is allowed (User Permission on Outlet scopes it to one - ticket
	# P1-14's per-outlet credential); read is all it ever needs from Frappe's permission engine, since its
	# writes are done by the guarded push_* methods themselves.
	from frappe.permissions import add_permission

	ensure_complete_custom_perms("Outlet")
	add_permission("Outlet", HUB_SERVICE_ROLE, 0)
	frappe.db.commit()

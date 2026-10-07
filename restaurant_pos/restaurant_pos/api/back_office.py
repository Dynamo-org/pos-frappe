# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P8.5-01/02: the React Back Office (`apps/back-office`) is served at /back-office from
# this same Frappe session — no separate login, token store or permission engine (Security &
# Access §5-6). Every method here relies on Frappe's own role + User Permission enforcement
# (frappe.get_list without ignore_permissions) rather than re-checking outlet scope by hand, per
# CLAUDE.md rule 9 and decision P0-12 ("Owner / Area manager / Branch manager via Frappe User
# Permissions on Outlet" — the role/assignment policy itself is still open; this is only the
# mechanism, which already works correctly with zero User Permission records configured (every
# role with read access to Outlet sees every outlet) or with any number added later.

import frappe

from restaurant_pos.restaurant_pos.api.scope import is_org_wide, require_logged_in


def _outlets_for_current_user():
	# frappe.get_list applies standard permission filtering: the caller's role permission on
	# Outlet (today, only System Manager has any), narrowed further by any User Permission
	# records on Outlet for this user. No custom branch-scope filtering is written here — this
	# is deliberately just Frappe's own mechanism, per decision P0-12's own stated design.
	return frappe.get_list(
		"Outlet",
		fields=["name", "outlet_name", "short_code", "city", "hub_mode", "branch"],
		order_by="outlet_name asc",
	)


# Ticket P8.5-14: the Desk screens the Back Office links into. Whether the person may OPEN each is decided by
# Frappe's own permission engine (role + branch scope) and reported here so the app can hide a link instead of
# sending someone to a "not permitted" page; Desk enforces it again on arrival either way.
DESK_LINK_DOCTYPES = (
	"Outlet", "Floor", "Restaurant Table", "Station", "Printer", "Device", "Staff PIN", "Item", "Item Price",
	"Void Log", "Refund Log", "Sync Dead Letter", "Sales Invoice", "POS Opening Entry", "POS Closing Entry",
	"Stock Entry", "User", "Role",
)
DESK_LINK_REPORTS = ("Sales Register", "Item-wise Sales Register")


def _desk_access() -> dict:
	access = {d: bool(frappe.has_permission(d, "read")) for d in DESK_LINK_DOCTYPES}
	for report in DESK_LINK_REPORTS:
		access[f"report:{report}"] = bool(frappe.db.exists("Report", report)) and bool(frappe.has_permission("Report", "read", doc=report))
	return access


@frappe.whitelist()
def get_my_outlets():
	require_logged_in()
	return _outlets_for_current_user()


@frappe.whitelist()
def get_bootstrap():
	"""One round trip for the Back Office shell: identity plus the branch switcher's own data."""
	require_logged_in()
	return {
		"user": frappe.session.user,
		"full_name": frappe.utils.get_fullname(frappe.session.user),
		"outlets": _outlets_for_current_user(),
		# Ticket P8.5-03: what this person may touch - the UI hides what the server would refuse anyway.
		# org_wide = no branch/outlet restriction (an owner); false = a branch-scoped manager.
		"org_wide": is_org_wide(),
		"desk_access": _desk_access(),
		"roles": [r for r in frappe.get_roles() if r in ("Owner/Manager", "Shift Supervisor", "Server/Waiter", "Kitchen Staff", "Cashier", "System Manager")],
	}

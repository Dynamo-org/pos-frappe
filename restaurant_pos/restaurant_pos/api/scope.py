# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Tickets P8.5-03 / P8.5-13: ONE place for the server-side rules every Back Office API method shares,
# so no method invents its own and none is left to "the UI hides it".
#
# The model (decision P0-12's mechanism; the role/assignment policy itself is still the client's call):
#   - ROLE says what kind of thing a person may do (Frappe roles + the Access rights matrix).
#   - SCOPE says where: Frappe User Permissions on Outlet and/or Branch. A person with NO such user
#     permission is ORG-WIDE (an owner); one with any is BRANCH-SCOPED and sees/changes only what falls
#     under those outlets/branches.
# Things that are shared across every outlet (the menu items themselves, roles, the access matrix,
# modifier groups, floor permissions) can only be changed by an org-wide person: a branch manager
# editing a shared dish or granting roles would change every other branch.
#
# Known limit (Frappe's own behaviour, flagged): a User Permission on Branch lets through an Outlet whose
# `branch` is EMPTY (unless the site turns on "Apply Strict User Permissions"). Keep every outlet's
# branch filled in; tests/verify_back_office_api.py proves the scoping for outlets that have one.

import frappe

HUB_SERVICE_ROLE = "Hub Service"
SCOPE_DOCTYPES = ("Outlet", "Branch")


def is_org_wide(user: str | None = None) -> bool:
	"""True if `user` has no branch/outlet restriction at all. Administrator and a System Manager with no
	User Permission on Outlet/Branch are org-wide; so is an owner with none."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return not frappe.db.exists("User Permission", {"user": user, "allow": ["in", list(SCOPE_DOCTYPES)]})


def require_org_wide(what: str) -> None:
	if not is_org_wide():
		frappe.throw(f"{what} affects every branch, so only an owner (not a branch-scoped manager) can do it", frappe.PermissionError)


def require_logged_in() -> None:
	if frappe.session.user == "Guest":
		frappe.throw("Not permitted", frappe.PermissionError)


def require_outlet(outlet: str, ptype: str = "read") -> None:
	"""The caller may `ptype` this outlet - role permission AND branch/outlet scope (never an outlet id
	taken on trust from the client, CLAUDE.md rule 9)."""
	if not outlet or not frappe.db.exists("Outlet", outlet):
		frappe.throw("Not permitted for this outlet", frappe.PermissionError)  # same answer as "exists but not yours": no probing
	if not frappe.has_permission("Outlet", ptype, doc=outlet):
		frappe.throw("Not permitted for this outlet", frappe.PermissionError)


def permitted_outlets(ptype: str = "read") -> list[str]:
	"""Outlet names the caller may `ptype` (Frappe applies role + User Permission filtering)."""
	if ptype == "read":
		return frappe.get_list("Outlet", pluck="name")
	return [o for o in frappe.get_list("Outlet", pluck="name") if frappe.has_permission("Outlet", ptype, doc=o)]


def require_hub_service() -> None:
	"""The Hub-facing sync methods (push_*, pull_changes, consume_pairing_code) are for the Hub's service
	user only - a logged-in Back Office user (even a System Manager) must not be able to post invoices
	or read every outlet's staff hashes through them. Administrator holds every role (a bench
	console call works); the Hub itself signs in with its own pairing-issued credential, which holds this role only (P1-14)."""
	if HUB_SERVICE_ROLE not in frappe.get_roles():
		frappe.throw("This method is only available to the Hub service account", frappe.PermissionError)
	# Ticket P1-23: refuse a sync API version this site no longer supports; record what the Hub reported.
	from restaurant_pos.restaurant_pos.api.versions import note_hub_call

	note_hub_call()


# ---------------------------------------------------------------------------------------------------------
# Outlet scope for every outlet-bearing doctype (tickets P8.5-03 / P8.5-13).
#
# Frappe's own User Permission on Branch only narrows doctypes that carry a Link to Branch - which is just
# Outlet. A branch manager restricted to Branch A would therefore still list every Floor, Station, Printer,
# Device, Void Log ... of every other branch through /api/resource (found by the harness). These hooks make
# one rule hold for all of them: a branch/outlet-scoped user reaches a record only if its OUTLET is one
# they are scoped to. Kinds of restriction combine the way Frappe's do: a user with both Branch and Outlet
# permissions must satisfy both.

OUTLET_SCOPED_DOCTYPES = (
	"Floor",
	"Restaurant Table",
	"Station",
	"Printer",
	"Device",
	"Pairing Code",
	"Void Log",
	"Refund Log",
	"Availability Log",
	"KOT",
	"Sync Dead Letter",
	"Guest Consent",
)


def scoped_outlet_names(user: str | None = None) -> list[str] | None:
	"""None = unrestricted. Otherwise the outlet names the user is allowed (possibly empty)."""
	user = user or frappe.session.user
	if user == "Administrator":
		return None
	cache = getattr(frappe.local, "_outlet_scope_cache", None)
	if cache is None:
		cache = frappe.local._outlet_scope_cache = {}
	if user in cache:
		return cache[user]
	rows = frappe.get_all("User Permission", filters={"user": user, "allow": ["in", list(SCOPE_DOCTYPES)]}, fields=["allow", "for_value"])
	if not rows:
		cache[user] = None
		return None
	allowed: set[str] | None = None
	by_outlet = {r.for_value for r in rows if r.allow == "Outlet"}
	by_branch = {r.for_value for r in rows if r.allow == "Branch"}
	if by_outlet:
		allowed = set(by_outlet)
	if by_branch:
		under = set(frappe.get_all("Outlet", filters={"branch": ["in", list(by_branch)]}, pluck="name"))
		allowed = under if allowed is None else allowed & under
	cache[user] = sorted(allowed or [])
	return cache[user]


def _in_list(column: str, values: list[str]) -> str:
	if not values:
		return "1=0"
	return f"{column} in ({', '.join(frappe.db.escape(v) for v in values)})"


def outlet_scope_conditions(user: str | None = None, doctype: str | None = None) -> str:
	"""permission_query_conditions hook: restricts lists of an outlet-bearing doctype to the user's outlets."""
	allowed = scoped_outlet_names(user)
	if allowed is None or not doctype:
		return ""
	if doctype == "Outlet":
		return _in_list("`tabOutlet`.`name`", allowed)
	if doctype == "Staff PIN":
		if not allowed:
			return "1=0"
		return (
			"`tabStaff PIN`.`name` in (select `parent` from `tabStaff Outlet Assignment` "
			f"where `parenttype`='Staff PIN' and {_in_list('`outlet`', allowed)})"
		)
	if doctype == "Item Price":
		prices = frappe.get_all("Outlet", filters={"name": ["in", allowed or [""]]}, pluck="price_list")
		return _in_list("`tabItem Price`.`price_list`", [p for p in prices if p])
	return _in_list(f"`tab{doctype}`.`outlet`", allowed)


def outlet_scope_permission(doc, ptype=None, user=None, debug=False):
	"""has_permission hook: the single-record version of the rule above. Must return True to allow - Frappe
	treats None as a refusal."""
	allowed = scoped_outlet_names(user)
	if allowed is None:
		return True
	doctype = doc.doctype
	if doctype == "Outlet":
		return doc.name in allowed
	if doctype == "Staff PIN":
		return any(a.outlet in allowed for a in (doc.get("assignments") or [])) if not doc.is_new() else True
	if doctype == "Item Price":
		prices = {p for p in frappe.get_all("Outlet", filters={"name": ["in", allowed or [""]]}, pluck="price_list") if p}
		return doc.price_list in prices
	return doc.get("outlet") in allowed if doc.get("outlet") else True


SCOPED_HOOK_DOCTYPES = ("Outlet", "Staff PIN", "Item Price", *OUTLET_SCOPED_DOCTYPES)

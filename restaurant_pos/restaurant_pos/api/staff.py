# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P1-27's PIN-pattern rule (Validation & Conventions §3): "all-same digits and straight
# runs rejected". A pure, standalone function rather than inline in the doctype so it can be
# unit-tested directly (the ticket's own "Unit: PIN pattern validator" test case) even before the
# real set/reset-PIN whitelisted method (Back Office Staff screen, M3) exists to call it — the
# Hub itself never sees a plaintext PIN to validate; this only ever runs on the Frappe side, at
# the moment a PIN is chosen.

import frappe

from restaurant_pos.restaurant_pos.api.scope import is_org_wide, permitted_outlets, require_outlet


def is_simple_pin(pin: str) -> bool:
	"""True if `pin` is a pattern the ticket explicitly calls out as too weak: all one digit
	(1111), or a straight run ascending or descending (1234, 4321, 123456)."""
	if len(pin) < 4:
		return True
	if len(set(pin)) == 1:
		return True
	digits = [int(d) for d in pin]
	ascending = all(digits[i] + 1 == digits[i + 1] for i in range(len(digits) - 1))
	descending = all(digits[i] - 1 == digits[i + 1] for i in range(len(digits) - 1))
	return ascending or descending


# Ticket P8.5-06 (Back Office Staff and branch assignments) — everything except Reset PIN, which
# needs a real security decision first (Frappe has no access to HUB_PIN_PEPPER and no Argon2
# dependency today; tracked separately as P4-34, not decided silently here). Staff creation here
# deliberately does NOT also create an ERPNext "Employee" record — Frontend Specs C.12 mentions
# one, but nothing else in this whole project (Hub sync, PIN login, P1-15/27/28) reads or writes
# Employee at all; Staff PIN alone is the real, load-bearing identity record every other ticket
# already depends on, so that's what this adds to, not a parallel HR record nothing consumes.


def _staff_outlets(staff: str) -> list[str]:
	return frappe.get_all("Staff Outlet Assignment", filters={"parent": staff, "parenttype": "Staff PIN"}, pluck="outlet")


def _staff_in_scope(staff: str) -> bool:
	if is_org_wide():
		return True
	mine = set(permitted_outlets("read"))
	return any(o in mine for o in _staff_outlets(staff))


def _require_staff_in_scope(staff: str) -> None:
	if not frappe.db.exists("Staff PIN", staff) or not _staff_in_scope(staff):
		frappe.throw("Not permitted for this staff member", frappe.PermissionError)  # same answer for missing and not-yours


@frappe.whitelist()
def list_staff() -> list:
	if not frappe.has_permission("Staff PIN", "read"):
		frappe.throw("Not permitted to view staff", frappe.PermissionError)
	rows = frappe.get_list(
		"Staff PIN", fields=["name", "full_name", "staff_number", "active", "pin_reset_requested_at"], order_by="full_name asc"
	)
	# Ticket P8.5-03: a staff member belongs to the outlets they are assigned to; a branch-scoped manager sees
	# only people assigned somewhere inside their scope (unassigned people belong to nobody in particular,
	# so only an owner sees them).
	rows = [r for r in rows if _staff_in_scope(r.name)]
	for row in rows:
		row["has_pin"] = bool(frappe.db.get_value("Staff PIN", row.name, "pin_hash"))
		row["pin_reset_pending"] = bool(row.pop("pin_reset_requested_at"))
		# frappe.get_list on the child doctype directly (filtering by parent/parenttype) silently
		# ignores requested `fields` beyond `name` — found live (every row came back "undefined:
		# undefined" in the UI). Reading the child table through its parent doc, the standard
		# Frappe pattern, is what actually returns the row data.
		row["assignments"] = [{"outlet": a.outlet, "role": a.role} for a in frappe.get_doc("Staff PIN", row.name).assignments]
	return rows


@frappe.whitelist()
def request_pin_reset(staff: str) -> dict:
	"""Ticket P4-34 (option A — the Hub stays the only thing that ever hashes a PIN, Frappe never
	gets a copy of HUB_PIN_PEPPER). This sets only a timestamp flag — no PIN, no hash, nothing
	sensitive crosses the boundary here. The person sets their own new PIN on a staff-app device
	once this flag reaches their outlet's Hub on the next pull; the Hub hashes it locally and
	pushes only the resulting hash back up (push_pin_reset, below)."""
	_require_staff_in_scope(staff)
	doc = frappe.get_doc("Staff PIN", staff)
	if not frappe.has_permission(doc=doc, ptype="write"):
		frappe.throw("Not permitted to reset this staff member's PIN", frappe.PermissionError)
	doc.pin_reset_requested_at = frappe.utils.now_datetime()
	doc.save()
	frappe.db.commit()
	return {"name": doc.name, "pin_reset_requested_at": str(doc.pin_reset_requested_at)}


@frappe.whitelist()
def save_staff(staff: dict | str) -> dict:
	"""`staff` is `{name?, full_name, staff_number?, active, assignments: [{outlet, role}, ...]}`.
	`assignments` is replaced wholesale on every save — simplest correct behaviour for a small
	per-person list edited as a whole from one side panel, matching Frontend Specs C.12's own
	"Branch assignments table... Add branch" editor shape (no independent per-row API)."""
	staff = frappe.parse_json(staff) if isinstance(staff, str) else staff
	assignments = staff.get("assignments") or []

	for a in assignments:
		require_outlet(a["outlet"], "write")

	kept_elsewhere = []  # assignments at outlets this person cannot touch: carried over, never dropped
	if staff.get("name"):
		_require_staff_in_scope(staff["name"])
		doc = frappe.get_doc("Staff PIN", staff["name"])
		if not frappe.has_permission(doc=doc, ptype="write"):
			frappe.throw("Not permitted to edit this staff member", frappe.PermissionError)
		writable = set(permitted_outlets("write"))
		kept_elsewhere = [{"outlet": a.outlet, "role": a.role} for a in doc.assignments if a.outlet not in writable]
	else:
		if not frappe.has_permission("Staff PIN", "create"):
			frappe.throw("Not permitted to create staff", frappe.PermissionError)
		if not is_org_wide() and not assignments:
			frappe.throw("Assign the new staff member to at least one of your outlets", frappe.ValidationError)
		doc = frappe.new_doc("Staff PIN")

	doc.full_name = staff["full_name"]
	doc.staff_number = staff.get("staff_number") or None
	doc.active = 1 if staff.get("active", True) else 0
	doc.set("assignments", [])
	for a in [*assignments, *kept_elsewhere]:
		doc.append("assignments", {"outlet": a["outlet"], "role": a["role"]})
	doc.save()
	frappe.db.commit()
	return {"name": doc.name}

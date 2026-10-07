# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-29: "corrections are new linked entries". An audit record is never edited or deleted
# (guards/immutability.py); when one was wrong, an Owner/Manager records a correction - a brand
# new row of the same doctype that points back at the original (`correction_of`) and says why
# (`correction_reason`). Amount fields are negated so a sum over the log nets the mistake out.

import frappe
from frappe import _

from restaurant_pos.restaurant_pos.api.scope import require_outlet

from restaurant_pos.restaurant_pos.guards.immutability import AUDIT_DOCTYPES

CORRECTING_ROLES = ("Owner/Manager", "System Manager")
_SKIP_FIELDTYPES = {"Section Break", "Column Break", "Tab Break", "Table", "Table MultiSelect", "HTML", "Button"}
_SYSTEM_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "amended_from", "hub_uuid"}


@frappe.whitelist()
def record_correction(doctype: str, original: str, reason: str) -> dict:
	if doctype not in AUDIT_DOCTYPES:
		frappe.throw(_("{0} is not an audit record").format(doctype), frappe.ValidationError)
	if not set(frappe.get_roles()) & set(CORRECTING_ROLES):
		frappe.throw(_("Only an Owner/Manager can record a correction"), frappe.PermissionError)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("A correction needs a reason"), frappe.ValidationError)

	if not frappe.db.exists(doctype, original):
		frappe.throw(_("Not permitted"), frappe.PermissionError)  # same answer for missing and not-yours
	source = frappe.get_doc(doctype, original)
	if source.get("outlet"):
		require_outlet(source.outlet, "read")  # a branch manager corrects only their own outlets' records
	if source.get("correction_of"):
		frappe.throw(_("{0} is itself a correction; correct the original instead").format(original), frappe.ValidationError)

	values = {}
	for field in frappe.get_meta(doctype).fields:
		if field.fieldtype in _SKIP_FIELDTYPES or field.fieldname in _SYSTEM_FIELDS:
			continue
		values[field.fieldname] = source.get(field.fieldname)
	if values.get("amount"):
		values["amount"] = -values["amount"]
	values.update(
		{
			"doctype": doctype,
			"correction_of": source.name,
			"correction_reason": reason,
			# unique per row; marks it as not Hub-originated and never collides with a real push.
			"hub_uuid": f"correction-{frappe.generate_hash(length=16)}",
		}
	)
	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)  # permission was checked above: roles, not doctype create
	doc.submit()
	frappe.db.commit()
	return {"name": doc.name, "correction_of": source.name}

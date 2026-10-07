# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Tickets P3-29 (immutable audit records) and P3-05 (the down/up flow rule, ERPNext half).
#
# Transactions are created only on the Hub and are append-only (CLAUDE.md rule 4). Once one has
# synced into ERPNext it is read-only here - for EVERY role, System Manager and Administrator
# included, because these are doc events (they run on every save/cancel/delete path) rather than
# permissions (which Administrator bypasses). A correction is never an edit: it is a NEW linked
# entry (api.audit.record_correction) or, for a bill, a credit note (the refund push already does
# that).
#
# What is guarded:
#   - our own audit doctypes (Void Log, Refund Log, Availability Log, KOT): always.
#   - ERPNext's native Sales Invoice / POS Opening Entry / POS Closing Entry: only the rows the Hub
#     created (those carrying a hub_uuid) - invoices an accountant enters by hand in Desk are not
#     ours to lock.
#
# The Hub's own insert-then-submit is let through: the guard only refuses changes to a row that
# was ALREADY submitted when the save began, so draft -> submit passes and nothing after it does.
#
# The one deliberate bypass is `frappe.flags.allow_audit_purge`, settable only by server-side
# code (a bench console, or the future retention/archiving job P3-35) - never from the Desk UI or
# any API call - so test data and legally-expired records can still be removed on purpose.

import frappe
from frappe import _

AUDIT_DOCTYPES = ("Void Log", "Refund Log", "Availability Log", "KOT")
# Ticket P4-25: append-only like the audit records, but with no correction mechanism - consent is
# corrected the only legitimate way: the guest changes their answer, which is a NEW row.
APPEND_ONLY_DOCTYPES = ("Guest Consent",)
HUB_ORIGINATED_DOCTYPES = ("Sales Invoice", "POS Opening Entry", "POS Closing Entry")


def _guarded(doc) -> bool:
	if doc.doctype in AUDIT_DOCTYPES or doc.doctype in APPEND_ONLY_DOCTYPES:
		return True
	return doc.doctype in HUB_ORIGINATED_DOCTYPES and bool(doc.get("hub_uuid"))


def _refuse(doc, what: str):
	frappe.throw(
		_("{0} {1} is an audit record created by the Hub and cannot be {2}. Record a correction instead - a new entry that points back at it.").format(
			_(doc.doctype), doc.name, what
		),
		frappe.PermissionError,
		title=_("Audit record is read-only"),
	)


# ERPNext itself legitimately re-saves a few STATUS fields on these documents as part of its own
# lifecycle (closing a shift flips the POS Opening Entry to "Closed" and links the closing entry; a
# credit note updates the original invoice's status/outstanding). Those, and only those, are allowed;
# a change to anything else - amounts, lines, dates, who/what - is refused. Found by the live shift-
# close test (tests/verify_business_date.py): without this, closing any shift was blocked.
SYSTEM_MANAGED_FIELDS = {
	"POS Opening Entry": {"status", "pos_closing_entry"},
	"POS Closing Entry": {"status"},
	"Sales Invoice": {"status", "outstanding_amount", "paid_amount", "per_returned", "consolidated_invoice"},
}
_IGNORED_FIELDS = {"modified", "modified_by"}


def _only_system_changes(doc, before) -> bool:
	allowed = SYSTEM_MANAGED_FIELDS.get(doc.doctype)
	if not allowed or before is None:
		return False
	changed = {f for f in doc.meta.get_valid_columns() if f not in _IGNORED_FIELDS and doc.get(f) != before.get(f)}
	# child tables: any difference in row content is a real edit
	for table in doc.meta.get_table_fields():
		if [r.as_dict(no_default_fields=True) for r in doc.get(table.fieldname)] != [r.as_dict(no_default_fields=True) for r in before.get(table.fieldname)]:
			return False
	return changed <= allowed


def block_edit_after_submit(doc, method=None):
	"""validate: refuses any save of a row that was already submitted before this save began."""
	if frappe.flags.get("allow_audit_purge") or not _guarded(doc) or doc.is_new():
		return
	before = doc.get_doc_before_save()
	if before is not None and before.docstatus == 1 and not _only_system_changes(doc, before):
		_refuse(doc, _("edited"))


def block_update_after_submit(doc, method=None):
	if frappe.flags.get("allow_audit_purge") or not _guarded(doc):
		return
	if _only_system_changes(doc, doc.get_doc_before_save()):
		return
	_refuse(doc, _("edited"))


def block_cancel(doc, method=None):
	if frappe.flags.get("allow_audit_purge") or not _guarded(doc):
		return
	_refuse(doc, _("cancelled"))


def block_delete(doc, method=None):
	# A draft is not a record yet (push_pos_invoice discards its own just-built draft when the total
	# mismatches); only a submitted/cancelled row is part of the audit trail.
	if frappe.flags.get("allow_audit_purge") or not _guarded(doc) or doc.docstatus == 0:
		return
	_refuse(doc, _("deleted"))


GUARD_EVENTS = {
	"validate": "restaurant_pos.restaurant_pos.guards.immutability.block_edit_after_submit",
	"before_update_after_submit": "restaurant_pos.restaurant_pos.guards.immutability.block_update_after_submit",
	"before_cancel": "restaurant_pos.restaurant_pos.guards.immutability.block_cancel",
	"on_trash": "restaurant_pos.restaurant_pos.guards.immutability.block_delete",
}

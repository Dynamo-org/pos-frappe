# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P4-33: a reusable set of choices (Size, Spice level, Add-ons) that menu items link to.
# The staff app's cart picker, the KOT/KDS and the bill all read this one definition — the Hub
# re-validates every chosen option against its own synced copy, never trusting a client-sent price.

import frappe
from frappe.model.document import Document


class ModifierGroup(Document):
	def validate(self):
		if not self.options:
			frappe.throw("Add at least one option to this group.", frappe.ValidationError)
		seen = set()
		for row in self.options:
			key = (row.option_name or "").strip().lower()
			if key in seen:
				frappe.throw(f"Option '{row.option_name}' appears twice in this group.", frappe.ValidationError)
			seen.add(key)
		if self.is_required and all(row.disabled for row in self.options):
			frappe.throw("A required group needs at least one enabled option.", frappe.ValidationError)

# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class StaffPIN(Document):
	def validate(self):
		# Ticket P1-27: "A staff member must have at least one outlet assignment with a role
		# before the PIN works anywhere" — enforced here, not left to the Hub, since master data
		# is only ever written in Frappe (CLAUDE.md rule 4).
		if not self.assignments:
			frappe.throw(
				"At least one outlet assignment is required before this staff member can log in.",
				frappe.ValidationError,
			)

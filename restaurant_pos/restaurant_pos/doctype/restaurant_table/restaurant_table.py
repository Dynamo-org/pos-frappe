# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class RestaurantTable(Document):
	def validate(self):
		# The json's own field description already promises "1-10 characters" — enforcing it here
		# closes a real gap (the class body was a no-op). Per-outlet uniqueness needs no separate
		# check: autoname ("format:{outlet}-{label}") already makes a duplicate label under the
		# same outlet fail as a naming collision on insert.
		if not (1 <= len(self.label or "") <= 10):
			frappe.throw("Table label must be 1-10 characters.", frappe.ValidationError)

# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

import re

import frappe
from frappe.model.document import Document

from restaurant_pos.restaurant_pos.api.gstin import validate_gstin, gstin_state_name

# Ticket P1-22: "Outlet must have Company, GSTIN address, POS Profile, Price List, Warehouse and
# invoice series before any device can pair" — checked at pairing time (pairing.py), not here,
# so an outlet can still be created and saved incrementally while its ERP links are being set up.
ERP_MAPPING_FIELDS = ["company", "branch", "gstin_address", "pos_profile", "price_list", "warehouse", "invoice_series_prefix"]

# Ticket P4-14: a real UPI VPA is "<handle>@<bank/PSP handle>" — no central registry of valid PSP
# handles exists to check against (unlike GSTIN's own official state-code table), so this is
# deliberately a loose sanity check (catches an empty half, stray spaces, a missing '@'), not a
# claim that the bank handle itself is real.
_UPI_VPA_SHAPE = re.compile(r"^[\w.\-]{2,256}@[A-Za-z]{2,64}$")


class Outlet(Document):
	def validate(self):
		if self.gstin:
			validate_gstin(self.gstin)
			self._validate_gstin_state_matches_address()
		if self.upi_vpa and not _UPI_VPA_SHAPE.match(self.upi_vpa.strip()):
			frappe.throw("This UPI VPA doesn't look right — it should look like name@bankhandle.", frappe.ValidationError)

	def _validate_gstin_state_matches_address(self):
		if not self.gstin_address:
			return
		address_state = frappe.db.get_value("Address", self.gstin_address, "state")
		expected_state = gstin_state_name(self.gstin)
		if address_state and expected_state and expected_state.lower() not in address_state.lower():
			frappe.throw(
				f"This GSTIN's state ({expected_state}) doesn't match the GSTIN Address's state ({address_state}).",
				frappe.ValidationError,
			)

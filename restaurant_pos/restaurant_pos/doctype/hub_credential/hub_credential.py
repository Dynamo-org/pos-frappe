# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P1-14: one row per credential issued to a Hub (see api/hub_credentials.py). The row is the
# record of WHO may call Frappe as the Hub; the secret itself lives only in the Frappe user it points
# at. A revoked row never comes back: a new credential is a new row (and a new pairing code).

import frappe
from frappe import _
from frappe.model.document import Document


class HubCredential(Document):
	def validate(self):
		before = self.get_doc_before_save()
		# status moves only through api/hub_credentials.py (which sets this flag) - never by editing the row
		if not frappe.flags.hub_credential_lifecycle:
			if not before and self.status != "Awaiting pairing":
				frappe.throw(_("A new Hub credential starts as Awaiting pairing."), frappe.ValidationError)
			if before and before.status != self.status:
				frappe.throw(_("A Hub credential's status changes only by pairing, or by Revoke."), frappe.ValidationError)
		if before and before.status == "Revoked" and self.status != "Revoked":
			frappe.throw(_("A revoked Hub credential cannot be reinstated. Issue a new one."), frappe.ValidationError)
		if before and before.status != "Awaiting pairing" and before.outlet != self.outlet:
			frappe.throw(_("A paired credential's scope cannot be changed. Revoke it and issue a new one."), frappe.ValidationError)

	def on_trash(self):
		frappe.throw(_("Hub credentials are kept as a record. Revoke it instead of deleting it."), frappe.PermissionError)

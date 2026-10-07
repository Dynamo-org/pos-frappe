# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-04: one row per latency-sensitive event ERPNext wants a Hub to apply (api/hub_push.py). Rows are written only by
# that module; nobody edits them by hand, and they are kept as the delivery record.

from frappe.model.document import Document


class HubEvent(Document):
	pass

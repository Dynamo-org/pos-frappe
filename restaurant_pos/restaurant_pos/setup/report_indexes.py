# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-35 (Tech Architecture §7): the reports that matter - "this outlet, this date range" - must stay fast as a chain
# grows. Frappe indexes a doctype's name and a few standard columns only, so every such lookup on the log doctypes would
# scan the whole table. These composite indexes put the outlet first (every report is scoped to an outlet or a set of them)
# and the date second, so one outlet's month is a range scan, not a table scan.
#
# Idempotent: run by after_migrate on every site; an index that exists is left alone, a column that does not exist
# (a site without the branch dimension) is skipped.
#
# The date-only indexes (rp_fired, rp_occurred) are for reports ACROSS outlets ("every outlet, yesterday"): a composite index
# led by the outlet cannot serve a date range on its own (found by tests/verify_report_perf.py: no gain without them).
#
# On a large production site, creating an index on ERPNext's own Sales Invoice table takes time and a table lock: run `bench
# migrate` for the first release that carries this in a quiet window.

import frappe

# doctype -> [(index name, columns)]. Names are short and prefixed so they are easy to recognise and never clash with Frappe's.
REPORT_INDEXES = {
	"KOT": [("rp_outlet_fired", ("outlet", "fired_at")), ("rp_fired", ("fired_at",)), ("rp_order", ("restaurant_order",))],
	"Void Log": [("rp_outlet_occurred", ("outlet", "occurred_at")), ("rp_occurred", ("occurred_at",)), ("rp_order", ("restaurant_order",))],
	"Refund Log": [("rp_outlet_occurred", ("outlet", "occurred_at")), ("rp_occurred", ("occurred_at",)), ("rp_order", ("restaurant_order",))],
	"Availability Log": [("rp_outlet_occurred", ("outlet", "occurred_at")), ("rp_occurred", ("occurred_at",))],
	"Guest Consent": [("rp_outlet_captured", ("outlet", "captured_at"))],
	"Sync Dead Letter": [("rp_outlet_created", ("outlet", "creation"))],
	# ERPNext's own invoice: reports by branch (an outlet's branch) and posting date
	"Sales Invoice": [("rp_branch_posting", ("branch", "posting_date"))],
}


def ensure_report_indexes() -> list[str]:
	"""Creates any missing report index. Returns the names it created (for the test and the migrate log)."""
	created = []
	for doctype, indexes in REPORT_INDEXES.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		table = f"tab{doctype}"
		columns = set(frappe.db.get_table_columns(doctype))
		for name, fields in indexes:
			if not set(fields) <= columns or frappe.db.has_index(table, name):
				continue
			frappe.db.add_index(doctype, list(fields), index_name=name)
			created.append(f"{doctype}.{name}")
	frappe.db.commit()
	return created

# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Without this, every Outlet under one Company is indistinguishable in P&L/GL/any standard
# ERPNext report — a real gap the user caught directly by inspecting a synced Sales Invoice.
# Fixed the ERPNext-idiomatic way: enable Branch as a real Accounting Dimension (Setup >
# Accounting Dimension normally does this by hand; this is the same thing, scripted and
# idempotent so every environment gets it on `bench migrate`, same pattern as billing_setup.py).
# Once enabled, ERPNext adds a `branch` field to every accounting doctype (Sales Invoice,
# Purchase Invoice, Journal Entry, GL Entry, ...) and every standard financial report becomes
# filterable/groupable by it — no custom report work needed.

import frappe


def ensure_branch_accounting_dimension():
	if frappe.db.exists("Accounting Dimension", {"document_type": "Branch"}):
		return

	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		make_dimension_in_accounting_doctypes,
	)

	doc = frappe.get_doc(
		{
			"doctype": "Accounting Dimension",
			"document_type": "Branch",
			"label": "Branch",
			"fieldname": "branch",
		}
	)
	doc.insert(ignore_permissions=True)
	# Ticket's own on_update() enqueues this to a background worker (frappe.enqueue, queue="long")
	# — real for a live site with a worker running, but this runs during `bench migrate` itself,
	# so it's called directly here rather than assuming a worker will ever pick the job up.
	make_dimension_in_accounting_doctypes(doc=doc)
	frappe.db.commit()

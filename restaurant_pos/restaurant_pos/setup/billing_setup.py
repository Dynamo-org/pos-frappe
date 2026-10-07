# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-07's minimum viable accounting setup: real GST ledger accounts, one Sales Taxes and
# Charges Template per company, and a default walk-in Customer, so a POS Invoice can actually post.
#
# Ticket P4-3x's own real, live-found discovery: a single flat-rate tax line (this file's original
# shape) is not just "simpler than CGST/SGST" — it's actively INCOMPATIBLE with per-item GST.
# ERPNext's item-wise tax override (Sales Invoice Item's item_tax_rate) only ever OVERRIDES the
# rate for a tax account that already has its own row on the invoice's header template; it never
# adds a new tax account row that isn't already there. Confirmed live: a real Sales Invoice built
# with an item whose Item Tax Template pointed at brand-new CGST/SGST accounts, with the header
# still using the old single "GST Output" account, silently computed tax on the OLD flat 5%
# account only — the item's own CGST/SGST rows never appeared in `doc.taxes` at all, because
# nothing on the header referenced those accounts. The fix: the header template itself must carry
# CGST + SGST rows on the SAME two canonical accounts every Item Tax Template slab (5%/12%/18%/28%)
# also uses — each slab template only ever varies the RATE on those same two accounts, never
# introduces new ones. Idempotent, run from hooks.py's after_migrate; upgrades an old single-row
# template in place rather than leaving it stuck on the incompatible shape.

import frappe


def ensure_billing_masters():
	for company in frappe.get_all("Company", pluck="name"):
		_ensure_tax_template(company)
	_ensure_walk_in_customer()
	# The M1 stand-in POS Opening Entry this used to auto-create per outlet (a $0-balance entry
	# under "Administrator", so P3-07's invoice sync had somewhere to post against) is gone now
	# that ticket P4-20 built the real thing: a supervisor opens the shift for real, with real
	# opening cash, from the staff app (services/hub/src/routes/shifts.ts). Leaving the old
	# stand-in in place would permanently block the real POS Opening Entry it was standing in
	# for — ERPNext refuses a second open entry per POS Profile.


def _ensure_gst_account(company, label):
	"""label is 'CGST' or 'SGST' — the two canonical accounts every Item Tax Template slab (and
	the invoice header template below) shares. Ticket P4-3x's own discovery (see this file's
	header comment) is exactly why there are two fixed accounts here, not one per slab."""
	abbr = frappe.db.get_value("Company", company, "abbr")
	account_name = f"{label} Output - {abbr}"
	if frappe.db.exists("Account", account_name):
		return account_name
	parent = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Tax", "is_group": 1, "root_type": "Liability"}, "name"
	)
	if not parent:
		return None
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": f"{label} Output",
			"company": company,
			"parent_account": parent,
			"account_type": "Tax",
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_tax_template(company):
	"""Keeps the same template name/title this project has always used ("GST 5%") so outlets
	already pointing their `taxes_and_charges` at it keep working unchanged — only the ROWS
	underneath it change shape. 2.5%/2.5% is the same working-default total (5%, pending P0-21)
	this template always had; per-item overrides are what actually vary the real rate per line."""
	template_name = f"GST 5% - {frappe.db.get_value('Company', company, 'abbr')}"
	cgst_account = _ensure_gst_account(company, "CGST")
	sgst_account = _ensure_gst_account(company, "SGST")
	if not cgst_account or not sgst_account:
		return None
	target_rows = [
		{"charge_type": "On Net Total", "account_head": cgst_account, "description": "CGST", "rate": 2.5},
		{"charge_type": "On Net Total", "account_head": sgst_account, "description": "SGST", "rate": 2.5},
	]
	if frappe.db.exists("Sales Taxes and Charges Template", template_name):
		doc = frappe.get_doc("Sales Taxes and Charges Template", template_name)
		current_accounts = {t.account_head for t in doc.taxes}
		if current_accounts == {cgst_account, sgst_account}:
			return template_name
		# Ticket P4-3x: upgrades an old single flat-rate row (found live to be actively
		# incompatible with per-item overrides, not just a simplification — see this file's own
		# header comment) to the real CGST+SGST shape, in place, rather than leaving new
		# environments correct and every existing one stuck.
		doc.taxes = []
		for row in target_rows:
			doc.append("taxes", row)
		doc.save(ignore_permissions=True)
		return doc.name
	doc = frappe.get_doc(
		{
			"doctype": "Sales Taxes and Charges Template",
			"title": "GST 5%",
			"company": company,
			"taxes": target_rows,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_walk_in_customer():
	if frappe.db.exists("Customer", "Walk-in Customer"):
		return
	group = frappe.db.get_value("Customer Group", {}, "name") or "All Customer Groups"
	territory = frappe.db.get_value("Territory", {}, "name") or "All Territories"
	frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": "Walk-in Customer",
			"customer_group": group,
			"territory": territory,
			"customer_type": "Individual",
		}
	).insert(ignore_permissions=True)

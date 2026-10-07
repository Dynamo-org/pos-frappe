# Removes test invoices a live Hub verification left behind (matched by hub_invoice_number prefix),
# through the server-side purge flag that the audit guards (guards/immutability.py) honour.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.cleanup_live.purge_invoices --kwargs '{"prefix": "ML/"}'

import frappe


def purge_invoices(prefix: str):
	frappe.set_user("Administrator")
	frappe.flags.allow_audit_purge = True
	originals = frappe.get_all("Sales Invoice", filters={"hub_invoice_number": ["like", f"{prefix}%"]}, pluck="name")
	# NEVER query with an empty list here: `return_against in [""]` matches every ordinary invoice.
	credit_notes = frappe.get_all("Sales Invoice", filters={"return_against": ["in", originals]}, pluck="name") if originals else []
	for name in [*credit_notes, *originals]:
		doc = frappe.get_doc("Sales Invoice", name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Sales Invoice", name, force=1, ignore_permissions=True)
	for name in frappe.get_all("Sync Dead Letter", filters={"payload_json": ["like", f"%{prefix}%"]}, pluck="name"):
		frappe.delete_doc("Sync Dead Letter", name, force=1, ignore_permissions=True)
	frappe.flags.allow_audit_purge = False
	frappe.db.commit()
	print(f"purged {len(originals)} invoices and {len(credit_notes)} credit notes")


def purge_hub_credentials(label_prefix: str = "zz"):
	"""Removes test Hub Credentials (and their service accounts) whose label starts with the prefix. Real credentials are
	never touched: the label must start with it, and a credential that is still ACTIVE is refused. Hub Credentials are
	records and cannot be deleted through the app (revoke instead); this is the server-side clean-up for test rows.
	  bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.cleanup_live.purge_hub_credentials --kwargs '{"label_prefix": "zz"}'"""
	frappe.set_user("Administrator")
	frappe.flags.hub_credential_lifecycle = True
	removed = 0
	for name in frappe.get_all("Hub Credential", filters={"label": ["like", f"{label_prefix}%"]}, pluck="name"):
		row = frappe.db.get_value("Hub Credential", name, ["status", "hub_user"], as_dict=True)
		if row.status == "Active":
			print(f"skipped {name}: still Active (revoke it first)")
			continue
		for up in frappe.get_all("User Permission", filters={"user": row.hub_user or "-"}, pluck="name"):
			frappe.delete_doc("User Permission", up, force=1, ignore_permissions=True)
		frappe.db.sql("delete from `tabHub Credential` where name=%s", name)
		frappe.db.sql("delete from `tabVersion` where docname=%s and ref_doctype='Hub Credential'", name)
		if row.hub_user and frappe.db.exists("User", row.hub_user):
			frappe.delete_doc("User", row.hub_user, force=1, ignore_permissions=True)
		removed += 1
	frappe.flags.hub_credential_lifecycle = False
	frappe.db.commit()
	print(f"removed {removed} test Hub Credential(s)")

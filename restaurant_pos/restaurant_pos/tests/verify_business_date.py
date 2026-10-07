# Ticket P3-33 (ERPNext half): a shift that runs past midnight. Uses a THROWAWAY outlet + POS Profile
# (copied from the dev outlet) so the real open shift is never touched; everything is removed at the end.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_business_date.run
#
# Proves: (1) Hub timestamps land in the site timezone (not UTC-as-local); (2) an invoice for the shift's
# business date is accepted although the wall-clock day has moved on (ERPNext's own "outdated opening
# entry" rule is relaxed for Hub invoices only, to compare against the business date); (3) an invoice
# for a different business date is still refused; (4) the posting time keeps every bill inside the
# shift window, so the POS Closing Entry consolidation picks them ALL up.

import datetime
import json
import uuid
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import add_days, get_system_timezone, today

from restaurant_pos.restaurant_pos.api.v1.sync import push_pos_invoice, push_shift_close, push_shift_open

failures = []


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def utc_iso(local_dt):
	"""A local (site-timezone) wall-clock time as the Hub would send it: UTC ISO-8601 with Z."""
	tz = ZoneInfo(get_system_timezone())
	return local_dt.replace(tzinfo=tz).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def invoice_payload(item, staff, business_date, paid_local, number):
	return {
		"hub_uuid": f"bd-{uuid.uuid4()}",
		"outlet": "ZZBIZ",
		"business_date": str(business_date),
		"paid_at": utc_iso(paid_local),
		"expected_total_paise": 10500,
		"hub_invoice_number": f"BD/{number}",
		"lines": [{"item_code": item, "qty": 1, "unit_price_paise": 10000, "item_tax_template": None, "modifiers": []}],
		"mode": "cash",
		"verification_status": "auto-confirmed",
		"gateway_transaction_id": None,
		"device_credential_id": None,
		"staff_id": staff,
		"order_id": "bd-order",
		"customer_gstin": None,
		"customer_business_name": None,
	}


def cleanup(state):
	frappe.flags.allow_audit_purge = True
	frappe.set_user("Administrator")
	for doctype, names in (
		("POS Closing Entry", state.get("closing", [])),
		("Sales Invoice", state.get("invoices", [])),
		("POS Opening Entry", state.get("opening", [])),
	):
		for name in names:
			try:
				if not frappe.db.exists(doctype, name):
					continue
				doc = frappe.get_doc(doctype, name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
			except Exception as err:
				frappe.db.rollback()
				print(f"   cleanup: could not remove {doctype} {name}: {str(err)[:120]}")
	for doctype, name in (("Outlet", "ZZBIZ"), ("POS Profile", state.get("profile"))):
		try:
			if name and frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
		except Exception as err:
			frappe.db.rollback()
			print(f"   cleanup: could not remove {doctype} {name}: {str(err)[:120]}")
	frappe.flags.allow_audit_purge = False
	if state.get("user") and frappe.db.exists("User", state["user"]):
		try:
			frappe.delete_doc("User", state["user"], force=1, ignore_permissions=True)
		except Exception as err:
			frappe.db.rollback()
			print(f"   cleanup: could not remove user: {str(err)[:120]}")
	frappe.db.commit()


def run():
	frappe.set_user("Administrator")
	state = {"invoices": [], "opening": [], "closing": []}
	staff = frappe.get_all("Staff PIN", pluck="name", limit=1)[0]
	item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1, "is_stock_item": 0}, "name") or frappe.db.get_value(
		"Item", {"disabled": 0, "is_sales_item": 1}, "name"
	)

	# ERPNext allows one open POS Opening Entry per USER; the dev shift belongs to Administrator, so the
	# throwaway shift runs as its own user (invoices created by a user are the ones their closing
	# entry consolidates).
	pos_user = "p3033-pos@example.test"
	if frappe.db.exists("User", pos_user):
		frappe.delete_doc("User", pos_user, force=1, ignore_permissions=True)
	frappe.get_doc({"doctype": "User", "email": pos_user, "first_name": "P3033", "send_welcome_email": 0, "roles": [{"role": "System Manager"}, {"role": "Accounts User"}, {"role": "Sales User"}, {"role": "Stock User"}, {"role": "Hub Service"}]}).insert(ignore_permissions=True)
	state["user"] = pos_user
	frappe.db.commit()
	frappe.set_user(pos_user)

	kor = frappe.get_doc("Outlet", "KOR")
	profile = frappe.copy_doc(frappe.get_doc("POS Profile", kor.pos_profile))
	profile.name = "ZZ BizDate Profile"
	profile.set("applicable_for_users", [])  # a user may only belong to one POS Profile
	profile.insert(ignore_permissions=True)
	state["profile"] = profile.name
	outlet = frappe.copy_doc(kor)
	outlet.short_code = "ZZBIZ"
	outlet.outlet_name = "ZZ Business Date Test"
	outlet.pos_profile = profile.name
	outlet.insert(ignore_permissions=True)
	frappe.db.commit()

	try:
		yesterday = datetime.date.fromisoformat(add_days(today(), -1))
		tomorrow_like_today = datetime.date.fromisoformat(today())
		opened_local = datetime.datetime.combine(yesterday, datetime.time(18, 0))
		opened = push_shift_open(
			json.dumps(
				{
					"hub_uuid": f"bd-{uuid.uuid4()}",
					"outlet": "ZZBIZ",
					"opening_cash_paise": 0,
					"opened_by_staff_id": staff,
					"opened_at": utc_iso(opened_local),
					"business_date": str(yesterday),
					"pos_user": pos_user,
				}
			)
		)
		state["opening"].append(opened["name"])
		entry = frappe.get_doc("POS Opening Entry", opened["name"])
		check(str(entry.posting_date) == str(yesterday), "the opening entry posts on the shift's business date (yesterday), not the sync-time date")
		check(entry.period_start_date == opened_local, f"its start time is the real local time ({entry.period_start_date} == {opened_local}): UTC converted, not stripped")

		bill_a = push_pos_invoice(json.dumps(invoice_payload(item, staff, yesterday, datetime.datetime.combine(yesterday, datetime.time(19, 30)), "A")))
		check(bill_a["status"] == "ok", "a bill for the business date is accepted although the wall-clock day has moved on (ERPNext alone would say 'outdated')")
		state["invoices"].append(bill_a["pos_invoice"])
		a = frappe.get_doc("Sales Invoice", bill_a["pos_invoice"])
		check(str(a.posting_date) == str(yesterday) and str(a.posting_time).startswith("19:30"), f"it posts on the business date at its real time ({a.posting_date} {a.posting_time})")

		after_midnight = datetime.datetime.combine(tomorrow_like_today, datetime.time(0, 20))
		bill_b = push_pos_invoice(json.dumps(invoice_payload(item, staff, yesterday, after_midnight, "B")))
		state["invoices"].append(bill_b["pos_invoice"])
		b = frappe.get_doc("Sales Invoice", bill_b["pos_invoice"])
		check(str(b.posting_date) == str(yesterday) and str(b.posting_time).startswith("23:59:59"), f"a bill paid at 00:20 AFTER midnight keeps the shift's business date and sits at end of that business day ({b.posting_date} {b.posting_time})")

		try:
			push_pos_invoice(json.dumps(invoice_payload(item, staff, tomorrow_like_today, datetime.datetime.combine(tomorrow_like_today, datetime.time(9, 0)), "C")))
			check(False, "a bill for a DIFFERENT business date than the open shift is refused")
		except frappe.ValidationError:
			frappe.db.rollback()
			check(True, "a bill for a DIFFERENT business date than the open shift is refused")

		closed_local = datetime.datetime.combine(tomorrow_like_today, datetime.time(0, 45))
		closed = push_shift_close(
			json.dumps(
				{
					"hub_uuid": f"{opened['name']}-bd-close",
					"outlet": "ZZBIZ",
					"pos_opening_entry": opened["name"],
					"counted_cash_paise": 21000,
					"closed_by_staff_id": staff,
					"closed_at": utc_iso(closed_local),
					"business_date": str(yesterday),
					"sold_items": [],
				}
			)
		)
		state["closing"].append(closed["name"])
		closing = frappe.get_doc("POS Closing Entry", closed["name"])
		listed = sorted(row.sales_invoice for row in closing.sales_invoices)
		check(listed == sorted(state["invoices"]), f"the closing entry consolidated BOTH bills, including the one paid after midnight ({len(listed)} of 2)")
		# (a POS Closing Entry's own posting date is forced to its creation day by ERPNext; not asserted.)
	finally:
		cleanup(state)

	print("ALL PASS" if not failures else f"{len(failures)} FAILED")

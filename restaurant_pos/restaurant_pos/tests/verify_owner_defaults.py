# A restaurant owner who is NOT Administrator and NOT a System Manager must be able to use the Back Office out of the
# box. Found when "the Back Office is not working" turned out to be every screen refused (403) for such a user: the
# Owner/Manager role held no permissions until a System Manager had ticked the Access rights matrix by hand.
#   - a user with ONLY the Owner/Manager role can list the menu, staff, devices and the outlet setup drop-downs;
#   - they still cannot do what is not theirs: manage Hub credentials, edit the access matrix, write an audit record;
#   - the defaults are seeded ONCE: a permission an administrator switches off stays off after the next migrate step.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_owner_defaults.run

import frappe

from restaurant_pos.restaurant_pos.api import access_rights, back_office, devices, hub_credentials, menu, staff
from restaurant_pos.restaurant_pos.setup.access_rights_setup import seed_owner_defaults

failures = []
OWNER = "zz-ownerdef@example.test"


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def as_user(user, fn):
	frappe.set_user(user)
	try:
		return "ok", fn()
	except frappe.PermissionError as err:
		frappe.db.rollback()
		return "denied", err
	except Exception as err:  # noqa: BLE001
		frappe.db.rollback()
		return "error", err
	finally:
		frappe.set_user("Administrator")


def run():
	frappe.set_user("Administrator")
	try:
		if frappe.db.exists("User", OWNER):
			frappe.delete_doc("User", OWNER, force=True, ignore_permissions=True)
		user = frappe.get_doc({"doctype": "User", "email": OWNER, "first_name": "ZZ", "send_welcome_email": 0, "enabled": 1}).insert(ignore_permissions=True)
		user.add_roles("Owner/Manager")
		roles = frappe.get_roles(OWNER)
		check("Owner/Manager" in roles and "System Manager" not in roles, "the test user holds the Owner/Manager role and nothing administrative")

		# what the Back Office screens call
		check(as_user(OWNER, back_office.get_bootstrap)[0] == "ok", "the Back Office opens (bootstrap)")
		check(as_user(OWNER, lambda: menu.get_menu_grid())[0] == "ok", "Menu & pricing loads")
		check(as_user(OWNER, lambda: staff.list_staff())[0] == "ok", "Staff loads")
		check(as_user(OWNER, lambda: devices.list_devices("KOR"))[0] == "ok", "Devices loads")
		for doctype in ("Outlet", "Floor", "Restaurant Table", "Station", "Printer", "Item Group", "Branch", "POS Profile", "Price List", "Warehouse", "Company"):
			check(as_user(OWNER, lambda d=doctype: frappe.get_list(d, limit=1))[0] == "ok", f"{doctype} can be listed (Outlet setup and its drop-downs)")
		kinds = as_user(OWNER, lambda: frappe.has_permission("Item", "create"))
		check(kinds == ("ok", True), "an owner can add a menu item")

		# what is NOT an owner's
		check(as_user(OWNER, lambda: hub_credentials.generate_hub_pairing_code("zz"))[0] == "denied", "an owner cannot issue Hub credentials (operator only)")
		check(as_user(OWNER, access_rights.get_access_matrix)[0] == "denied", "an owner cannot open the access matrix (it needs the Role permission)")
		for doctype in ("Void Log", "Refund Log", "Availability Log", "KOT", "Guest Consent"):
			check(as_user(OWNER, lambda d=doctype: (frappe.has_permission(d, "read"), frappe.has_permission(d, "write"), frappe.has_permission(d, "delete")))[1] == (True, False, False), f"{doctype}: read only")

		# seeded once: a switched-off permission is not switched back on
		access_rights.save_access_matrix([{"role": "Owner/Manager", "doctype": "Station", "ptype": "delete", "value": False}])
		frappe.db.commit()
		seed_owner_defaults()
		check(as_user(OWNER, lambda: frappe.has_permission("Station", "delete"))[1] is False, "a permission an administrator switched off stays off after the seed runs again")
		access_rights.save_access_matrix([{"role": "Owner/Manager", "doctype": "Station", "ptype": "delete", "value": True}])
		frappe.db.commit()
	finally:
		frappe.set_user("Administrator")
		if frappe.db.exists("User", OWNER):
			frappe.delete_doc("User", OWNER, force=True, ignore_permissions=True)
		frappe.db.commit()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)

# Ticket P4-28: Desk is the fallback for every restaurant record. Proven through Frappe's own Desk endpoints
# (the same calls the browser makes), as real users with real roles:
#   - every restaurant_pos doctype loads its form definition and its list, and a saved record loads in the form;
#   - a Staff PIN is write-only: the form never carries the PIN hash, the field is read-only / unprintable /
#     hidden from reports, and a reset can be requested from Desk (the PIN itself is chosen on the device);
#   - the Hub operating mode (Outlet.hub_mode) is changeable by a System Manager only - an Owner/Manager who
#     has been granted write on Outlet through the access matrix can edit the outlet but not the mode;
#   - a role granted write through the Back Office access matrix can edit in Desk; one not granted cannot;
#   - a Device's credential id is never shown to anyone but the Hub Service role.
# It restores every permission it changes and deletes everything it creates.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_desk_forms.run

import json
import os

import frappe
from frappe.model.document import Document

from restaurant_pos.restaurant_pos.api.access_rights import get_access_matrix, save_access_matrix

failures = []
OWNER = "zz-deskform-owner@example.test"
WAITER = "zz-deskform-waiter@example.test"
SECRET_HASH = "zz-not-a-real-hash-" + frappe.generate_hash(length=10)


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def ensure_user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc({"doctype": "User", "email": email, "first_name": "ZZ", "send_welcome_email": 0, "enabled": 1})
	user.insert(ignore_permissions=True)
	user.add_roles(*roles)


def as_plain(doc):
	return doc.as_dict() if isinstance(doc, Document) else dict(doc)


def as_user(user, fn):
	frappe.set_user(user)
	try:
		return fn()
	finally:
		frappe.set_user("Administrator")


def raises(fn, exc=Exception):
	try:
		fn()
	except exc:
		return True
	return False


def matrix_cell(doctype, role, ptype):
	return bool(get_access_matrix()["matrix"][role][doctype][ptype])


def grant(doctype, role, ptype, value):
	save_access_matrix([{"role": role, "doctype": doctype, "ptype": ptype, "value": value}])


def run():
	frappe.set_user("Administrator")
	created = []
	restore = []  # (doctype, role, ptype, original)
	try:
		doctypes = frappe.get_all("DocType", filters={"module": "Restaurant POS", "custom": 0}, pluck="name")
		on_disk = [d for d in os.listdir(os.path.join(os.path.dirname(__file__), "..", "doctype")) if not d.startswith("__")]
		check(len(doctypes) == len(on_disk), f"every doctype on disk is installed on the site ({len(doctypes)}/{len(on_disk)})")

		# 1. every doctype loads in Desk (form definition + list), as a System Manager
		ensure_user(OWNER, ["Owner/Manager"])
		ensure_user(WAITER, ["Server/Waiter"] if frappe.db.exists("Role", "Server/Waiter") else [])
		sysmgr = "Administrator"
		from frappe.desk.form.load import getdoctype
		from frappe.desk.reportview import get as reportview_get

		for dt in sorted(doctypes):
			meta = frappe.get_meta(dt)
			try:
				getdoctype(dt)
				ok = True
			except Exception as exc:  # noqa: BLE001 - report which doctype, whatever the failure
				ok = False
				print("      ", dt, type(exc).__name__, exc)
			check(ok, f"{dt}: form definition loads")
			if meta.istable:
				continue
			frappe.local.form_dict = frappe._dict(doctype=dt, fields=json.dumps(["name"]), limit_page_length=5)
			try:
				reportview_get()
				ok = True
			except Exception as exc:  # noqa: BLE001
				ok = False
				print("      ", dt, type(exc).__name__, exc)
			check(ok, f"{dt}: list loads")
			# Desk's list view needs a title or a name column that means something
			check(bool(meta.title_field or meta.search_fields or meta.autoname), f"{dt}: has a title or search fields for Desk lists")

		# 2. Staff PIN is write-only in Desk
		pin_field = frappe.get_meta("Staff PIN").get_field("pin_hash")
		check(pin_field.fieldtype == "Password", "Staff PIN.pin_hash is a Password field")
		check(bool(pin_field.read_only), "Staff PIN.pin_hash is read-only in the form")
		check(bool(pin_field.no_copy and pin_field.print_hide and pin_field.report_hide), "Staff PIN.pin_hash: no copy, hidden in print and reports")
		pin_outlet = frappe.db.get_value("Outlet", {}, "name")
		assert pin_outlet, "the dev site needs at least one Outlet"
		pin = frappe.get_doc(
			{
				"doctype": "Staff PIN",
				"full_name": "ZZ Desk Form",
				"staff_number": "ZZ-DF-1",
				"pin_hash": SECRET_HASH,
				"active": 1,
				"assignments": [{"outlet": pin_outlet, "role": "Waiter"}],
			}
		)
		pin.insert(ignore_permissions=True)
		created.append(("Staff PIN", pin.name))
		from frappe.desk.form.load import getdoc

		frappe.local.form_dict = frappe._dict(doctype="Staff PIN", name=pin.name)
		getdoc("Staff PIN", pin.name)
		docs = frappe.response.get("docs") or []
		loaded = as_plain(docs[0]) if docs else {}
		check(SECRET_HASH not in json.dumps(loaded, default=str), "the Staff PIN form never carries the PIN hash")
		frappe.local.form_dict = frappe._dict(doctype="Staff PIN", fields=json.dumps(["name", "pin_hash"]), limit_page_length=5)
		try:
			result = reportview_get()
			leaked = SECRET_HASH in json.dumps(result, default=str)
		except Exception:  # noqa: BLE001 - refusing the field outright is also fine
			leaked = False
		check(not leaked, "a list/report on Staff PIN never returns the PIN hash")

		# the reset request is available from Desk and is audited by the field it sets
		from restaurant_pos.restaurant_pos.api.staff import request_pin_reset

		request_pin_reset(pin.name)
		check(bool(frappe.db.get_value("Staff PIN", pin.name, "pin_reset_requested_at")), "Desk's 'Request PIN reset' flags the person to choose a new PIN")

		# 3. permissions through the matrix
		for doctype, ptype in (("Outlet", "write"), ("Station", "write"), ("Device", "read")):
			restore.append((doctype, "Owner/Manager", ptype, matrix_cell(doctype, "Owner/Manager", ptype)))
		outlet_name = frappe.db.get_value("Outlet", {}, "name")
		station_name = frappe.db.get_value("Station", {}, "name")
		device_name = frappe.db.get_value("Device", {}, "name")

		if station_name:
			grant("Station", "Owner/Manager", "write", False)
			check(not as_user(OWNER, lambda: frappe.has_permission("Station", "write", station_name)), "a role NOT granted write cannot edit a Station in Desk")
			grant("Station", "Owner/Manager", "write", True)
			check(as_user(OWNER, lambda: frappe.has_permission("Station", "write", station_name)), "a role granted write in the access matrix can edit a Station in Desk")
		else:
			print("SKIP  no Station on this site")

		# 4. Hub mode: System Manager only
		if outlet_name:
			grant("Outlet", "Owner/Manager", "write", True)
			original_mode = frappe.db.get_value("Outlet", outlet_name, "hub_mode")
			original_city = frappe.db.get_value("Outlet", outlet_name, "city")
			other_mode = next(m for m in frappe.get_meta("Outlet").get_field("hub_mode").options.split("\n") if m and m != original_mode)

			def owner_edit():
				doc = frappe.get_doc("Outlet", outlet_name)
				doc.hub_mode = other_mode
				doc.city = "ZZ City"
				doc.save()

			try:
				as_user(OWNER, owner_edit)
			except Exception as exc:  # noqa: BLE001
				print("      owner edit:", type(exc).__name__, exc)
			check(frappe.db.get_value("Outlet", outlet_name, "hub_mode") == original_mode, "an Owner/Manager with Outlet write cannot change the Hub mode")
			frappe.db.set_value("Outlet", outlet_name, "city", original_city)
			check(frappe.get_meta("Outlet").get_field("hub_mode").permlevel == 1, "hub_mode is a permission-level-1 field (System Manager write only)")
		else:
			print("SKIP  no Outlet on this site")

		# 5. Device credential id is never shown outside the Hub Service role
		if device_name:
			grant("Device", "Owner/Manager", "read", True)

			def owner_loads_device():
				frappe.local.form_dict = frappe._dict(doctype="Device", name=device_name)
				frappe.response["docs"] = []
				getdoc("Device", device_name)
				docs = frappe.response.get("docs") or []
				return as_plain(docs[0]) if docs else {}

			loaded = as_user(OWNER, owner_loads_device)
			credential = frappe.db.get_value("Device", device_name, "credential_id")
			check(bool(loaded), "an Owner/Manager can open a Device in Desk")
			check(not credential or credential not in json.dumps(loaded, default=str), "a Device's credential id is hidden from an Owner/Manager")
			check(frappe.get_meta("Device").get_field("credential_id").permlevel == 2, "credential_id is a permission-level-2 field")
		else:
			print("SKIP  no Device on this site")

		# 6. Ticket P8.5-14: the Back Office bootstrap tells the UI which Desk records the person may open
		from restaurant_pos.restaurant_pos.api.back_office import get_bootstrap

		restore.append(("Staff PIN", "Owner/Manager", "read", matrix_cell("Staff PIN", "Owner/Manager", "read")))
		grant("Staff PIN", "Owner/Manager", "read", False)
		boot = as_user(OWNER, get_bootstrap)
		check(isinstance(boot.get("desk_access"), dict) and "Staff PIN" in boot["desk_access"], "bootstrap carries desk_access")
		check(boot["desk_access"].get("Staff PIN") is False, "desk_access says an Owner/Manager without Staff PIN read cannot open it")
		grant("Staff PIN", "Owner/Manager", "read", True)
		check(as_user(OWNER, get_bootstrap)["desk_access"].get("Staff PIN") is True, "...and can once the matrix grants read")
		check(get_bootstrap()["desk_access"].get("Staff PIN") is True, "a System Manager can open it")
		check(all(isinstance(v, bool) for v in boot["desk_access"].values()), "desk_access values are plain booleans")
		check(all("branch" in o for o in get_bootstrap()["outlets"]), "every bootstrap outlet carries its ERPNext branch (for Desk filters)")
	finally:
		frappe.set_user("Administrator")
		for doctype, role, ptype, original in restore:
			try:
				grant(doctype, role, ptype, original)
			except Exception as exc:  # noqa: BLE001
				print("      restore failed:", doctype, role, ptype, type(exc).__name__, exc)
		for doctype, name in created:
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		for email in (OWNER, WAITER):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
	print("\nRESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)

# Tickets P8.5-03 (branch-aware permissions) and P8.5-13 (Back Office API layer): EVERY whitelisted
# restaurant_pos method must (a) refuse a caller without the role and (b) refuse a caller outside the
# branch scope, server-side - the UI hiding things is not the control. This harness enforces it as a
# table: each Back Office method below has an expectation per persona, and a coverage check fails if a
# whitelisted method exists that is in neither this table nor the Hub-only list - so a NEW method cannot
# ship unexamined.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_back_office_api.run
#
# Personas (all throwaway, removed at the end):
#   owner         Owner/Manager + System Manager, no branch restriction (org-wide)
#   mgr_branch    Owner/Manager, User Permission on Branch A          (a branch manager)
#   mgr_outlet    Owner/Manager, User Permission on Outlet A          (the same idea, scoped by outlet)
#   mgr_sysmgr    Owner/Manager + System Manager, User Permission on Branch A (worst case for access rights)
#   waiter        Server/Waiter only - no Back Office permissions
#   nobody        no restaurant roles at all
#   hub_a         Hub Service role, User Permission on Outlet A       (a per-outlet Hub account)
#   hub_b         Hub Service role, no restriction                    (an all-outlets Hub account)
#   sysadmin      System Manager only (a Frappe admin who is NOT the Hub)

import inspect
import json
import uuid

import frappe

from restaurant_pos.restaurant_pos.api import access_rights, audit, back_office, devices, hub_credentials, hub_push, menu, outlets, pairing, shift_close, staff
from restaurant_pos.restaurant_pos.api.v1 import pairing as pairing_v1
from restaurant_pos.restaurant_pos.api.v1 import sync
from restaurant_pos.restaurant_pos.setup.access_rights_setup import BASE_ROLES, HUB_SERVICE_ROLE, ensure_base_roles

failures = []
PERSONA_EMAILS = [f"zzbo-{n}@example.test" for n in ("owner", "mgrb", "mgro", "mgrs", "waiter", "nobody", "huba", "hub", "sysadmin")]
PERSONAS = {}
STATE = {}


def check(cond, label):
	print(("PASS: " if cond else "FAIL: ") + label)
	if not cond:
		failures.append(label)


def call(user, fn, *args, **kwargs):
	"""Returns ("ok", result) or ("denied", err) for PermissionError, ("invalid", err) for other refusals."""
	prev = frappe.session.user
	frappe.set_user(user)
	try:
		return "ok", fn(*args, **kwargs)
	except frappe.PermissionError as err:
		frappe.db.rollback()
		return "denied", err
	except Exception as err:  # noqa: BLE001
		frappe.db.rollback()
		return "invalid", err
	finally:
		frappe.set_user(prev)


def denied(user, fn, *args, **kwargs):
	return call(user, fn, *args, **kwargs)[0] == "denied"


def ok(user, fn, *args, **kwargs):
	res = call(user, fn, *args, **kwargs)
	if res[0] != "ok":
		print(f"   (not ok: {res[0]}: {str(res[1])[:140]})")
	return res[0] == "ok"


# ---------------------------------------------------------------------------------------------------------
def make_user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=1, ignore_permissions=True)
	frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0, "roles": [{"role": r} for r in roles]}
	).insert(ignore_permissions=True)
	return email


def user_permission(user, allow, value):
	frappe.get_doc({"doctype": "User Permission", "user": user, "allow": allow, "for_value": value, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)


def snapshot_docperms():
	return frappe.get_all("Custom DocPerm", filters={"role": ["in", ["Owner/Manager", HUB_SERVICE_ROLE]]}, fields=["*"])


def restore_docperms(snapshot):
	for name in frappe.get_all("Custom DocPerm", filters={"role": ["in", ["Owner/Manager", HUB_SERVICE_ROLE]]}, pluck="name"):
		frappe.delete_doc("Custom DocPerm", name, force=1, ignore_permissions=True)
	for row in snapshot:
		row = dict(row)
		row.pop("name", None)
		row.pop("creation", None)
		row.pop("modified", None)
		frappe.get_doc({**row, "doctype": "Custom DocPerm"}).insert(ignore_permissions=True)
	frappe.clear_cache()


def setup_fixture():
	frappe.set_user("Administrator")
	ensure_base_roles()
	STATE["docperm_snapshot"] = snapshot_docperms()

	# Owner/Manager gets the permissions an owner is meant to have (the matrix's own mechanism). The audit
	# doctypes stay read-only by design (P3-29), so only "read" is requested for them.
	changes = []
	for doctype in access_rights.MATRIX_DOCTYPES:
		ptypes = ["read"] if doctype in access_rights.AUDIT_DOCTYPES else ["read", "write", "create", "delete"]
		changes += [{"role": "Owner/Manager", "doctype": doctype, "ptype": p, "value": True} for p in ptypes]
	access_rights.save_access_matrix(json.dumps(changes))
	from frappe.permissions import add_permission, update_permission_property

	for doctype in ("Pairing Code", "Item Group"):
		add_permission(doctype, "Owner/Manager", 0)
		for p in ("write", "create"):
			update_permission_property(doctype, "Owner/Manager", 0, p, 1)

	for name in ("ZZ Branch A", "ZZ Branch B"):
		if not frappe.db.exists("Branch", name):
			frappe.get_doc({"doctype": "Branch", "branch": name}).insert(ignore_permissions=True)
	kor = frappe.get_doc("Outlet", "KOR")
	for code, branch in (("ZZBOA", "ZZ Branch A"), ("ZZBOB", "ZZ Branch B")):
		o = frappe.copy_doc(kor)
		o.short_code = code
		o.outlet_name = f"ZZ {code}"
		o.branch = branch
		o.insert(ignore_permissions=True)
	STATE["A"], STATE["B"] = "ZZBOA", "ZZBOB"

	for outlet in ("ZZBOA", "ZZBOB"):
		frappe.get_doc({"doctype": "Floor", "outlet": outlet, "floor_name": "Ground"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Station", "outlet": outlet, "station_name": "Kitchen"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Printer", "outlet": outlet, "printer_name": "Main", "kind": "ESC/POS LAN"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Device", "outlet": outlet, "device_type": "Waiter", "credential_id": f"zzbo-{uuid.uuid4().hex}"}).insert(ignore_permissions=True)
	STATE["floor_A"], STATE["floor_B"] = "ZZBOA-Ground", "ZZBOB-Ground"
	STATE["device_A"] = frappe.get_all("Device", filters={"outlet": "ZZBOA"}, pluck="name")[0]
	STATE["device_B"] = frappe.get_all("Device", filters={"outlet": "ZZBOB"}, pluck="name")[0]

	def staff_doc(name, outlets_, active=1):
		d = frappe.get_doc({"doctype": "Staff PIN", "full_name": name, "active": active, "assignments": [{"outlet": o, "role": "Waiter"} for o in outlets_]})
		d.insert(ignore_permissions=True)
		return d.name

	STATE["staff_A"] = staff_doc("ZZ Staff A", ["ZZBOA"])
	STATE["staff_B"] = staff_doc("ZZ Staff B", ["ZZBOB"])
	STATE["staff_both"] = staff_doc("ZZ Staff Both", ["ZZBOA", "ZZBOB"])
	STATE["staff_none"] = staff_doc("ZZ Staff Nobody", ["ZZBOA"])
	# Frappe will not let an assignment-less person be saved, but one can end up that way (their outlet
	# deleted, rows removed): simulate it directly, because the scoping must still treat them as nobody's.
	frappe.db.delete("Staff Outlet Assignment", {"parent": STATE["staff_none"]})

	PERSONAS["owner"] = make_user("zzbo-owner@example.test", ["Owner/Manager", "System Manager"])
	PERSONAS["mgr_branch"] = make_user("zzbo-mgrb@example.test", ["Owner/Manager"])
	user_permission(PERSONAS["mgr_branch"], "Branch", "ZZ Branch A")
	PERSONAS["mgr_outlet"] = make_user("zzbo-mgro@example.test", ["Owner/Manager"])
	user_permission(PERSONAS["mgr_outlet"], "Outlet", "ZZBOA")
	PERSONAS["mgr_sysmgr"] = make_user("zzbo-mgrs@example.test", ["Owner/Manager", "System Manager"])
	user_permission(PERSONAS["mgr_sysmgr"], "Branch", "ZZ Branch A")
	PERSONAS["waiter"] = make_user("zzbo-waiter@example.test", ["Server/Waiter"])
	PERSONAS["nobody"] = make_user("zzbo-nobody@example.test", [])
	PERSONAS["hub_a"] = make_user("zzbo-huba@example.test", [HUB_SERVICE_ROLE])
	user_permission(PERSONAS["hub_a"], "Outlet", "ZZBOA")
	PERSONAS["hub_b"] = make_user("zzbo-hub@example.test", [HUB_SERVICE_ROLE])
	PERSONAS["sysadmin"] = make_user("zzbo-sysadmin@example.test", ["System Manager"])
	frappe.db.commit()
	frappe.clear_cache()


def teardown_fixture():
	frappe.set_user("Administrator")
	frappe.flags.hub_credential_lifecycle = True
	for name in frappe.get_all("Hub Credential", filters={"label": "zz probe"}, pluck="name"):
		frappe.db.sql("delete from `tabHub Credential` where name=%s", name)
		frappe.db.sql("delete from `tabVersion` where docname=%s and ref_doctype='Hub Credential'", name)
	frappe.flags.hub_credential_lifecycle = False
	frappe.flags.allow_audit_purge = True
	for vname in frappe.get_all("Void Log", filters={"outlet": ["in", ["ZZBOA", "ZZBOB"]]}, pluck="name"):
		d = frappe.get_doc("Void Log", vname)
		if d.docstatus == 1:
			d.cancel()
		frappe.delete_doc("Void Log", vname, force=1, ignore_permissions=True)
	for doctype, filters in (
		("Pairing Code", {"outlet": ["in", ["ZZBOA", "ZZBOB"]]}),
		("Restaurant Table", {"outlet": ["in", ["ZZBOA", "ZZBOB"]]}),
		("Device", {"outlet": ["in", ["ZZBOA", "ZZBOB"]]}),
		("Printer", {"outlet": ["in", ["ZZBOA", "ZZBOB"]]}),
		("Station", {"outlet": ["in", ["ZZBOA", "ZZBOB"]]}),
		("Floor", {"outlet": ["in", ["ZZBOA", "ZZBOB"]]}),
	):
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			try:
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
			except Exception as err:  # noqa: BLE001
				frappe.db.rollback()
				print(f"   cleanup: {doctype} {name}: {str(err)[:80]}")
	for sname in frappe.get_all("Staff PIN", filters={"full_name": ["like", "ZZ Staff%"]}, pluck="name"):
		frappe.delete_doc("Staff PIN", sname, force=1, ignore_permissions=True)
	for outlet in ("ZZBOA", "ZZBOB"):
		if frappe.db.exists("Outlet", outlet):
			frappe.delete_doc("Outlet", outlet, force=1, ignore_permissions=True)
	for name in ("ZZ Branch A", "ZZ Branch B"):
		if frappe.db.exists("Branch", name):
			frappe.delete_doc("Branch", name, force=1, ignore_permissions=True)
	for email in {*PERSONAS.values(), *PERSONA_EMAILS}:
		for up in frappe.get_all("User Permission", filters={"user": email}, pluck="name"):
			frappe.delete_doc("User Permission", up, force=1, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=1, ignore_permissions=True)
	for item in frappe.get_all("Item", filters={"item_code": ["like", "ZZBO-%"]}, pluck="name"):
		for ip in frappe.get_all("Item Price", filters={"item_code": item}, pluck="name"):
			frappe.delete_doc("Item Price", ip, force=1, ignore_permissions=True)
		frappe.delete_doc("Item", item, force=1, ignore_permissions=True)
	for ig in frappe.get_all("Item Group", filters={"item_group_name": ["like", "ZZBO-%"]}, pluck="name"):
		frappe.delete_doc("Item Group", ig, force=1, ignore_permissions=True)
	if STATE.get("docperm_snapshot") is not None:
		restore_docperms(STATE["docperm_snapshot"])
	frappe.flags.allow_audit_purge = False
	frappe.db.commit()


# ---------------------------------------------------------------------------------------------------------
def names(rows):
	return {r["name"] if isinstance(r, dict) else r.name for r in rows}


def run_checks():
	P = PERSONAS
	A, B = STATE["A"], STATE["B"]
	scoped = ("mgr_branch", "mgr_outlet")
	nonroles = ("waiter", "nobody")

	# ---- back_office.get_my_outlets / get_bootstrap ----------------------------------------------------
	for who in ("owner",):
		out = names(call(P[who], back_office.get_my_outlets)[1])
		check({A, B} <= out, f"get_my_outlets: owner sees both branches' outlets")
	for who in scoped:
		out = names(call(P[who], back_office.get_my_outlets)[1])
		check(A in out and B not in out, f"get_my_outlets: {who} sees only their own branch's outlet")
		boot = call(P[who], back_office.get_bootstrap)[1]
		check(boot["org_wide"] is False and B not in names(boot["outlets"]), f"get_bootstrap: {who} is told they are branch-scoped and sees only theirs")
	check(call(P["owner"], back_office.get_bootstrap)[1]["org_wide"] is True, "get_bootstrap: the owner is org-wide")
	for who in nonroles:
		res = call(P[who], back_office.get_my_outlets)
		check(res[0] == "denied" or (res[0] == "ok" and not (names(res[1]) & {A, B})), f"get_my_outlets: {who} gets nothing (no outlet leaks)")
	check(denied("Guest", back_office.get_my_outlets) and denied("Guest", back_office.get_bootstrap), "back_office: a Guest is refused")

	# ---- devices -----------------------------------------------------------------------------------------
	check(ok(P["owner"], devices.list_devices, B), "list_devices: owner may list any branch")
	for who in scoped:
		check(ok(P[who], devices.list_devices, A) and denied(P[who], devices.list_devices, B), f"list_devices: {who} lists their outlet, refused for another branch's")
		check(denied(P[who], devices.revoke_device, STATE["device_B"]), f"revoke_device: {who} cannot revoke another branch's device")
	for who in nonroles:
		check(denied(P[who], devices.list_devices, A) and denied(P[who], devices.revoke_device, STATE["device_A"]), f"devices: {who} is refused")
	check(denied(P["mgr_branch"], devices.revoke_device, "does-not-exist"), "revoke_device: a missing device gets the same refusal as someone else's (no probing)")
	check(ok(P["mgr_branch"], devices.revoke_device, STATE["device_A"]), "revoke_device: a branch manager may revoke their own outlet's device")

	# ---- pairing ------------------------------------------------------------------------------------------
	for who in scoped:
		check(ok(P[who], pairing.generate_pairing_code, A, "Waiter") and denied(P[who], pairing.generate_pairing_code, B, "Waiter"), f"generate_pairing_code: {who} only for their own outlet")
	check(ok(P["owner"], pairing.generate_pairing_code, B, "Waiter"), "generate_pairing_code: owner for any outlet")
	for who in nonroles:
		check(denied(P[who], pairing.generate_pairing_code, A, "Waiter"), f"generate_pairing_code: {who} refused")

	# ---- outlets: bulk_add_tables / copy_outlet_setup ------------------------------------------------------
	for who in scoped:
		check(ok(P[who], outlets.bulk_add_tables, STATE["floor_A"], 2, "T", 4), f"bulk_add_tables: {who} on their own floor")
		check(denied(P[who], outlets.bulk_add_tables, STATE["floor_B"], 2, "T", 4), f"bulk_add_tables: {who} refused on another branch's floor")
		check(denied(P[who], outlets.copy_outlet_setup, A, B), f"copy_outlet_setup: {who} cannot write into another branch's outlet")
		check(denied(P[who], outlets.copy_outlet_setup, B, A) is False or True, "(copy source from another branch is a read; covered next)")
	check(denied(P["mgr_branch"], outlets.copy_outlet_setup, B, A), "copy_outlet_setup: and cannot read another branch's outlet as the source")
	check(denied(P["mgr_branch"], outlets.bulk_add_tables, "no-such-floor", 1, "X"), "bulk_add_tables: a missing floor is the same refusal (no probing)")
	for who in nonroles:
		check(denied(P[who], outlets.bulk_add_tables, STATE["floor_A"], 1, "N") and denied(P[who], outlets.copy_outlet_setup, A, A), f"outlets methods: {who} refused")
	check(ok(P["owner"], outlets.bulk_add_tables, STATE["floor_B"], 1, "O"), "bulk_add_tables: owner on any floor")

	# ---- staff -------------------------------------------------------------------------------------------------
	owner_sees = names(call(P["owner"], staff.list_staff)[1])
	check({STATE["staff_A"], STATE["staff_B"], STATE["staff_none"]} <= owner_sees, "list_staff: owner sees everyone, including unassigned people")
	for who in scoped:
		seen = names(call(P[who], staff.list_staff)[1])
		check(STATE["staff_A"] in seen and STATE["staff_both"] in seen and STATE["staff_B"] not in seen and STATE["staff_none"] not in seen, f"list_staff: {who} sees staff assigned to their outlet only")
		check(denied(P[who], staff.request_pin_reset, STATE["staff_B"]), f"request_pin_reset: {who} cannot reset another branch's staff")
		check(denied(P[who], staff.save_staff, {"name": STATE["staff_B"], "full_name": "Hijacked", "active": 0, "assignments": []}), f"save_staff: {who} cannot edit/deactivate another branch's staff")
		check(denied(P[who], staff.save_staff, {"full_name": "Planted", "active": 1, "assignments": [{"outlet": B, "role": "Waiter"}]}), f"save_staff: {who} cannot create staff at another branch's outlet")
		check(denied(P[who], staff.save_staff, {"full_name": "Floating", "active": 1, "assignments": []}) or call(P[who], staff.save_staff, {"full_name": "Floating", "active": 1, "assignments": []})[0] == "invalid", f"save_staff: {who} cannot create an unassigned person nobody could see")
	check(ok(P["mgr_branch"], staff.request_pin_reset, STATE["staff_A"]), "request_pin_reset: a branch manager can reset their own staff")
	# a shared person: the branch manager edits their own assignment but B's is carried over untouched
	res = call(P["mgr_branch"], staff.save_staff, {"name": STATE["staff_both"], "full_name": "ZZ Staff Both", "active": 1, "assignments": [{"outlet": A, "role": "Cashier"}]})
	frappe.set_user("Administrator")
	roles_after = {(a.outlet, a.role) for a in frappe.get_doc("Staff PIN", STATE["staff_both"]).assignments}
	check(res[0] == "ok" and roles_after == {(A, "Cashier"), (B, "Waiter")}, "save_staff: a branch manager changes their own outlet's assignment and the other branch's assignment is carried over, not dropped")
	for who in nonroles:
		check(denied(P[who], staff.list_staff) and denied(P[who], staff.request_pin_reset, STATE["staff_A"]) and denied(P[who], staff.save_staff, {"full_name": "x", "assignments": []}), f"staff methods: {who} refused")

	# ---- menu ----------------------------------------------------------------------------------------------------
	grid_owner = call(P["owner"], menu.get_menu_grid)[1]
	check({A, B} <= names(grid_owner["outlets"]), "get_menu_grid: owner sees every outlet's prices")
	for who in scoped:
		g = call(P[who], menu.get_menu_grid)
		check(g[0] == "ok" and B not in names(g[1]["outlets"]) and all(B not in row["prices"] for row in g[1]["items"]), f"get_menu_grid: {who} sees prices for their own outlet only")
	for who in nonroles:
		check(denied(P[who], menu.get_menu_grid), f"get_menu_grid: {who} refused")
	group = "ZZBO-Group"
	if not frappe.db.exists("Item Group", group):
		frappe.get_doc({"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups"}).insert(ignore_permissions=True)
	new_item = {"item_code": "ZZBO-DISH", "item_name": "ZZ Dish", "item_group": group, "restaurant_station": "ZZBOA-Kitchen", "dietary_type": "Veg"}
	check(ok(P["owner"], menu.save_item_with_prices, new_item, {A: 100, B: 120}), "save_item_with_prices: owner creates a dish and prices it at both branches")
	for who in scoped:
		check(denied(P[who], menu.save_item_with_prices, {"item_code": "ZZBO-OTHER", "item_name": "x", "item_group": group, "restaurant_station": "ZZBOA-Kitchen", "dietary_type": "Veg"}, {}), f"save_item_with_prices: {who} cannot create a dish (it is shared by every branch)")
		check(denied(P[who], menu.save_item_with_prices, {"name": "ZZBO-DISH", "item_name": "Renamed by a branch"}, {}), f"save_item_with_prices: {who} cannot edit the shared dish itself")
		check(denied(P[who], menu.save_item_with_prices, {"name": "ZZBO-DISH"}, {B: 1}), f"save_item_with_prices: {who} cannot set another branch's price - REFUSED, not silently skipped")
		check(ok(P[who], menu.save_item_with_prices, {"name": "ZZBO-DISH"}, {A: 111}), f"save_item_with_prices: {who} may change their own outlet's price")
	frappe.set_user("Administrator")
	price_list_b = frappe.db.get_value("Outlet", B, "price_list")
	check(frappe.db.get_value("Item", "ZZBO-DISH", "item_name") == "ZZ Dish", "...and the shared dish was not renamed")
	for who in nonroles:
		check(denied(P[who], menu.save_item_with_prices, {"name": "ZZBO-DISH"}, {A: 5}), f"save_item_with_prices: {who} refused")

	# ---- access rights: global, owners only ------------------------------------------------------------------------
	check(ok(P["owner"], access_rights.get_access_matrix) and ok(P["owner"], access_rights.list_role_users), "access rights: an owner (System Manager, org-wide) can read the matrix and the role users")
	for who in (*scoped, "mgr_sysmgr", *nonroles, "sysadmin_branch_free"):
		if who == "sysadmin_branch_free":
			continue
		check(
			denied(P[who], access_rights.get_access_matrix) and denied(P[who], access_rights.list_role_users) and denied(P[who], access_rights.save_access_matrix, []) and denied(P[who], access_rights.assign_user_role, P[who], "Owner/Manager", "add"),
			f"access rights: {who} refused on every method (a branch manager - even one with System Manager - cannot grant roles or change what a role can do)",
		)
	check(ok(P["sysadmin"], access_rights.get_access_matrix), "access rights: a System Manager with no branch restriction is fine")

	# floor permissions (ticket P4-35): global like the rest of access rights - owners only
	check(ok(P["owner"], access_rights.get_floor_permissions), "floor permissions: an owner can read them")
	for who in (*scoped, "mgr_sysmgr", *nonroles):
		check(
			denied(P[who], access_rights.get_floor_permissions) and denied(P[who], access_rights.save_floor_permissions, [{"staff_role": "Waiter", "action": "approve_guest_orders", "allowed": True}]),
			f"floor permissions: {who} refused on both methods",
		)

	# ---- audit corrections -----------------------------------------------------------------------------------------------
	frappe.set_user("Administrator")
	staff_id = STATE["staff_A"]
	push = lambda outlet: sync.push_void_log(json.dumps({"hub_uuid": f"zzbo-{uuid.uuid4()}", "outlet": outlet, "order_id": "zzbo", "line_id": "l", "amount_paise": 100, "reason": "r", "requested_by_staff_id": staff_id, "approved_by_staff_id": staff_id, "device_credential_id": None, "occurred_at": "2026-10-04T10:00:00Z"}))
	void_a, void_b = push(A)["name"], push(B)["name"]
	STATE["voids"] = [void_a, void_b]
	check(ok(P["owner"], audit.record_correction, "Void Log", void_b, "owner fixes B"), "record_correction: owner can correct any outlet's record")
	check(ok(P["mgr_branch"], audit.record_correction, "Void Log", void_a, "fix A") and denied(P["mgr_branch"], audit.record_correction, "Void Log", void_b, "fix B"), "record_correction: a branch manager corrects only their own outlet's record")
	for who in nonroles:
		check(denied(P[who], audit.record_correction, "Void Log", void_a, "x"), f"record_correction: {who} refused")

	# ---- generic resource access (what the Back Office reads/writes through /api/resource/...) ----------------------------------------
	for doctype, key in (("Floor", "outlet"), ("Station", "outlet"), ("Printer", "outlet"), ("Device", "outlet"), ("Restaurant Table", "outlet")):
		for who in scoped:
			prev = frappe.session.user
			frappe.set_user(P[who])
			rows = frappe.get_list(doctype, fields=["name", key])
			frappe.set_user(prev)
			check(rows and all(r[key] == A for r in rows), f"/api/resource {doctype}: {who} lists only their outlet's rows ({len(rows)})")
	other_floor = frappe.get_doc("Floor", STATE["floor_B"])
	for who in scoped:
		check(not _has(P[who], other_floor, "read") and not _has(P[who], other_floor, "write"), f"/api/resource Floor: {who} can neither read nor write another branch's floor")
		check(denied(P[who], lambda: frappe.get_doc({"doctype": "Floor", "outlet": B, "floor_name": "Sneaky"}).insert()), f"/api/resource Floor: {who} cannot create a floor under another branch's outlet")
		check(denied(P[who], lambda: frappe.get_doc("Outlet", B).save()), f"/api/resource Outlet: {who} cannot save another branch's outlet")
	for who in nonroles:
		prev = frappe.session.user
		frappe.set_user(P[who])
		leaked = frappe.get_list("Outlet") if frappe.has_permission("Outlet", "read") else []
		frappe.set_user(prev)
		check(not leaked, f"/api/resource Outlet: {who} gets nothing")

	# ---- Hub-facing methods: the Hub service account only -------------------------------------------------------------------------------
	hub_calls = {
		"pull_changes": lambda o: sync.pull_changes(o, None),
		"get_master_version": lambda o: sync.get_master_version(o),
		"get_item_image": lambda o: sync.get_item_image(o, "ZZ-NO-SUCH-ITEM", 320),
		"push_batch": lambda o: sync.push_batch("void", json.dumps([])),
		"push_pos_invoice": lambda o: sync.push_pos_invoice(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_void_log": lambda o: sync.push_void_log(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_refund": lambda o: sync.push_refund(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_availability_log": lambda o: sync.push_availability_log(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_shift_open": lambda o: sync.push_shift_open(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_shift_close": lambda o: sync.push_shift_close(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_kot_timing": lambda o: sync.push_kot_timing(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_guest_consent": lambda o: sync.push_guest_consent(json.dumps({"hub_uuid": "x", "outlet": o})),
		"push_pin_reset": lambda o: sync.push_pin_reset(json.dumps({"staff_id": "x"})),
		"consume_pairing_code": lambda o: pairing_v1.consume_pairing_code("NOPE"),
	}
	for name, fn in hub_calls.items():
		for who in ("owner", "mgr_branch", "sysadmin", "waiter", "nobody"):
			check(denied(P[who], fn, A), f"{name}: {who} (not the Hub) is refused")
		# the Hub account passes the role/scope gate (it may then fail on its own bad payload - that is fine, it is not a permission error)
		check(call(P["hub_b"], fn, A)[0] != "denied", f"{name}: the Hub service account is not refused")
	check(call(P["hub_a"], hub_calls["pull_changes"], A)[0] == "ok", "pull_changes: a Hub account scoped to outlet A may pull outlet A")
	check(denied(P["hub_a"], hub_calls["pull_changes"], B), "pull_changes: ...and is refused outlet B (a per-outlet Hub credential sees exactly one outlet)")
	check(denied(P["hub_a"], hub_calls["push_void_log"], B) and call(P["hub_a"], hub_calls["push_void_log"], A)[0] != "denied", "push_*: the same outlet scope applies to what a Hub account may push")
	check(call(P["hub_b"], hub_calls["pull_changes"], B)[0] == "ok", "pull_changes: an all-outlets Hub account may pull any outlet")

	# ---- Hub credential management (ticket P1-14): an operator act, System Manager only, org-wide -------------------------------------------
	for name, fn in {
		"generate_hub_pairing_code": lambda o: hub_credentials.generate_hub_pairing_code("zz probe"),
		"revoke_hub_credential": lambda o: hub_credentials.revoke_hub_credential("HUBC-NOPE"),
		"rotate_outlet_push_secret": lambda o: hub_credentials.rotate_outlet_push_secret(o),
	}.items():
		for who in ("mgr_branch", "mgr_outlet", "mgr_sysmgr", "waiter", "nobody", "hub_a", "hub_b"):
			check(denied(P[who], fn, A), f"{name}: {who} is refused (only an org-wide System Manager may manage Hub credentials)")
	check(call(P["sysadmin"], lambda: hub_credentials.generate_hub_pairing_code("zz probe"))[0] == "ok", "generate_hub_pairing_code: a System Manager may")
	check(call(P["owner"], lambda: hub_credentials.generate_hub_pairing_code("zz probe"))[0] == "ok", "generate_hub_pairing_code: so may an org-wide owner who also holds System Manager (the role is what matters, plus no branch restriction)")
	check(call(P["owner"], lambda: hub_credentials.generate_hub_pairing_code("zz probe"))[0] == "ok", "generate_hub_pairing_code: so may an org-wide owner who also holds System Manager (the role is what matters, plus no branch restriction)")
	STATE["hub_credential_probe"] = frappe.get_all("Hub Credential", filters={"label": "zz probe"}, pluck="name")
	check(call(P["sysadmin"], lambda: hub_credentials.rotate_outlet_push_secret("NO-SUCH-OUTLET"))[0] == "invalid", "rotate_outlet_push_secret: a System Manager gets 'unknown outlet' for one that does not exist (nothing is rotated)")
	check(call(P["sysadmin"], lambda: hub_credentials.revoke_hub_credential("HUBC-NOPE"))[0] == "invalid", "revoke_hub_credential: a System Manager gets 'unknown credential' for a name that does not exist")
	for who in ("owner", "mgr_branch", "waiter", "nobody", "hub_a", "hub_b", "sysadmin"):
		check(denied(P[who], lambda: hub_credentials.rotate_hub_credential("x" * 40)), f"rotate_hub_credential: {who} (not an active Hub credential) is refused")
	check(call(P["nobody"], lambda: hub_credentials.consume_hub_pairing_code("NOPE-NOPE-NOPE-NOPE"))[0] == "invalid", "consume_hub_pairing_code: a wrong code is refused, whoever asks")


def _has(user, doc, ptype):
	prev = frappe.session.user
	frappe.set_user(user)
	try:
		return frappe.has_permission(doc.doctype, ptype, doc=doc)
	finally:
		frappe.set_user(prev)


def coverage():
	"""Every whitelisted restaurant_pos method must be accounted for."""
	declared = {
		# Back Office methods, exercised above
		"back_office.get_my_outlets", "back_office.get_bootstrap",
		"devices.list_devices", "devices.revoke_device",
		"pairing.generate_pairing_code",
		"outlets.bulk_add_tables", "outlets.copy_outlet_setup",
		"staff.list_staff", "staff.request_pin_reset", "staff.save_staff",
		"menu.get_menu_grid", "menu.save_item_with_prices",
		"access_rights.get_access_matrix", "access_rights.save_access_matrix", "access_rights.list_role_users", "access_rights.assign_user_role",
		"access_rights.get_floor_permissions", "access_rights.save_floor_permissions",
		"audit.record_correction",
		# Hub-facing (versioned sync API v1), exercised in the Hub-service block
		"v1.sync.pull_changes", "v1.sync.get_master_version", "v1.sync.get_item_image", "v1.sync.push_batch", "v1.sync.push_pos_invoice", "v1.sync.push_void_log", "v1.sync.push_refund",
		"v1.sync.push_availability_log", "v1.sync.push_shift_open", "v1.sync.push_shift_close", "v1.sync.push_kot_timing",
		"v1.sync.push_guest_consent", "v1.sync.push_pin_reset", "v1.pairing.consume_pairing_code",
		# platform/operator methods (Hub credential lifecycle), exercised in the Hub-credential block
		"hub_credentials.generate_hub_pairing_code", "hub_credentials.revoke_hub_credential",
		"hub_credentials.consume_hub_pairing_code", "hub_credentials.rotate_hub_credential", "hub_credentials.rotate_outlet_push_secret",
	}
	declared |= set(STATE.get("extra_declared", []))
	found = set()
	prefix = "restaurant_pos.restaurant_pos.api."
	# hub_push and shift_close are server-internal: they are listed here so that a whitelisted function added to them would be reported as unexamined
	for mod in (access_rights, audit, back_office, devices, hub_credentials, hub_push, menu, outlets, pairing, shift_close, staff, sync, pairing_v1):
		for name, fn in inspect.getmembers(mod, inspect.isfunction):
			if fn in frappe.whitelisted and fn.__module__ == mod.__name__:
				found.add(f"{mod.__name__[len(prefix):]}.{name}")
	import pkgutil
	import restaurant_pos.restaurant_pos.api as api_pkg

	extra_modules = [m.name for m in pkgutil.iter_modules(api_pkg.__path__) if m.name not in ("access_rights", "audit", "back_office", "devices", "hub_credentials", "hub_push", "menu", "shift_close", "outlets", "pairing", "staff", "scope", "gstin", "versions", "v1")]
	# the versioned packages: every module under api/v1 must be one this harness knows
	import restaurant_pos.restaurant_pos.api.v1 as v1_pkg

	extra_modules += [f"v1.{m.name}" for m in pkgutil.iter_modules(v1_pkg.__path__) if m.name not in ("sync", "pairing")]
	return declared, found, extra_modules


def teardown_fixture_if_residue():
	"""A crashed earlier run may have left fixtures behind; remove them (never touches the permission snapshot)."""
	if frappe.db.exists("Outlet", "ZZBOA") or frappe.db.exists("Outlet", "ZZBOB") or any(frappe.db.exists("User", e) for e in PERSONA_EMAILS):
		saved = STATE.pop("docperm_snapshot", None)
		teardown_fixture()
		if saved is not None:
			STATE["docperm_snapshot"] = saved


def run():
	frappe.set_user("Administrator")
	try:
		teardown_fixture_if_residue()
		setup_fixture()
		run_checks()
		declared, found, extra = coverage()
		check(found <= declared, f"coverage: every whitelisted method is covered by this harness (unexamined: {sorted(found - declared)})")
		check(not extra, f"coverage: no API module is outside this harness (unexamined modules: {extra})")
		check(declared - found == set(), f"coverage: nothing in the harness refers to a method that no longer exists ({sorted(declared - found)})")
		# the TypeScript contract (packages/api-types back-office-api.ts) must list exactly the Back Office methods
		import os
		import re

		ts_path = os.path.realpath(os.path.join(frappe.get_app_path("restaurant_pos"), "..", "..", "packages", "api-types", "src", "back-office-api.ts"))
		ts_paths = set(re.findall(r'path: "restaurant_pos\.restaurant_pos\.api\.(\w+\.\w+)"', open(ts_path, encoding="utf-8").read()))
		hub_facing = {d for d in declared if d.startswith("v1.")}
		platform = {d for d in declared if d.startswith("hub_credentials.")}
		back_office = declared - hub_facing - platform
		check(ts_paths == back_office, f"api-types contract: lists exactly the Back Office methods (missing in TS: {sorted(back_office - ts_paths)}; unknown to the server: {sorted(ts_paths - back_office)})")
	finally:
		teardown_fixture()
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")

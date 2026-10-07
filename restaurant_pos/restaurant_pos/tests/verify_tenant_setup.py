# Tickets P3-40 / P3-41: the tenant setup script and the onboarding it supports. Builds a REAL two-branch test client on the dev
# company (outlets ZZT1 and ZZT2, two stations each, the sample menu, one staff member working at BOTH branches and one at one),
# and proves:
#   - the script finishes in seconds and ends with every check green ("ready for pairing");
#   - it is idempotent: a second run creates nothing and changes nothing;
#   - the shared menu reaches EACH outlet routed to THAT outlet's own station (the fix this ticket needed: an Item links to one
#     station, but a menu is shared);
#   - the staff member at both branches is in both Hubs' pulls, the one-branch member only in one;
#   - device pairing really works against the result (the outlet passes ERPNext's own "fully configured" rule);
#   - the check FAILS, naming the outlet, when a station is missing - so it is a real smoke test, not a rubber stamp;
#   - a company that does not exist stops the script at the preflight with a clear message.
# Everything it creates is removed at the end; things that already existed (item groups, tax templates) are left alone.
#   bench --site dev.localhost execute restaurant_pos.restaurant_pos.tests.verify_tenant_setup.run

import json
import time

import frappe

from restaurant_pos.restaurant_pos.setup import tenant

failures = []
COMPANY = "YB"
CODES = ("ZZT1", "ZZT2")


def check(cond, label):
	print(("PASS  " if cond else "FAIL  ") + label)
	if not cond:
		failures.append(label)


def gstin(state_code: str, pan: str) -> str:
	"""A GSTIN with a correct check character (the Outlet and India Compliance both validate it)."""
	body = f"{state_code}{pan}1Z"
	chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
	total = 0
	for i, ch in enumerate(body):
		product = chars.index(ch) * (1 if i % 2 == 0 else 2)
		total += product // 36 + product % 36
	return body + chars[(36 - total % 36) % 36]


def config():
	def branch(code, name, pan):
		return {
			"code": code,
			"name": name,
			"city": "Bengaluru",
			"hub_mode": "Cloud",
			"hub_url": "https://hub-zz.example.test",
			"gstin": gstin("29", pan),
			"address": {"line1": "1 Test Street", "pincode": "560001", "state": "Karnataka"},
			"stations": ["Kitchen", "Bar"],
			"printers": [{"name": "Kitchen", "kind": "ESC/POS LAN", "address": "192.168.1.50:9100"}],
			"floors": [{"name": "Ground", "tables": ["T1", "T2", "T3"], "seats": 4}],
		}

	return {
		"company": COMPANY,
		"menu": "sample",
		"menu_name_prefix": "ZZ ",  # the dev site already has a real "Paneer Tikka"; names must be unique within an item group
		"branches": [branch("ZZT1", "ZZ Tenant North", "ABCDE1234F"), branch("ZZT2", "ZZ Tenant South", "PQRST5678U")],
		"staff": [
			{"full_name": "ZZ Floater", "staff_number": "ZZ-900", "assignments": [{"outlet": "ZZT1", "role": "Waiter"}, {"outlet": "ZZT2", "role": "Cashier"}]},
			{"full_name": "ZZ Northie", "staff_number": "ZZ-901", "assignments": [{"outlet": "ZZT1", "role": "Owner/Manager"}]},
		],
	}


def existing_names(doctype):
	return set(frappe.get_all(doctype, pluck="name"))


def run():
	frappe.set_user("Administrator")
	before = {dt: existing_names(dt) for dt in ("Item Group", "Item Tax Template")}
	cash_account_existed = bool(frappe.db.exists("Mode of Payment Account", {"parent": "Cash", "company": COMPANY}))
	cfg = config()
	try:
		cleanup(before, cash_account_existed)  # in case an earlier run died

		# ---- the preflight stops a bad start
		bad = tenant.setup_tenant(config={**cfg, "company": "No Such Company"})
		check(bad["ok"] is False and bad["errors"] and "does not exist" in bad["errors"][0][2], "a company that does not exist stops the script at the preflight, saying why")
		check(not frappe.db.exists("Outlet", "ZZT1"), "...and nothing was created")

		# ---- first run
		t0 = time.time()
		first = tenant.setup_tenant(config=cfg)
		elapsed = time.time() - t0
		check(first["ok"], f"the script completes without errors ({first['counts']})")
		check(elapsed < 60, f"...in {elapsed:.1f} s (the ticket's budget is 30 minutes for the whole onboarding)")
		check(all(frappe.db.exists("Outlet", c) for c in CODES), "both outlets exist")
		check(frappe.db.count("Station", {"outlet": ["in", list(CODES)]}) == 4 and frappe.db.count("Restaurant Table", {"outlet": ["in", list(CODES)]}) == 6, "each outlet has its two stations and three tables")

		# ---- idempotent
		second = tenant.setup_tenant(config=cfg)
		check(second["ok"] and not second["counts"].get("created"), f"a second run creates nothing ({second['counts']})")
		check(frappe.db.count("Item", {"item_code": ["like", "SAMPLE-%"]}) == len(tenant.SAMPLE_MENU), "...and there are no duplicate menu items")

		# ---- the smoke test
		report = tenant.check_tenant(config=cfg)
		check(report["ok"], "check_tenant: the tenant is READY for pairing (every check green)")

		# ---- the shared menu is routed per outlet
		from restaurant_pos.restaurant_pos.api.v1.sync import pull_changes

		views = {c: pull_changes(c) for c in CODES}
		for c in CODES:
			menu = [m for m in views[c]["menu_items"] if m["item_code"].startswith("SAMPLE-")]
			check(len(menu) == len(tenant.SAMPLE_MENU), f"{c}: the Hub receives the whole sample menu ({len(menu)} items)")
			check(all(m["station"] and m["station"].startswith(f"{c}-") for m in menu), f"{c}: every item is routed to {c}'s OWN station, not the other outlet's")
		check({m["station"] for m in views["ZZT2"]["menu_items"] if m["item_code"] == "SAMPLE-LIME-SODA"} == {"ZZT2-Bar"}, "a drink at ZZT2 goes to ZZT2-Bar (it is linked to ZZT1-Bar in the Item)")
		check(frappe.db.get_value("Item", "SAMPLE-LIME-SODA", "restaurant_station") == "ZZT1-Bar", "(the Item itself is linked to the first outlet's station, as ERPNext's single link requires)")
		prices = {c: {m["item_code"]: m["price_paise"] for m in views[c]["menu_items"]} for c in CODES}
		check(prices["ZZT1"]["SAMPLE-DAL-MAKHANI"] == 25000 and prices["ZZT2"]["SAMPLE-DAL-MAKHANI"] == 25000, "prices are per outlet (each outlet has its own price list) and are integer paise")
		gst = {m["item_code"]: m["gst_rate_pct"] for m in views["ZZT1"]["menu_items"]}
		check(gst["SAMPLE-DAL-MAKHANI"] == 5 and gst["SAMPLE-COLD-COFFEE"] == 18, "each item carries its GST slab (5% and 18%) from its Item Tax Template")

		def staff_names(c):
			return {s["name"] for s in views[c]["staff"] if s["active"]}  # (an inactive row is the Hub's cue to remove them)

		check("ZZ Floater" in staff_names("ZZT1") and "ZZ Floater" in staff_names("ZZT2"), "the staff member who works at BOTH branches is in both Hubs' pulls")
		check("ZZ Northie" in staff_names("ZZT1") and "ZZ Northie" not in staff_names("ZZT2"), "the one-branch staff member is only in their own branch's pull")
		roles = {c: {s["name"]: s["role"] for s in views[c]["staff"] if s["active"]}.get("ZZ Floater") for c in CODES}
		check(roles["ZZT1"] != roles["ZZT2"], f"...with the role each branch gave them ({roles['ZZT1']} at ZZT1, {roles['ZZT2']} at ZZT2)")

		# ---- pairing really works against it
		from restaurant_pos.restaurant_pos.api.pairing import generate_pairing_code
		from restaurant_pos.restaurant_pos.api.v1.pairing import consume_pairing_code

		code = generate_pairing_code("ZZT2", "Waiter")["code"]
		paired = consume_pairing_code(code)
		check(paired["outlet"] == "ZZT2" and paired["hub_url"] == "https://hub-zz.example.test" and paired["hub_mode"] == "Cloud", "a device can pair to the new outlet: it passes ERPNext's own 'outlet fully configured' rule")

		# ---- the check is a real smoke test: break something and it must say so
		frappe.delete_doc("Station", "ZZT2-Bar", force=1, ignore_permissions=True)
		frappe.db.commit()
		broken = tenant.check_tenant(config=cfg)
		bad_lines = [label for ok, label in broken["checks"] if not ok]
		check(not broken["ok"] and any("ZZT2" in label and "station" in label for label in bad_lines), f"with a station missing, check_tenant FAILS and names the outlet ({[l[:70] for l in bad_lines][:2]})")
		check(not any("ZZT1" in label for label in bad_lines), "...and does not blame the outlet that is fine")
		again = tenant.setup_tenant(config=cfg)
		check(again["ok"] and again["counts"].get("created") == 1, "re-running the script repairs exactly the missing piece (1 created)")
		check(tenant.check_tenant(config=cfg)["ok"], "...and the check is green again")
	finally:
		cleanup(before, cash_account_existed)
		left = [frappe.db.exists("Outlet", c) for c in CODES] + [frappe.db.count("Item", {"item_code": ["like", "SAMPLE-%"]})]
		check(not any(left), "everything the test created was removed")
	print("ALL PASS" if not failures else f"{len(failures)} FAILED")
	if failures:
		raise SystemExit(1)


def cleanup(before, cash_account_existed):
	frappe.set_user("Administrator")
	frappe.flags.allow_audit_purge = True

	def drop(doctype, filters):
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			try:
				frappe.delete_doc(doctype, name, force=1, ignore_permissions=True)
			except Exception as err:  # noqa: BLE001
				frappe.db.rollback()
				print(f"   cleanup: {doctype} {name}: {str(err)[:100]}")

	drop("Device", {"outlet": ["in", list(CODES)]})
	drop("Pairing Code", {"outlet": ["in", list(CODES)]})
	for dt in ("Restaurant Table", "Floor", "Printer", "Station"):
		drop(dt, {"outlet": ["in", list(CODES)]})
	drop("Staff PIN", {"full_name": ["like", "ZZ %"]})
	for ip in frappe.get_all("Item Price", filters={"item_code": ["like", "SAMPLE-%"]}, pluck="name"):
		frappe.delete_doc("Item Price", ip, force=1, ignore_permissions=True)
	drop("Item", {"item_code": ["like", "SAMPLE-%"]})
	drop("Outlet", {"name": ["in", list(CODES)]})
	drop("POS Profile", {"name": ["like", "ZZ Tenant %"]})
	drop("Warehouse", {"warehouse_name": ["like", "ZZ Tenant %"]})
	drop("Price List", {"name": ["like", "ZZ Tenant %"]})
	drop("Address", {"address_title": ["like", "ZZ Tenant %"]})
	drop("Branch", {"name": ["like", "ZZ Tenant %"]})
	for dt in ("Item Group", "Item Tax Template"):
		# only what the test itself created is removed. With no record of what existed before it removes NOTHING: an empty snapshot
		# once made a scratch call delete every item group on the dev site.
		if not before.get(dt):
			print(f"   cleanup: no snapshot of existing {dt} records, so none are deleted")
			continue
		for name in existing_names(dt) - before[dt]:
			try:
				frappe.delete_doc(dt, name, force=1, ignore_permissions=True)
			except Exception as err:  # noqa: BLE001
				frappe.db.rollback()
				print(f"   cleanup: {dt} {name}: {str(err)[:100]}")
	if not cash_account_existed:
		frappe.db.sql("delete from `tabMode of Payment Account` where parent='Cash' and company=%s", COMPANY)
	frappe.flags.allow_audit_purge = False
	frappe.db.commit()

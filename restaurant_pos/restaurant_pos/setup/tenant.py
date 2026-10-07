# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-40 (Tech Architecture section 5, load safety rule 9): "Script that creates a new tenant's baseline: tax templates,
# POS Profile, roles, printers, outlet mapping, sample menu. A new restaurant tenant is ready for pairing in under 30 minutes using
# only the script and a short checklist." Used alongside the onboarding guide (docs/runbooks/tenant-onboarding.md, ticket P3-41).
#
#   bench --site <site> execute restaurant_pos.restaurant_pos.setup.tenant.setup_tenant --kwargs '{"config_path": "/path/tenant.json"}'
#   bench --site <site> execute restaurant_pos.restaurant_pos.setup.tenant.check_tenant --kwargs '{"config_path": "/path/tenant.json"}'
#
# `setup_tenant` is IDEMPOTENT: run it again after a failure or an edit to the file and it creates only what is missing; it never
# changes or deletes anything that exists (an outlet's settings, a price, a menu item someone has edited in Desk are left as they
# are). It prints one line per step: created / exists / SKIPPED / ERROR, and stops at the first ERROR in the preflight.
# `check_tenant` is READ-ONLY: the smoke test. It ends with a real pull_changes for every outlet - exactly what the outlet's Hub
# will do - and reports what each Hub would receive.
#
# THE COMPANY is not created here: it comes from ERPNext's own setup (chart of accounts, fiscal year, currency) and must exist.
# Pairing (Hub credential, device codes) is deliberately NOT done here: it is an operator act with its own audit trail (the guide).
#
# Config (JSON):
#   {
#     "company": "My Restaurants Pvt Ltd",
#     "menu": "sample",                                   # "sample" or omit
#     "menu_name_prefix": "",                             # optional text put in front of the sample item NAMES (a name must be unique in its item group)
#     "branches": [{
#        "code": "KOR", "name": "Koramangala", "city": "Bengaluru", "hub_mode": "Cloud", "hub_url": "https://hub1.example.com",
#        "gstin": "29ABCDE1234F1Z5", "address": {"line1": "...", "pincode": "560034", "state": "Karnataka"},
#        "invoice_series_prefix": "KOR/", "gst_rate_pct": 5,
#        "stations": ["Kitchen", "Bar"], "printers": [{"name": "Kitchen", "kind": "ESC/POS LAN", "address": "192.168.1.50:9100"}],
#        "floors": [{"name": "Ground", "tables": ["T1", "T2", "T3"], "seats": 4}]
#     }],
#     "staff": [{"full_name": "Asha K.", "staff_number": "101", "assignments": [{"outlet": "KOR", "role": "Owner/Manager"}]}]
#   }

import json
import time

import frappe
from frappe.utils import flt, now_datetime

from restaurant_pos.restaurant_pos.doctype.outlet.outlet import ERP_MAPPING_FIELDS

GST_SLABS = (5, 12, 18, 28)

# A small, real menu: enough to prove the whole flow (kitchen routing, GST slabs, veg/non-veg, an 86-able item). Prices are rupees.
SAMPLE_MENU = [
	# (item code, name, group, station, dietary, price, gst slab)
	("SAMPLE-PANEER-TIKKA", "Paneer Tikka", "Starters", "Kitchen", "Veg", 280, 5),
	("SAMPLE-CHICKEN-65", "Chicken 65", "Starters", "Kitchen", "Non-veg", 260, 5),
	("SAMPLE-DAL-MAKHANI", "Dal Makhani", "Mains", "Kitchen", "Veg", 250, 5),
	("SAMPLE-BUTTER-CHICKEN", "Butter Chicken", "Mains", "Kitchen", "Non-veg", 360, 5),
	("SAMPLE-GULAB-JAMUN", "Gulab Jamun", "Desserts", "Kitchen", "Veg", 120, 5),
	("SAMPLE-LIME-SODA", "Fresh Lime Soda", "Drinks", "Bar", "Veg", 90, 5),
	("SAMPLE-MASALA-CHAI", "Masala Chai", "Drinks", "Bar", "Veg", 60, 5),
	("SAMPLE-COLD-COFFEE", "Cold Coffee", "Drinks", "Bar", "Veg", 140, 18),
]
SAMPLE_GROUPS = ("Starters", "Mains", "Desserts", "Drinks")
RESTAURANT_HSN = "996331"  # "Services provided by restaurants, cafes and similar eating facilities"


class Report:
	def __init__(self):
		self.lines: list[tuple[str, str, str]] = []

	def add(self, status: str, what: str, detail: str = ""):
		self.lines.append((status, what, detail))
		print(f"{status:<8} {what}{(' - ' + detail) if detail else ''}")

	@property
	def errors(self):
		return [line for line in self.lines if line[0] == "ERROR"]

	def counts(self):
		out: dict[str, int] = {}
		for status, _w, _d in self.lines:
			out[status] = out.get(status, 0) + 1
		return out


def load_config(config_path: str | None = None, config: dict | str | None = None) -> dict:
	if config_path:
		with open(config_path, encoding="utf-8") as f:
			return json.load(f)
	return json.loads(config) if isinstance(config, str) else (config or {})


def _ensure(report: Report, doctype: str, name: str, values: dict, label: str | None = None):
	"""Creates the document if it does not exist. Never touches one that does."""
	label = label or f"{doctype} {name}"
	if frappe.db.exists(doctype, name):
		report.add("exists", label)
		return frappe.get_doc(doctype, name)
	try:
		# POS Profile is named by whoever creates it ("Prompt" naming): pass the name explicitly
		extra = {"__newname": name} if doctype == "POS Profile" else {}
		doc = frappe.get_doc({"doctype": doctype, **values, **extra})
		doc.insert(ignore_permissions=True)
		report.add("created", label)
		return doc
	except Exception as err:  # noqa: BLE001
		frappe.db.rollback()
		report.add("ERROR", label, str(err).split("\n")[0][:200])
		return None


def setup_tenant(config_path: str | None = None, config: dict | str | None = None) -> dict:
	frappe.set_user("Administrator")
	cfg = load_config(config_path, config)
	report = Report()
	started = time.time()

	company = cfg.get("company")
	if not company or not frappe.db.exists("Company", company):
		report.add("ERROR", "Company", f'"{company}" does not exist: create it in ERPNext first (its setup wizard makes the chart of accounts)')
		return _finish(report, started)
	abbr, currency, country = frappe.db.get_value("Company", company, ["abbr", "default_currency", "country"])
	if currency != "INR" or country != "India":
		report.add("ERROR", "Company", f"{company} is {country}/{currency}: this product bills GST in INR for India")
		return _finish(report, started)
	report.add("ok", f"Company {company}", f"{abbr}, {currency}")

	# 1. POS Settings: the Hub creates Sales Invoices directly (docs/notes/P3-06-erpnext-v16-pos-billing-model.md)
	current = frappe.db.get_single_value("POS Settings", "invoice_type")
	if current == "Sales Invoice":
		report.add("exists", "POS Settings invoice type", "Sales Invoice")
	elif frappe.db.count("POS Opening Entry", {"docstatus": 1, "status": "Open"}):
		report.add("ERROR", "POS Settings invoice type", f'is "{current}" and ERPNext will not change it while a POS Opening Entry is open: close those shifts, then re-run')
		return _finish(report, started)
	else:
		frappe.db.set_single_value("POS Settings", "invoice_type", "Sales Invoice")
		report.add("created", "POS Settings invoice type", f'"{current}" -> "Sales Invoice"')

	# 2. the shared masters this app owns: GST ledger + header template, walk-in customer, roles, owner defaults
	from restaurant_pos.restaurant_pos.setup.access_rights_setup import ensure_base_roles, seed_owner_defaults
	from restaurant_pos.restaurant_pos.setup.billing_setup import ensure_billing_masters

	ensure_billing_masters()
	ensure_base_roles()
	seed_owner_defaults()
	report.add("ok", "GST ledger accounts, invoice tax template, walk-in customer, roles and owner permissions")
	slab_templates = _ensure_item_tax_templates(report, company, abbr)
	_ensure_cash_mode(report, company)

	# 3. the shared menu structure
	if cfg.get("menu") == "sample":
		for group in SAMPLE_GROUPS:
			_ensure(report, "Item Group", group, {"item_group_name": group, "parent_item_group": "All Item Groups", "is_group": 0})

	# 4. each branch / outlet
	outlet_names = []
	for branch in cfg.get("branches", []):
		outlet = _setup_branch(report, company, abbr, branch)
		if outlet:
			outlet_names.append(outlet)

	# 5. the shared menu: items once, a price per outlet
	if cfg.get("menu") == "sample" and outlet_names:
		_setup_sample_menu(report, outlet_names, slab_templates, cfg.get("menu_name_prefix", ""))

	# 6. staff (their PIN is set on their own device after a reset request: nothing secret is created here)
	for person in cfg.get("staff", []):
		_setup_staff(report, person, outlet_names)

	frappe.db.commit()
	return _finish(report, started)


def _finish(report: Report, started: float) -> dict:
	elapsed = round(time.time() - started, 1)
	counts = report.counts()
	print(f"\n{'FAILED' if report.errors else 'DONE'} in {elapsed}s: {', '.join(f'{n} {k}' for k, n in sorted(counts.items()))}")
	if not report.errors:
		print("Next: run check_tenant, then follow the guide (Hub pairing code, device codes, install, smoke test).")
	return {"ok": not report.errors, "seconds": elapsed, "counts": counts, "errors": [list(e) for e in report.errors], "lines": [list(l) for l in report.lines]}


def _ensure_cash_mode(report: Report, company: str):
	"""A POS Profile takes cash only if the Cash mode of payment has an account for the company."""
	if frappe.db.exists("Mode of Payment Account", {"parent": "Cash", "company": company}):
		report.add("exists", "Cash payment account", company)
		return
	account = frappe.db.get_value("Company", company, "default_cash_account")
	if not account:
		report.add("ERROR", "Cash payment account", f"{company} has no default cash account to use for Cash")
		return
	mode = frappe.get_doc("Mode of Payment", "Cash")
	mode.append("accounts", {"company": company, "default_account": account})
	mode.save(ignore_permissions=True)
	report.add("created", "Cash payment account", account)


def _ensure_item_tax_templates(report: Report, company: str, abbr: str) -> dict[int, str]:
	"""One Item Tax Template per GST slab on the two canonical accounts (CGST/SGST output), like the invoice header template: the
	rate varies per item, the accounts never do (billing_setup.py explains why)."""
	cgst, sgst = f"CGST Output - {abbr}", f"SGST Output - {abbr}"
	out = {}
	for slab in GST_SLABS:
		name = f"GST {slab}% - {abbr}"
		if frappe.db.exists("Item Tax Template", name):
			out[slab] = name
			continue
		if not (frappe.db.exists("Account", cgst) and frappe.db.exists("Account", sgst)):
			report.add("ERROR", f"Item Tax Template {name}", "the CGST/SGST output accounts are missing")
			continue
		doc = _ensure(
			report,
			"Item Tax Template",
			name,
			{"title": f"GST {slab}%", "company": company, "taxes": [{"tax_type": cgst, "tax_rate": slab / 2}, {"tax_type": sgst, "tax_rate": slab / 2}]},
		)
		if doc:
			out[slab] = doc.name
	return out


def _setup_branch(report: Report, company: str, abbr: str, b: dict) -> str | None:
	code, name = b["code"], b["name"]
	branch = _ensure(report, "Branch", name, {"branch": name})
	if not branch:
		return None

	address_name = None
	if b.get("gstin"):
		addr = b.get("address", {})
		address_name = f"{name} GSTIN-Billing"
		_ensure(
			report,
			"Address",
			address_name,
			{
				"address_title": f"{name} GSTIN",
				"address_type": "Billing",
				"address_line1": addr.get("line1", name),
				"city": b.get("city") or addr.get("city") or name,
				"state": addr.get("state", ""),
				"pincode": addr.get("pincode", ""),
				"country": "India",
				"gst_category": "Registered Regular",
				"gstin": b["gstin"],
				"links": [{"link_doctype": "Company", "link_name": company}],
			},
			f"GSTIN address of {name}",
		)

	price_list = f"{name} Price List"
	_ensure(report, "Price List", price_list, {"price_list_name": price_list, "currency": "INR", "enabled": 1, "selling": 1})
	warehouse = f"{name} - {abbr}"
	root = frappe.db.get_value("Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ["is", "not set"]}, "name")
	_ensure(report, "Warehouse", warehouse, {"warehouse_name": name, "company": company, "parent_warehouse": root, "is_group": 0})

	profile = f"{name} POS"
	write_off, cost_center = frappe.db.get_value("Company", company, ["write_off_account", "cost_center"])
	_ensure(
		report,
		"POS Profile",
		profile,
		{
			"company": company,
			"warehouse": warehouse,
			"currency": "INR",
			"country": "India",
			"selling_price_list": price_list,
			"write_off_account": write_off,
			"write_off_cost_center": cost_center,
			"update_stock": 1,
			"payments": [{"mode_of_payment": "Cash", "default": 1}],
		},
	)

	outlet = _ensure(
		report,
		"Outlet",
		code,
		{
			"outlet_name": name,
			"short_code": code,
			"city": b.get("city"),
			"outlet_type": "Restaurant",
			"hub_mode": b.get("hub_mode", "Cloud"),
			"hub_url": b.get("hub_url"),
			"company": company,
			"branch": name,
			"gstin_address": address_name,
			"gstin": b.get("gstin"),
			"pos_profile": profile,
			"price_list": price_list,
			"warehouse": warehouse,
			"invoice_series_prefix": b.get("invoice_series_prefix") or f"{code}/",
			"gst_rate_pct": b.get("gst_rate_pct", 5),
		},
		f"Outlet {code} ({name})",
	)
	if not outlet:
		return None

	for station in b.get("stations", ["Kitchen"]):
		_ensure(report, "Station", f"{code}-{station}", {"outlet": code, "station_name": station}, f"Station {code}-{station}")
	for printer in b.get("printers", []):
		_ensure(
			report,
			"Printer",
			f"{code}-{printer['name']}",
			{"outlet": code, "printer_name": printer["name"], "kind": printer.get("kind", "ESC/POS LAN"), "address": printer.get("address"), "paper_size": printer.get("paper_size", "80mm")},
			f"Printer {code}-{printer['name']}",
		)
	for floor in b.get("floors", []):
		floor_doc = _ensure(report, "Floor", f"{code}-{floor['name']}", {"outlet": code, "floor_name": floor["name"]}, f"Floor {code}-{floor['name']}")
		if not floor_doc:
			continue
		for label in floor.get("tables", []):
			_ensure(report, "Restaurant Table", f"{code}-{label}", {"outlet": code, "floor": floor_doc.name, "label": label, "seat_count": floor.get("seats", 4)}, f"Table {code}-{label}")
	return code


def _setup_sample_menu(report: Report, outlets: list[str], slab_templates: dict[int, str], prefix: str = ""):
	# the item points at the FIRST outlet's station of that name; every other outlet resolves its own namesake (sync._station_for_outlet)
	home = outlets[0]
	for code, name, group, station, dietary, price, slab in SAMPLE_MENU:
		name = f"{prefix}{name}"
		station_name = f"{home}-{station}"
		if not frappe.db.exists("Station", station_name):
			report.add("SKIPPED", f"Item {code}", f"{home} has no station called {station}")
			continue
		values = {
			"item_code": code,
			"item_name": name,
			"item_group": group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_sales_item": 1,
			"restaurant_station": station_name,
			"dietary_type": dietary,
			"gst_hsn_code": RESTAURANT_HSN,
		}
		if slab in slab_templates:
			values["taxes"] = [{"item_tax_template": slab_templates[slab]}]
		if not frappe.db.exists("Item", code) and frappe.db.exists("Item", {"item_group": group, "item_name": name}):
			report.add("SKIPPED", f"Item {code}", f'an item called "{name}" already exists in {group}')
			continue
		_ensure(report, "Item", code, values, f"Item {code}")
		for outlet in outlets:
			price_list = frappe.db.get_value("Outlet", outlet, "price_list")
			if not frappe.db.exists("Item Price", {"item_code": code, "price_list": price_list}):
				frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": price_list, "price_list_rate": price, "selling": 1}).insert(ignore_permissions=True)
				report.add("created", f"Price {code} at {outlet}", f"Rs.{price}")


def _setup_staff(report: Report, person: dict, outlets: list[str]):
	existing = frappe.db.get_value("Staff PIN", {"full_name": person["full_name"], "staff_number": person.get("staff_number")}, "name")
	if existing:
		report.add("exists", f"Staff {person['full_name']}")
		return
	assignments = [{"outlet": a["outlet"], "role": a["role"]} for a in person.get("assignments", []) if a["outlet"] in outlets or frappe.db.exists("Outlet", a["outlet"])]
	if not assignments:
		report.add("SKIPPED", f"Staff {person['full_name']}", "no valid outlet assignment")
		return
	try:
		frappe.get_doc(
			{"doctype": "Staff PIN", "full_name": person["full_name"], "staff_number": person.get("staff_number"), "active": 1, "pin_reset_requested_at": now_datetime(), "assignments": assignments}
		).insert(ignore_permissions=True)
		report.add("created", f"Staff {person['full_name']}", ", ".join(f"{a['outlet']}:{a['role']}" for a in assignments) + " (sets their own PIN on their device)")
	except Exception as err:  # noqa: BLE001
		frappe.db.rollback()
		report.add("ERROR", f"Staff {person['full_name']}", str(err).split("\n")[0][:200])


# ---------------------------------------------------------------------------------------------------------------------
# the smoke test

def check_tenant(config_path: str | None = None, config: dict | str | None = None) -> dict:
	"""READ-ONLY. Is this tenant ready for pairing? Ends with what each outlet's Hub would actually receive."""
	frappe.set_user("Administrator")
	cfg = load_config(config_path, config)
	results: list[tuple[bool, str]] = []

	def check(ok, label):
		results.append((bool(ok), label))
		print(("PASS  " if ok else "FAIL  ") + label)

	company = cfg.get("company")
	check(company and frappe.db.exists("Company", company), f"company {company} exists")
	check(frappe.db.get_single_value("POS Settings", "invoice_type") == "Sales Invoice", "POS Settings invoice type is Sales Invoice (Hub-created bills post directly)")
	abbr = frappe.db.get_value("Company", company, "abbr") if company and frappe.db.exists("Company", company) else None
	check(abbr and frappe.db.exists("Sales Taxes and Charges Template", f"GST 5% - {abbr}"), "the GST invoice tax template exists for the company")
	check(frappe.db.exists("Customer", "Walk-in Customer"), "the walk-in customer exists")
	check(all(frappe.db.exists("Role", r) for r in ("Owner/Manager", "Shift Supervisor", "Server/Waiter", "Kitchen Staff", "Cashier", "Hub Service")), "the restaurant roles exist")

	from restaurant_pos.restaurant_pos.api.v1.sync import pull_changes

	outlets = [b["code"] for b in cfg.get("branches", [])]
	hub_view = {}
	for code in outlets:
		if not frappe.db.exists("Outlet", code):
			check(False, f"outlet {code} exists")
			continue
		o = frappe.get_doc("Outlet", code)
		missing = [f for f in ERP_MAPPING_FIELDS if not o.get(f)] + ([] if o.hub_url else ["hub_url"])
		check(not missing, f"{code}: ready for device pairing (Company, Branch, GSTIN address, POS Profile, Price List, Warehouse, series, Hub URL){' - missing: ' + ', '.join(missing) if missing else ''}")
		if o.gstin_address and frappe.get_meta("Address").has_field("gstin"):  # (a field India Compliance adds)
			check(frappe.db.get_value("Address", o.gstin_address, "gstin") == o.gstin, f"{code}: the outlet's GSTIN matches its GSTIN address")
		check(frappe.db.count("Station", {"outlet": code}) >= 1, f"{code}: has at least one kitchen station")
		check(frappe.db.count("Restaurant Table", {"outlet": code}) >= 1, f"{code}: has at least one table")
		check(frappe.db.get_value("Price List", o.price_list, "enabled") == 1, f"{code}: its price list is enabled")
		check(frappe.db.exists("Mode of Payment Account", {"parent": "Cash", "company": company}), f"{code}: Cash has an account for the company (a bill can take cash)")
		# what the outlet's Hub will receive
		page = pull_changes(code)
		hub_view[code] = page
		menu = [m for m in page["menu_items"] if not m["removed"]]
		check(bool(menu), f"{code}: the Hub would receive {len(menu)} menu item(s)")
		check(all(m["station"] for m in menu), f"{code}: every menu item resolves to a station AT THIS OUTLET ({sum(1 for m in menu if not m['station'])} do not)")
		check(all(m["price_paise"] > 0 for m in menu), f"{code}: every menu item has a price")
		check(bool(page["tables"]) and bool(page["stations"]), f"{code}: the Hub would receive {len(page['tables'])} table(s) and {len(page['stations'])} station(s)")
		check(bool(page["push_secrets"]["current"]) and page["billing_settings"]["invoice_series_prefix"], f"{code}: the Hub would receive its push secret and invoice series")
	for person in cfg.get("staff", []):
		wanted = {a["outlet"] for a in person.get("assignments", [])}
		doc = frappe.db.get_value("Staff PIN", {"full_name": person["full_name"], "staff_number": person.get("staff_number")}, "name")
		check(doc, f"staff {person['full_name']} exists")
		for code in wanted & set(outlets):
			seen = any(s.get("name") == person["full_name"] for s in hub_view.get(code, {}).get("staff", []))
			check(seen, f"staff {person['full_name']} reaches the Hub of {code}")
	ok = all(r for r, _ in results)
	print(f"\n{'READY for pairing' if ok else 'NOT READY'}: {sum(1 for r, _ in results if r)}/{len(results)} checks")
	return {"ok": ok, "checks": [[r, label] for r, label in results]}

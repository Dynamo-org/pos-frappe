# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-13: menu items, modifiers and BOMs are entered through ERPNext's native Item/BOM
# forms for MVP — this module is the validation layer that makes native-form entry safe, not a
# second menu-editing UI (CLAUDE.md rule 4: master data is edited only in ERPNext/Back Office).
# Wired via hooks.py's doc_events on the core "Item" and "Item Price" doctypes, since neither is
# ours to add a validate() method to directly.

import frappe

from restaurant_pos.restaurant_pos.api.scope import require_org_wide, require_outlet

MAX_PRICE_RUPEES = 100_000


def validate_item(doc, method=None):
	"""Ticket P3-13's two Item-level rules: a unique name within its item group (Frappe's own
	uniqueness is only on item_code, not item_name), and — for anything actually sold — a
	kitchen station and a veg/non-veg mark, so nothing reaches the Hub's menu sync (P1-26/P3-03)
	missing what the KDS board and the guest menu both need to render it."""
	if doc.item_name and doc.item_group:
		duplicate = frappe.db.exists(
			"Item",
			{"item_group": doc.item_group, "item_name": doc.item_name, "name": ["!=", doc.name or ""]},
		)
		if duplicate:
			frappe.throw(
				f"An item with this name already exists in {doc.item_group}.",
				frappe.ValidationError,
			)

	if doc.is_sales_item:
		if not doc.get("restaurant_station"):
			frappe.throw("Choose a kitchen station for this item before it can be sold.", frappe.ValidationError)
		if not doc.get("dietary_type"):
			frappe.throw("Mark this item Veg or Non-veg before it can be sold.", frappe.ValidationError)


def validate_item_price(doc, method=None):
	"""Ticket P3-13: 0-Rs.1,00,000, and 0 only for an item explicitly marked complimentary —
	never a silent free item from a typo."""
	rate = doc.price_list_rate or 0
	if rate < 0 or rate > MAX_PRICE_RUPEES:
		frappe.throw(f"Enter a price between Rs.0 and Rs.{MAX_PRICE_RUPEES:,}.", frappe.ValidationError)
	if rate == 0:
		is_complimentary = frappe.db.get_value("Item", doc.item_code, "is_complimentary")
		if not is_complimentary:
			frappe.throw(
				f"Enter a price between Rs.0 and Rs.{MAX_PRICE_RUPEES:,} — 0 is only allowed for an item marked complimentary.",
				frappe.ValidationError,
			)


# Ticket P8.5-05 (Back Office Menu & pricing) + the buildable slice of P4-27: "availability" per
# outlet is not a separate flag anywhere in this system — an outlet only ever sees an item in its
# synced menu when it has an Item Price row on that outlet's own price_list (api/sync.py's
# pull_changes, unchanged by this ticket). So "available at this branch" here means exactly
# "has an Item Price row for this outlet's price_list" — toggling it off deletes that row rather
# than adding a new boolean nothing downstream reads. Two composite whitelisted methods per
# Frontend Specs C.2's own "Calls" line (plain Item/Item Group/Item Price CRUD goes through
# Frappe's generic /api/resource/<doctype> REST instead, permission-checked by Frappe itself,
# same as every other Back Office screen this session).


def _outlets_with_price_list():
	return frappe.get_list("Outlet", fields=["name", "outlet_name", "price_list"])


@frappe.whitelist()
def get_menu_grid(item_group: str | None = None) -> dict:
	if not frappe.has_permission("Item", "read"):
		frappe.throw("Not permitted to view the menu", frappe.PermissionError)

	outlets = _outlets_with_price_list()
	filters = {"is_sales_item": 1}
	if item_group:
		filters["item_group"] = item_group
	items = frappe.get_list(
		"Item",
		filters=filters,
		fields=["name", "item_name", "item_group", "restaurant_station", "dietary_type", "is_complimentary", "disabled", "description"],
		order_by="item_name asc",
	)

	price_rows = (
		frappe.get_list(
			"Item Price",
			filters={"item_code": ["in", [i.name for i in items]]},
			fields=["item_code", "price_list", "price_list_rate"],
		)
		if items
		else []
	)
	prices_by_item: dict[str, dict[str, float]] = {}
	for row in price_rows:
		prices_by_item.setdefault(row.item_code, {})[row.price_list] = row.price_list_rate

	grid = []
	for item in items:
		by_outlet = {}
		for outlet in outlets:
			rate = prices_by_item.get(item.name, {}).get(outlet.price_list) if outlet.price_list else None
			by_outlet[outlet.name] = {"available": rate is not None, "price": rate}
		grid.append({**item, "prices": by_outlet})

	switched_off_counts = {outlet.name: sum(1 for row in grid if not row["prices"][outlet.name]["available"]) for outlet in outlets}
	return {"outlets": outlets, "items": grid, "switched_off_counts": switched_off_counts}


@frappe.whitelist()
def save_item_with_prices(item: dict | str, prices: dict | str) -> dict:
	"""`item` is a dict of Item fields — `name` present means update an existing item, absent
	means create one. `prices` is `{outlet_name: price_in_rupees_or_None}`; None means "not
	available at this outlet" and deletes that outlet's Item Price row rather than merely zeroing
	it (a 0 price means something else entirely — complimentary, per validate_item_price)."""
	item = frappe.parse_json(item) if isinstance(item, str) else item
	prices = frappe.parse_json(prices) if isinstance(prices, str) else prices

	# Ticket P8.5-03: an Item is SHARED by every outlet; only its per-outlet prices are branch-scoped. A
	# branch-scoped manager may therefore change prices at their own outlets (item carries just its name) but
	# not create or edit the dish itself.
	item_fields = {k for k in item if k != "name"}
	if item_fields or not item.get("name"):
		require_org_wide("Creating or editing a menu item")
	# every outlet whose price is being set must be one the caller may write - checked up front so a refused
	# call changes nothing (before, an out-of-scope outlet was silently skipped)
	for outlet_name in (prices or {}):
		require_outlet(outlet_name, "write")

	if item.get("name"):
		if not frappe.db.exists("Item", item["name"]):
			frappe.throw("Not permitted to edit this item", frappe.PermissionError)
		doc = frappe.get_doc("Item", item["name"])
		if item_fields and not frappe.has_permission(doc=doc, ptype="write"):
			frappe.throw("Not permitted to edit this item", frappe.PermissionError)
		for field in ("item_name", "item_group", "description", "restaurant_station", "dietary_type", "is_complimentary", "disabled"):
			if field in item:
				doc.set(field, item[field])
		doc.save()
	else:
		if not frappe.has_permission("Item", "create"):
			frappe.throw("Not permitted to create an item", frappe.PermissionError)
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item["item_code"],
				"item_name": item.get("item_name") or item["item_code"],
				"item_group": item["item_group"],
				"description": item.get("description"),
				"restaurant_station": item.get("restaurant_station"),
				"dietary_type": item.get("dietary_type"),
				"is_complimentary": item.get("is_complimentary", 0),
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		)
		doc.insert()

	outlets_by_name = {o.name: o.price_list for o in _outlets_with_price_list()}
	for outlet_name, price in (prices or {}).items():
		price_list = outlets_by_name.get(outlet_name)
		if not price_list:
			continue
		# A price edit is still branch-scoped even though the item itself is shared (Frontend
		# Specs C.2's own "a branch-scoped manager... edits only their branch's price" line) — the
		# natural check here is the same Outlet read/write permission every other Back Office
		# screen already relies on (User Permissions on Outlet, decision P0-12), not a new
		# per-price permission concept.
		existing = frappe.db.get_value("Item Price", {"item_code": doc.name, "price_list": price_list}, "name")
		if price is None:
			if existing:
				frappe.delete_doc("Item Price", existing, ignore_permissions=True)
		elif existing:
			frappe.db.set_value("Item Price", existing, "price_list_rate", price)
		else:
			frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": doc.name,
					"price_list": price_list,
					"price_list_rate": price,
					"uom": doc.stock_uom or "Nos",
					"currency": "INR",
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()
	return {"name": doc.name}

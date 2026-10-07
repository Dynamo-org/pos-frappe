# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Backend for the Back Office's Outlet setup screen (ticket P8.5-09, Frontend Specs C.11.1).
# Plain CRUD on Outlet/Floor/Restaurant Table/Station/Printer goes through Frappe's own generic
# `/api/resource/<doctype>` REST API (already permission-checked by Frappe itself, same as
# api/back_office.py's get_my_outlets relies on) — this module only holds the two operations
# that have no generic-REST equivalent: bulk table creation and copying one outlet's setup onto
# another.

import frappe
from frappe.utils import cint

from restaurant_pos.restaurant_pos.api.scope import require_outlet


@frappe.whitelist()
def bulk_add_tables(floor: str, count: int, label_prefix: str, seat_count: int = 4) -> list:
	"""Creates `count` Restaurant Table rows on `floor`, labelled `<label_prefix>1`, `<label_prefix>2`,
	... — Frontend Specs C.11.1's "Bulk add tables". Skips any label that already exists under
	the floor's outlet, rather than failing the whole batch on one collision."""
	if not frappe.db.exists("Floor", floor) or not frappe.has_permission("Floor", "read", doc=floor):
		frappe.throw("Not permitted for this floor", frappe.PermissionError)  # same answer for missing and not-yours
	floor_doc = frappe.get_doc("Floor", floor)
	if not frappe.has_permission("Restaurant Table", "create"):
		frappe.throw("Not permitted to add tables", frappe.PermissionError)
	# Adding tables CHANGES the outlet: it needs write on it, not just sight of it.
	require_outlet(floor_doc.outlet, "write")

	count = cint(count)
	if count < 1 or count > 100:
		frappe.throw("Add between 1 and 100 tables at a time.", frappe.ValidationError)

	created = []
	for i in range(1, count + 1):
		label = f"{label_prefix}{i}"
		if frappe.db.exists("Restaurant Table", f"{floor_doc.outlet}-{label}"):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Restaurant Table",
				"outlet": floor_doc.outlet,
				"floor": floor,
				"label": label,
				"seat_count": cint(seat_count) or 4,
			}
		)
		doc.insert()
		created.append(doc.name)
	frappe.db.commit()
	return created


# Frontend Specs C.11.1: "choose a source outlet and what to copy (floors and tables layout,
# stations and printer routing, business hours, POS Profile settings). Company, GSTIN, price
# list and warehouse are never copied." Field-level choices for what "POS Profile settings"
# means in practice (the tax/billing fields that conceptually sit with POS Profile, not the
# `pos_profile` Link itself — copying that link would point the new outlet at the OLD outlet's
# specific POS Profile record, which is exactly the kind of cross-outlet coupling "never copied"
# is guarding against for warehouse/price list too) and the decision to exclude `fssai_license_number`
# / `upi_vpa` / `upi_payee_name` (physically/financially specific to one real location, not
# reusable) and a printer's `address` (a physical IP, not a template value) are this
# implementation's own calls — flagged in the ticket's STATUS_NOTES, not silently assumed.
_POS_SETTINGS_FIELDS = ["gst_rate_pct", "gst_inclusive", "service_charge_pct", "service_charge_gst_applicable", "receipt_language"]
_BUSINESS_HOURS_FIELDS = ["business_hours_opens", "business_hours_closes", "business_hours_day_ends"]


@frappe.whitelist()
def copy_outlet_setup(
	source_outlet: str,
	target_outlet: str,
	copy_floors_and_tables=1,
	copy_stations_and_printers=1,
	copy_business_hours=1,
	copy_pos_settings=1,
) -> dict:
	require_outlet(source_outlet, "read")
	require_outlet(target_outlet, "write")
	target = frappe.get_doc("Outlet", target_outlet)

	source = frappe.get_doc("Outlet", source_outlet)
	result = {"floors": [], "tables": [], "stations": [], "printers": []}

	if cint(copy_business_hours):
		for f in _BUSINESS_HOURS_FIELDS:
			target.set(f, source.get(f))

	if cint(copy_pos_settings):
		for f in _POS_SETTINGS_FIELDS:
			target.set(f, source.get(f))

	if cint(copy_business_hours) or cint(copy_pos_settings):
		target.save()

	floor_name_map = {}
	if cint(copy_floors_and_tables):
		for source_floor in frappe.get_list("Floor", filters={"outlet": source_outlet}, fields=["name", "floor_name"]):
			new_name = f"{target_outlet}-{source_floor.floor_name}"
			if not frappe.db.exists("Floor", new_name):
				new_floor = frappe.get_doc({"doctype": "Floor", "outlet": target_outlet, "floor_name": source_floor.floor_name})
				new_floor.insert()
				result["floors"].append(new_floor.name)
			floor_name_map[source_floor.name] = new_name

		for source_table in frappe.get_list(
			"Restaurant Table", filters={"outlet": source_outlet}, fields=["name", "floor", "label", "seat_count"]
		):
			new_floor_name = floor_name_map.get(source_table.floor)
			if not new_floor_name:
				continue
			new_table_name = f"{target_outlet}-{source_table.label}"
			if frappe.db.exists("Restaurant Table", new_table_name):
				continue
			new_table = frappe.get_doc(
				{
					"doctype": "Restaurant Table",
					"outlet": target_outlet,
					"floor": new_floor_name,
					"label": source_table.label,
					"seat_count": source_table.seat_count,
				}
			)
			new_table.insert()
			result["tables"].append(new_table.name)

	if cint(copy_stations_and_printers):
		printer_name_map = {}
		for source_printer in frappe.get_list(
			"Printer", filters={"outlet": source_outlet}, fields=["name", "printer_name", "kind", "paper_size"]
		):
			new_name = f"{target_outlet}-{source_printer.printer_name}"
			if not frappe.db.exists("Printer", new_name):
				new_printer = frappe.get_doc(
					{
						"doctype": "Printer",
						"outlet": target_outlet,
						"printer_name": source_printer.printer_name,
						"kind": source_printer.kind,
						"paper_size": source_printer.paper_size,
						# `address` deliberately not copied — a physical IP is specific to the
						# source outlet's own network, never a sensible template value.
					}
				)
				new_printer.insert()
				result["printers"].append(new_printer.name)
			printer_name_map[source_printer.name] = new_name

		for source_station in frappe.get_list(
			"Station", filters={"outlet": source_outlet}, fields=["name", "station_name", "printer", "sla_seconds"]
		):
			new_name = f"{target_outlet}-{source_station.station_name}"
			if frappe.db.exists("Station", new_name):
				continue
			new_station = frappe.get_doc(
				{
					"doctype": "Station",
					"outlet": target_outlet,
					"station_name": source_station.station_name,
					"printer": printer_name_map.get(source_station.printer),
					"sla_seconds": source_station.sla_seconds,
				}
			)
			new_station.insert()
			result["stations"].append(new_station.name)

	frappe.db.commit()
	return result

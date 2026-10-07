# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Tickets P1-26 (incremental, paged catalogue sync) and P3-03 (downstream master-data pull with
# change cursor): one whitelisted endpoint the Hub's sync-worker calls every 30-60s and on
# demand, returning everything that changed since the cursor it was given last time. Master data
# is only ever written here, in Frappe (CLAUDE.md rule 4) — the Hub applies what this returns,
# never the other way round.
#
# Cursor design: a single ISO datetime, advanced to the newest `modified` timestamp actually
# returned in a page. Every relevant doctype is queried by `modified > cursor`, the results are
# merged into one list and sorted by (modified, doctype, name) before the page-size cutoff is
# applied, so one cursor advances correctly across every doctype at once, not one per type.
# Known limitation, not silently glossed over: this trusts `modified`'s microsecond precision to
# order concurrent writes; a genuine tie at the exact same microsecond across two rows at a page
# boundary could in theory delay one row to the next cycle — acceptable for M1's scale, revisit
# with a real append-only change log if that ever matters at higher write volume.
#
# Deletion is only ever detected via `disabled` flags (Item) or `revoked_at` (Device) — an
# outright deleted Frappe row leaves no `modified` timestamp to poll for, so real delete-tracking
# (a tombstone log) is a known gap, not attempted here.

import datetime
import json
import types
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import cint, get_datetime, flt

from restaurant_pos.restaurant_pos.api.gstin import validate_gstin
from restaurant_pos.restaurant_pos.api.access_rights import floor_permission_rows
from restaurant_pos.restaurant_pos.api.scope import require_hub_service, require_outlet


def _hub_guard(outlet=None):
	"""Ticket P8.5-13: only the Hub's service account may call the sync methods (a Back Office user - even a
	System Manager - must not post invoices or read every outlet's staff hashes through them); and, when the
	call names an outlet, the account must be allowed that outlet (so a per-outlet Hub credential, P1-14,
	can be given exactly one)."""
	require_hub_service()
	if outlet:
		require_outlet(outlet, "read")


def _hub_dt(value):
	"""Hub timestamps are UTC ISO-8601 (...Z). ERPNext stores Datetime fields NAIVE, in the site's own
	timezone, so the value has to be converted - not just stripped of its tzinfo (which stored the UTC
	clock reading as if it were local time: 5 h 30 min early on an India site, found while building
	ticket P3-33 and fixed for every Hub timestamp here)."""
	dt = get_datetime(value)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=datetime.timezone.utc)
	return dt.astimezone(ZoneInfo(frappe.utils.get_system_timezone())).replace(tzinfo=None)


EPOCH = "1970-01-01 00:00:00.000000"
PAGE_SIZE = 200
# Half a paisa: absorbs pure floating-point noise from the paise<->rupee conversion, nothing
# more. Validation & Conventions §2 is explicit that the two must never differ "by even 1
# paisa" — so this must stay well under 0.01, never be widened to "tolerate" a real 1-paisa gap.
TOLERANCE_RUPEES = 0.005


def _rows(doctype, filters, fields, since):
	scoped = dict(filters)
	scoped["modified"] = [">", since]
	rows = frappe.get_all(
		doctype,
		filters=scoped,
		fields=list(fields) + ["modified", "name"],
		order_by="modified asc",
		limit_page_length=0,
	)
	for row in rows:
		row["_doctype"] = doctype
	return rows


def _merge_and_page(all_rows, page_size):
	all_rows.sort(key=lambda r: (r["modified"], r["_doctype"], r["name"]))
	page = all_rows[:page_size]
	truncated = len(all_rows) > page_size
	new_cursor = str(page[-1]["modified"]) if page else None
	return page, truncated, new_cursor


def _item_price(item_code, price_list):
	return frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": price_list}, "price_list_rate")


def _resolve_item_tax(item_code):
	"""Ticket P4-3x (per-item GST via ERPNext's native Item Tax Template mechanism, replacing the
	outlet-wide flat rate): resolves the item's own assigned template (Item.taxes, first row with
	no tax_category override — MVP scope is intra-state CGST+SGST only, per the user's own
	explicit choice, so date-range/tax-category variation on the same item is out of scope) and
	sums that template's own tax rows into one effective GST %. Returns (None, None) when no
	template is assigned yet — the Hub falls back to the outlet's own flat gst_rate_pct in that
	case, exactly as it always has, so an item nobody's gotten to yet doesn't silently charge 0%."""
	# "no tax category" is an empty string when a row is saved from Desk but NULL when it is inserted by code or imported: match both
	# (an exact-"" filter silently lost the slab of every programmatically created row and billed the outlet's flat rate instead)
	row = frappe.db.get_value(
		"Item Tax", {"parent": item_code, "parenttype": "Item", "tax_category": ["is", "not set"]}, "item_tax_template"
	)
	if not row:
		return None, None
	template = frappe.get_doc("Item Tax Template", row)
	rate_pct = sum(flt(t.tax_rate) for t in template.taxes if not t.not_applicable)
	return row, rate_pct


def _modifier_description(line):
	mods = line.get("modifiers") or []
	if not mods:
		return {}
	item_name = frappe.db.get_value("Item", line["item_code"], "item_name") or line["item_code"]
	return {"description": f"{item_name} ({', '.join(mods)})"}


def _item_tax_rate_json(item_tax_template):
	"""Real bug found live: setting Sales Invoice Item's `item_tax_template` alone does NOT make
	ERPNext's tax engine actually use it — `calculate_taxes_and_totals` reads the separate
	`item_tax_rate` field (a JSON `{account: rate}` map), which Desk's own client-side JS normally
	populates when a template is picked in the UI; nothing does that server-side on a plain
	`doc.insert()`. Building it explicitly here is what actually makes ERPNext apply the item's own
	rate rather than silently falling back to the invoice header's flat template."""
	if not item_tax_template:
		return None
	template = frappe.get_doc("Item Tax Template", item_tax_template)
	return json.dumps({t.tax_type: flt(t.tax_rate) for t in template.taxes if not t.not_applicable})


# Ticket P3-39: widths an item image may be served at (small list thumbnail, normal, large). Anything else is refused, so a
# caller cannot make ERPNext resize the same picture a thousand different ways.
IMAGE_WIDTHS = (160, 320, 640)


def _image_version(item_code, image):
	"""A short, stable id for THIS picture of this item: it changes when the picture (or its file) changes, so the Hub and every
	device can cache the resized copies forever and re-download only when it moves."""
	if not image:
		return None
	import hashlib

	modified = frappe.db.get_value("File", {"file_url": image}, "modified")
	return hashlib.sha1(f"{item_code}|{image}|{modified}".encode()).hexdigest()[:12]


def _station_for_outlet(item_station, outlet):
	"""Ticket P3-40: an Item links to ONE Station (a Station belongs to one outlet, e.g. "KOR-Tandoor"), but a menu is shared by every
	outlet of a tenant. Each outlet keeps its own stations with the same NAMES ("Tandoor", "Beverages"), so the station a given
	outlet's Hub must use is the one in THAT outlet with the item's station name. For the outlet the item points at, that is the
	item's own station; for another outlet it is its namesake there; if that outlet has no such station the item has none (and the
	Hub leaves it off that outlet's menu - the tenant check reports it)."""
	if not item_station or not outlet:
		return item_station
	station = frappe.db.get_value("Station", item_station, ["outlet", "station_name"], as_dict=True)
	if not station:
		return None
	if station.outlet == outlet:
		return item_station
	return frappe.db.get_value("Station", {"outlet": outlet, "station_name": station.station_name}, "name")


def _menu_item_payload(item_code, price_list, outlet=None):
	item = frappe.db.get_value(
		"Item",
		item_code,
		["item_code", "item_name", "item_group", "restaurant_station", "dietary_type", "disabled", "is_sales_item", "gst_hsn_code", "image"],
		as_dict=True,
	)
	if not item:
		return None
	rate = _item_price(item_code, price_list) or 0
	item_tax_template, gst_rate_pct = _resolve_item_tax(item_code)
	return {
		"item_code": item.item_code,
		"name": item.item_name,
		"category": item.item_group,
		"station": _station_for_outlet(item.restaurant_station, outlet),
		"dietary_type": item.dietary_type,
		# Money crosses to the Hub as integer paise (CLAUDE.md rule 10) — the one conversion
		# point between ERPNext's decimal Currency fieldtype and the Hub's own money type.
		"price_paise": round(float(rate) * 100),
		"removed": bool(item.disabled) or not item.is_sales_item,
		"item_tax_template": item_tax_template,
		"gst_rate_pct": gst_rate_pct,
		"hsn_code": item.get("gst_hsn_code"),
		# Ticket P3-39: null when the item has no picture; otherwise the picture's version (see _image_version). The picture
		# itself is fetched on demand by get_item_image, never carried in a pull.
		"image_version": _image_version(item.item_code, item.get("image")),
		# Ticket P4-33: which modifier groups this item offers, in the order Desk lists them.
		"modifier_groups": frappe.get_all(
			"Item Modifier Group", filters={"parent": item_code, "parenttype": "Item"}, order_by="idx asc", pluck="modifier_group"
		),
	}


@frappe.whitelist()
def get_item_image(outlet: str, item_code: str, width: int = 320):
	"""Ticket P3-39: an item's picture, resized to `width` and returned as WebP, for the Hub to cache. Hub Service only, scoped to
	an outlet it may read. The Hub fetches each (item, version, width) ONCE; devices then get it from the Hub (and Cloudflare's
	cache in front of it), never from here."""
	_hub_guard(outlet)
	width = cint(width)
	if width not in IMAGE_WIDTHS:
		frappe.throw(f"Image width must be one of {', '.join(map(str, IMAGE_WIDTHS))}.", frappe.ValidationError)
	image = frappe.db.get_value("Item", item_code, "image")
	if not image:
		frappe.throw("This item has no picture.", frappe.DoesNotExistError)
	import io

	from PIL import Image

	file_doc = frappe.get_doc("File", {"file_url": image})
	try:
		picture = Image.open(io.BytesIO(file_doc.get_content()))
		picture = picture.convert("RGBA" if picture.mode in ("RGBA", "LA", "P") else "RGB")
	except Exception:  # noqa: BLE001
		frappe.throw("This item's picture could not be read.", frappe.ValidationError)
	picture.thumbnail((width, width))  # keeps the aspect ratio, never enlarges
	out = io.BytesIO()
	picture.save(out, format="WEBP", quality=80, method=6)
	frappe.local.response.filename = f"{frappe.scrub(item_code)}-{width}.webp"
	frappe.local.response.filecontent = out.getvalue()
	frappe.local.response.type = "download"


@frappe.whitelist()
def get_master_version(outlet: str) -> dict:
	"""Ticket P3-32: the cheap question a Hub asks on its safety-net poll - "has anything changed since I last pulled?".
	The answer is a version string built from a per-outlet counter (bumped by the change-hint hooks) and a cached
	latest-modified stamp, so an idle outlet's poll is a cache read, not a database query. If the string equals the one
	the Hub holds, the Hub skips the (expensive) pull_changes."""
	_hub_guard(outlet)
	if not frappe.db.exists("Outlet", outlet):
		frappe.throw("Unknown outlet.", frappe.DoesNotExistError)
	from restaurant_pos.restaurant_pos.api.hub_push import master_version

	return {"version": master_version(outlet)}


@frappe.whitelist()
def pull_changes(outlet: str, cursor: str = None, ack_events: list | str = None) -> dict:
	"""Called by the Hub's sync-worker, authenticated the same way pairing.py is (a dedicated
	Hub API key/secret) — never by a browser. `outlet` always comes from the Hub's own paired
	identity in practice (the Hub only ever asks for its own outlet); this endpoint still checks
	the outlet exists rather than trusting the string blindly."""
	_hub_guard(outlet)
	if not frappe.db.exists("Outlet", outlet):
		frappe.throw("Unknown outlet.", frappe.DoesNotExistError)

	# Ticket P3-04: events the Hub says it has applied (received by pull earlier) are marked Delivered.
	if ack_events:
		from restaurant_pos.restaurant_pos.api.hub_push import acknowledge_events

		acknowledge_events(outlet, frappe.parse_json(ack_events) if isinstance(ack_events, str) else ack_events)

	# Ticket P3-32: the version is read BEFORE collecting, so a change made while this page is being built can only make
	# the Hub's next check differ (one extra pull), never hide the change.
	from restaurant_pos.restaurant_pos.api.hub_push import master_version as _master_version

	version_at_start = _master_version(outlet)
	since = get_datetime(cursor) if cursor else get_datetime(EPOCH)
	outlet_doc = frappe.get_doc("Outlet", outlet)

	collected = []
	collected += _rows("Floor", {"outlet": outlet}, ["outlet", "floor_name"], since)
	collected += _rows("Restaurant Table", {"outlet": outlet}, ["outlet", "floor", "label", "seat_count"], since)
	collected += _rows("Station", {"outlet": outlet}, ["outlet", "station_name", "sla_seconds", "printer"], since)
	# Ticket P2-16/17: printers sync down like any other outlet master data (CLAUDE.md rule 4) —
	# the Hub's print worker never reads Frappe, only its own copy of this.
	collected += _rows("Printer", {"outlet": outlet}, ["printer_name", "kind", "address", "paper_size"], since)
	collected += _rows("Device", {"outlet": outlet, "revoked_at": ["is", "set"]}, ["outlet", "credential_id", "revoked_at"], since)

	# Staff: only rows with an assignment at THIS outlet matter; a row whose assignment was just
	# removed still shows up here (removing a child row bumps the parent's `modified`) so the
	# Hub can drop someone who's no longer assigned, not just skip an unrelated update.
	staff_rows = _rows("Staff PIN", {}, ["full_name", "staff_number", "active", "pin_reset_requested_at"], since)
	staff_payloads = []
	for row in staff_rows:
		assignment = frappe.db.get_value(
			"Staff Outlet Assignment", {"parent": row["name"], "outlet": outlet}, "role"
		)
		staff_payloads.append(
			{
				"_doctype": "Staff PIN",
				"modified": row["modified"],
				"name": row["name"],
				"full_name": row["full_name"],
				"staff_number": row["staff_number"],
				"active_here": bool(assignment) and bool(row["active"]),
				"role": assignment,
				"pin_hash": frappe.utils.password.get_decrypted_password("Staff PIN", row["name"], fieldname="pin_hash", raise_exception=False)
				if assignment
				else None,
				# Ticket P4-34: a plain timestamp, never a PIN or a hash-in-progress — the one thing
				# this flow needs flowing DOWN; the resulting new pin_hash flows back UP via
				# push_pin_reset, the only field in this whole sync that legitimately goes both ways.
				"pin_reset_requested_at": str(row["pin_reset_requested_at"]) if row["pin_reset_requested_at"] else None,
			}
		)
	collected += staff_payloads

	# Ticket P4-33: modifier groups are plain master data (not outlet-scoped, like Item) — a group
	# edit bumps its own `modified`, so it rides the same cursor as every other doctype here.
	collected += _rows("Modifier Group", {}, ["group_name", "is_required", "allow_multiple"], since)

	# Menu: driven off Item Price changes for this outlet's own price list (a price-only change
	# never touches the Item doc's own `modified`) UNION Item changes for items already on this
	# price list (a rename/disable never touches Item Price's `modified`) — either alone would
	# miss the other's edit.
	if outlet_doc.price_list:
		price_rows = _rows("Item Price", {"price_list": outlet_doc.price_list}, ["item_code"], since)
		existing_on_list = frappe.get_all("Item Price", filters={"price_list": outlet_doc.price_list}, pluck="item_code")
		item_rows = _rows(
			"Item",
			{"is_sales_item": 1, "item_code": ["in", existing_on_list or [""]]},
			["item_code"],
			since,
		)
		collected += price_rows
		collected += item_rows

	page, truncated, new_cursor_from_page = _merge_and_page(collected, PAGE_SIZE)
	new_cursor = new_cursor_from_page or cursor or EPOCH

	floors, tables, stations, devices_revoked, staff = [], [], [], [], []
	modifier_groups = []
	printers = []
	seen_menu_codes = set()
	for row in page:
		dtype = row["_doctype"]
		if dtype == "Floor":
			floors.append({"id": row["name"], "name": row["floor_name"]})
		elif dtype == "Restaurant Table":
			tables.append({"id": row["name"], "floor": row["floor"], "label": row["label"], "seat_count": row["seat_count"]})
		elif dtype == "Station":
			stations.append(
				{
					"id": row["name"],
					"name": row["station_name"],
					"sla_seconds": row.get("sla_seconds"),
					"printer_id": row.get("printer") or None,
				}
			)
		elif dtype == "Printer":
			printers.append(
				{
					"id": row["name"],
					"name": row["printer_name"],
					"kind": row["kind"],
					"address": row.get("address") or None,
					"paper_size": row.get("paper_size") or "80mm",
				}
			)
		elif dtype == "Device":
			devices_revoked.append({"credential_id": row["credential_id"]})
		elif dtype == "Staff PIN":
			staff.append(
				{
					"id": row["name"],
					"name": row["full_name"],
					"staff_number": row["staff_number"],
					"role": row["role"],
					"pin_hash": row["pin_hash"],
					"active": row["active_here"],
					"pin_reset_requested_at": row["pin_reset_requested_at"],
				}
			)
		elif dtype == "Modifier Group":
			group_doc = frappe.get_doc("Modifier Group", row["name"])
			modifier_groups.append(
				{
					"id": group_doc.name,
					"name": group_doc.group_name,
					"required": bool(group_doc.is_required),
					"multiple": bool(group_doc.allow_multiple),
					# Money crosses as integer paise (CLAUDE.md rule 10), same as item prices. The
					# option's own row name is its stable id: a chosen option on an order line is
					# validated against this exact id Hub-side, never against a client-sent price.
					"options": [
						{
							"id": opt.name,
							"name": opt.option_name,
							"price_delta_paise": round(flt(opt.price_delta) * 100),
							"disabled": bool(opt.disabled),
						}
						for opt in group_doc.options
					],
				}
			)
		elif dtype in ("Item", "Item Price"):
			seen_menu_codes.add(row["item_code"])

	menu_items = []
	for code in seen_menu_codes:
		payload = _menu_item_payload(code, outlet_doc.price_list, outlet)
		if payload:
			menu_items.append(payload)

	from restaurant_pos.restaurant_pos.api.hub_push import pending_events_for_hub, push_secrets_for_hub

	return {
		"cursor": new_cursor,
		"truncated": truncated,
		# Ticket P3-27: what the Hub verifies ERPNext's signed pushes with (this method is Hub Service only).
		"push_secrets": push_secrets_for_hub(outlet),
		# Ticket P3-04: latency-sensitive events whose push never got through - applied here, idempotently by id.
		"events": pending_events_for_hub(outlet),
		# Ticket P3-32: the version this pull brings the Hub up to, so its next poll can skip an unchanged outlet.
		"master_version": version_at_start,
		"floors": floors,
		"tables": tables,
		"stations": stations,
		"printers": printers,
		"menu_items": menu_items,
		"modifier_groups": modifier_groups,
		"staff": staff,
		"revoked_devices": devices_revoked,
		# Ticket P4-35: what each staff role may do on the floor (every explicit row; the Hub keeps the
		# built-in defaults for any role/action with no row).
		"floor_permissions": [{"staff_role": r.staff_role, "action": r.action, "allowed": bool(r.allowed)} for r in floor_permission_rows()],
		"billing_settings": {
			"tax_rate_pct": outlet_doc.gst_rate_pct or 0,
			"tax_inclusive": bool(outlet_doc.gst_inclusive),
			"service_charge_pct": outlet_doc.service_charge_pct or 0,
			"service_charge_taxable": bool(outlet_doc.service_charge_gst_applicable),
			"round_to_nearest_paise": 100,
			"invoice_series_prefix": outlet_doc.invoice_series_prefix or "",
			# Ticket P4-14: the outlet's static UPI details, for the Edge Hub's offline
			# static-QR-and-manual-confirm fallback (Tech Architecture §10). Reuses this same
			# synced bucket rather than a new sync category — this is outlet-level config synced
			# down the identical way tax/service-charge settings already are.
			"upi_vpa": outlet_doc.upi_vpa or None,
			"upi_payee_name": outlet_doc.upi_payee_name or outlet_doc.outlet_name,
			# Ticket P2-18: what a printed receipt needs in its header, and which printer it (and
			# the cash-drawer kick) goes to — same synced bucket, same reasoning as the UPI fields.
			"outlet_name": outlet_doc.outlet_name,
			"outlet_city": outlet_doc.city or None,
			"gstin": outlet_doc.gstin or None,
			"fssai_license_number": outlet_doc.fssai_license_number or None,
			"receipt_printer_id": outlet_doc.receipt_printer or None,
			"guest_order_approval": bool(outlet_doc.guest_order_approval),  # ticket P2-31
		},
	}


def _get_or_create_tax_template(company):
	from restaurant_pos.restaurant_pos.setup.billing_setup import _ensure_tax_template

	return _ensure_tax_template(company)


def _invoice_posting_time(payload, key: str = "paid_at") -> str:
	if not payload.get(key):
		return frappe.utils.nowtime()
	paid_local = _hub_dt(payload[key])
	if str(paid_local.date()) == str(payload["business_date"]):
		return paid_local.strftime("%H:%M:%S")
	return "23:59:59"



def _hub_validate_pos_opening_entry(self):
	"""Ticket P3-33. ERPNext's own validate_pos_opening_entry insists the open POS Opening Entry
	started on the wall-clock `today()`. A shift that runs past midnight (or a bill replayed after an
	outage) therefore gets "Outdated POS Opening Entry" - even though the Hub has already fixed its
	business date at shift open. For invoices the Hub created, the same two safety checks are kept
	(exactly one open entry for the profile) but the date test compares the entry with the invoice's
	own posting date, which IS the Hub's business date. Installed per document in push_pos_invoice
	(instance attribute), never globally, so hand-made invoices keep ERPNext's own rule."""
	entries = frappe.get_all(
		"POS Opening Entry",
		fields=["name", "period_start_date"],
		filters={"pos_profile": self.pos_profile, "status": "Open"},
		order_by="period_start_date desc",
	)
	if not entries:
		frappe.throw(title="POS Opening Entry Missing", msg=f"No open POS Opening Entry found for POS Profile {frappe.bold(self.pos_profile)}.")
	if len(entries) > 1:
		frappe.throw(
			title="Multiple POS Opening Entry",
			msg=f"POS Profile - {self.pos_profile} has multiple open POS Opening Entries. Please close or cancel the existing entries before proceeding.",
		)
	if frappe.utils.get_date_str(entries[0].get("period_start_date")) != frappe.utils.get_date_str(self.posting_date):
		frappe.throw(
			title="Outdated POS Opening Entry",
			msg=f"POS Opening Entry - {entries[0].get('name')} started on a different business date than this invoice ({self.posting_date}). Close that shift and open a new one.",
		)



@frappe.whitelist()
def push_pos_invoice(payload) -> dict:
	"""Tickets P3-02 (idempotent by Hub UUID) and P3-07 (POS Invoice sync), called by the Hub's
	sync-worker once a bill is paid. Frappe always recomputes totals itself (never trusts the
	Hub's number blindly) — a mismatch is set aside in Sync Dead Letter with the reason, never
	silently corrected, and the rest of the Hub's queue keeps flowing regardless (that's the
	Hub's own retry loop's job, not this method's).

	Targets Sales Invoice, not the POS Invoice doctype ticket P3-07's title names — this bench's
	own POS Settings.invoice_type is "Sales Invoice" (checked directly, not assumed), which is
	exactly the open question ticket P3-06 asks to confirm before locking the sync schema. A
	Sales Invoice with is_pos=1 is what real POS activity looks like under that setting."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("Sales Invoice", {"hub_uuid": payload["hub_uuid"]}, ["name", "docstatus"], as_dict=True)
	if existing:
		return {"status": "ok", "pos_invoice": existing.name, "already_existed": True}

	# Ticket P4-19: the Hub already validated this synchronously before ever recording the
	# payment (services/hub/src/billing/gstin.ts) — this is defense in depth only, same "never
	# trust the Hub blindly" philosophy as the total-mismatch check below, not the primary gate.
	customer_gstin = payload.get("customer_gstin")
	if customer_gstin:
		try:
			validate_gstin(customer_gstin)
		except frappe.ValidationError as err:
			frappe.get_doc(
				{
					"doctype": "Sync Dead Letter",
					"outlet": payload["outlet"],
					"hub_uuid": payload["hub_uuid"],
					"target_doctype": "Sales Invoice",
					"reason": f"Invalid customer GSTIN pushed from the Hub: {err}",
					"payload_json": json.dumps(payload),
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
			return {"status": "mismatch", "message": f"Invalid customer GSTIN: {err} — sent to Sync Dead Letter, not posted."}

	outlet_doc = frappe.get_doc("Outlet", payload["outlet"])
	tax_template = _get_or_create_tax_template(outlet_doc.company)

	# Ticket P4-18: same boundary-normalization convention as apply-changes.ts's ROLE_MAP / KOT's
	# course_map — the Hub's lowercase values on one side, Frappe's own Title Case Select/Mode of
	# Payment records on the other. Only "cash" is ever produced today (P4-11); "upi"/"card" are
	# real keys ready for whichever gateway P4-12/13 lands, not placeholders.
	mode_of_payment_map = {"cash": "Cash", "upi": "UPI", "card": "Card"}
	verification_status_map = {
		"auto-confirmed": "Auto-confirmed",
		"pending-verification": "Pending Verification",
		"manually-verified": "Manually Verified",
		"mismatch-flagged": "Mismatch Flagged",
	}
	hub_device_name = None
	if payload.get("device_credential_id"):
		hub_device_name = frappe.db.get_value("Device", {"credential_id": payload["device_credential_id"]}, "name")

	doc = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": "Walk-in Customer",
			"company": outlet_doc.company,
			# Ticket raised by the user directly: without this, every outlet under one Company is
			# indistinguishable in P&L/GL — Branch is now enabled as a real Accounting Dimension
			# (see setup/accounting_setup.py), so this one field is what makes every standard
			# ERPNext financial report filterable/groupable by outlet.
			"branch": outlet_doc.branch,
			"is_pos": 1,
			# Ticket P4-21/P3-08's own real dependency, found while building shift-close: ERPNext's
			# own POS Closing Entry consolidation (get_invoices() in pos_closing_entry.py) filters
			# strictly on is_created_using_pos=1 — without this, every invoice this Hub has ever
			# pushed would silently never be picked up by a real closing entry, no error, just an
			# empty consolidation every time.
			"is_created_using_pos": 1,
			"pos_profile": outlet_doc.pos_profile,
			"update_stock": 1,
			"set_warehouse": outlet_doc.warehouse,
			"selling_price_list": outlet_doc.price_list,
			"posting_date": payload["business_date"],
			# Ticket P3-33: ERPNext's POS closing consolidation picks invoices by
			# timestamp(posting_date, posting_time) inside the shift's [start, end]. The Hub's business
			# date can be earlier than the calendar date a bill was actually paid on (a shift past
			# midnight), so the time is the real local time when the dates agree and 23:59:59 of the
			# business day when they do not - always inside the shift window, never "sync time".
			"set_posting_time": 1,
			"posting_time": _invoice_posting_time(payload),
			"taxes_and_charges": tax_template,
			"hub_uuid": payload["hub_uuid"],
			"hub_invoice_number": payload.get("hub_invoice_number"),
			# Ticket P4-18: "every payment in ERPNext is traceable to its device and staff member
			# and carries a verification status."
			"verification_status": verification_status_map.get(payload.get("verification_status"), "Auto-confirmed"),
			"gateway_transaction_id": payload.get("gateway_transaction_id"),
			"processed_by": payload.get("staff_id"),
			"hub_device": hub_device_name,
			# Ticket P4-10: a split bill's sub-bills each push their own Sales Invoice, all sharing
			# this same order_id — same "Data, not Link" convention as KOT/Void Log's own field.
			"restaurant_order": payload.get("order_id"),
			# Ticket P4-19: optional B2B customer details. `tax_id` is Sales Invoice's own native
			# GSTIN-carrying field (confirmed on the live bench — no custom field needed for it);
			# `customer` stays "Walk-in Customer" (see this function's own docstring convention),
			# so `customer_name` would just read "Walk-in Customer" too — customer_business_name
			# is the one place the actual B2B business name is recorded.
			"tax_id": customer_gstin or None,
			"customer_business_name": payload.get("customer_business_name") or None,
			"items": [
				{
					"item_code": line["item_code"],
					"qty": line["qty"],
					"rate": line["unit_price_paise"] / 100,
					"warehouse": outlet_doc.warehouse,
					# Ticket P4-3x (per-item GST): set only when the Hub resolved one at sync time —
					# ERPNext's own tax engine then derives this line's CGST/SGST the standard way,
					# same basis the Hub's own bill-math.ts used, rather than the header template
					# below applying uniformly. Omitted (None) falls back to the header template,
					# same as every item before this ticket. Both fields are needed — see
					# _item_tax_rate_json's own docstring for why item_tax_template alone isn't enough.
					"item_tax_template": line.get("item_tax_template"),
					"item_tax_rate": _item_tax_rate_json(line.get("item_tax_template")),
					# Ticket P4-33: the chosen modifiers' price changes are already inside `rate` above
					# (the Hub folds them into the line's unit price, so every total reads one number);
					# the description just records what was actually served on the invoice itself.
					**_modifier_description(line),
				}
				for line in payload["lines"]
			],
			"payments": [
				{
					"mode_of_payment": mode_of_payment_map.get(payload.get("mode"), "Cash"),
					"amount": payload["expected_total_paise"] / 100,
				}
			],
		}
	)

	doc.validate_pos_opening_entry = types.MethodType(_hub_validate_pos_opening_entry, doc)  # ticket P3-33

	try:
		doc.insert(ignore_permissions=True)
	except Exception as err:
		# A concurrent push with the same hub_uuid may have beaten us to the unique-constraint
		# insert (ticket P3-02's own "concurrent inserts of one UUID -> one already exists" case)
		# — re-check rather than assume every insert failure means that, though.
		raced = frappe.db.get_value("Sales Invoice", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "pos_invoice": raced, "already_existed": True}
		raise err

	expected_rupees = payload["expected_total_paise"] / 100
	actual_rupees = doc.rounded_total or doc.grand_total
	if abs(actual_rupees - expected_rupees) > TOLERANCE_RUPEES:
		frappe.delete_doc("Sales Invoice", doc.name, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Sync Dead Letter",
				"outlet": payload["outlet"],
				"hub_uuid": payload["hub_uuid"],
				"target_doctype": "Sales Invoice",
				"reason": f"Total mismatch: Hub expected {expected_rupees}, ERPNext recomputed {actual_rupees}.",
				"payload_json": json.dumps(payload),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return {
			"status": "mismatch",
			"message": f"Total mismatch (Hub {expected_rupees} vs ERPNext {actual_rupees}) — sent to Sync Dead Letter, not posted.",
		}

	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "pos_invoice": doc.name, "already_existed": False, "grand_total": doc.grand_total}


@frappe.whitelist()
def push_void_log(payload) -> dict:
	"""Ticket P2-04's audit trail synced up (CLAUDE.md rule 4: transactions flow up, created only
	on the Hub). Idempotent by hub_uuid, same pattern as push_pos_invoice — a retried push finds
	the existing row rather than creating a second one.

	`restaurant_order` is stored as the Hub's own order UUID string (Data, not a Link) since
	orders themselves are never mirrored into Frappe — only this audit record and the eventual
	Sales Invoice (P3-07) cross up. `requested_by`/`approved_by` are the Hub's staff ids, which
	are themselves Staff PIN docnames (P1-26/P3-03 syncs `id: row["name"]"), so no extra lookup is
	needed there. `device` does need one: the Hub only knows its own credential_id, not the
	Device doctype's docname."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("Void Log", {"hub_uuid": payload["hub_uuid"]}, "name")
	if existing:
		return {"status": "ok", "name": existing, "already_existed": True}

	device_name = None
	if payload.get("device_credential_id"):
		device_name = frappe.db.get_value("Device", {"credential_id": payload["device_credential_id"]}, "name")

	doc = frappe.get_doc(
		{
			"doctype": "Void Log",
			"outlet": payload["outlet"],
			"restaurant_order": payload["order_id"],
			"line_item_id": payload["line_id"],
			"kind": "Void",
			"amount": payload["amount_paise"],
			"reason": payload["reason"],
			"requested_by": payload["requested_by_staff_id"],
			"approved_by": payload["approved_by_staff_id"],
			"device": device_name,
			# The Hub sends its own UTC ISO 8601 timestamp (...T...Z). get_datetime() alone still
			# leaves it timezone-aware (+00:00), which MySQL's Datetime column rejects outright
			# (every other Datetime value in this codebase — Frappe's own `modified` — is already
			# naive, since it never crosses a UTC-vs-local boundary). Stripped naive here on the
			# assumption the Hub and this site agree on UTC, same as pull_changes' own cursor
			# handling never has to consider a timezone offset at all.
			"occurred_at": _hub_dt(payload["occurred_at"]),
			"hub_uuid": payload["hub_uuid"],
		}
	)

	try:
		doc.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("Void Log", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced, "already_existed": True}
		raise err

	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "name": doc.name, "already_existed": False}


@frappe.whitelist()
def push_refund(payload) -> dict:
	"""Ticket P4-16 (cash/void half only — UPI gateway refunds are P4-12/13's job, not this).
	Idempotent by hub_uuid, same pattern as every other push. Unlike a void of an unpaid line
	(push_void_log), this reverses a bill that's ALREADY a real, submitted, stock-affecting Sales
	Invoice — Tech Architecture §8's "a void creates a linked reversing entry; numbers are never
	reused or deleted" is built here as a real ERPNext Credit Note (`is_return=1`,
	`return_against` pointing at the original), never a cancel-and-resubmit (which ERPNext can
	refuse once a shift close has consolidated the invoice — see this ticket's own STATUS_NOTES
	for why a Credit Note was chosen instead, a real judgment call flagged and confirmed with the
	user before building).

	`update_stock=0` on the credit note is deliberate, not an oversight: this is a restaurant,
	the "returned" item is food that's already been served/consumed, not physical stock coming
	back onto a shelf — reversing the original invoice's stock depletion here would be wrong.
	The credit note's single line reuses the original invoice's own first item_code (Sales
	Invoice requires a valid item_code on every line; the refund is a monetary reversal, not an
	item-level return, so which real item_code carries it is arbitrary) at `qty=-1` and a `rate`
	equal to the refund amount — letting the SAME tax template the original invoice already used
	compute its own proportional tax on this one line, exactly the same way every ordinary line
	already does, rather than this function trying to reverse-engineer bill-math.ts's own
	tax-inclusive/exclusive arithmetic a second time. This means the credit note's own grand
	total is not guaranteed to land on the refund amount to the exact paisa the way an original
	bill's total is (CLAUDE.md rule 10's "to the paisa" guarantee, enforced by push_pos_invoice's
	own mismatch dead-letter, doesn't have a stated equivalent for a refund anywhere in the
	docs) — a known, flagged simplification, not silently glossed over."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("Refund Log", {"hub_uuid": payload["hub_uuid"]}, ["name", "credit_note"], as_dict=True)
	if existing:
		return {"status": "ok", "name": existing.name, "credit_note": existing.credit_note, "already_existed": True}

	# A refund can only be pushed once the ORIGINAL payment has itself finished syncing and has a
	# real Sales Invoice name — push-refunds.ts's own retry loop is what waits for that, not this
	# function; if it somehow got called too early anyway, fail loudly rather than build a return
	# against a document that doesn't exist.
	original = frappe.get_doc("Sales Invoice", payload["sales_invoice"])

	device_name = None
	if payload.get("device_credential_id"):
		device_name = frappe.db.get_value("Device", {"credential_id": payload["device_credential_id"]}, "name")

	refund_amount_rupees = payload["amount_paise"] / 100
	original_mode_of_payment = original.payments[0].mode_of_payment if original.payments else "Cash"

	credit_note = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": original.customer,
			"company": original.company,
			"branch": original.branch,
			"is_pos": 1,
			"is_created_using_pos": 1,
			"pos_profile": original.pos_profile,
			"is_return": 1,
			"return_against": original.name,
			# Deliberately NOT 1 — see this function's own docstring on why a restaurant refund
			# never reverses stock (the food is already consumed).
			"update_stock": 0,
			"set_warehouse": original.set_warehouse,
			"selling_price_list": original.selling_price_list,
			# Ticket P3-33: the credit note belongs to the open shift's business day, not the sync-time date -
			# otherwise a refund made while a shift is running past midnight (or synced the next morning)
			# is refused by ERPNext's "outdated POS Opening Entry" rule.
			"posting_date": payload.get("business_date") or frappe.utils.nowdate(),
			"set_posting_time": 1,
			"posting_time": _invoice_posting_time(payload, "occurred_at") if payload.get("business_date") else frappe.utils.nowtime(),
			"taxes_and_charges": original.taxes_and_charges,
			"hub_uuid": payload["hub_uuid"],
			"restaurant_order": payload.get("order_id"),
			"items": [
				{
					"item_code": original.items[0].item_code,
					"qty": -1,
					"rate": refund_amount_rupees,
					"warehouse": original.set_warehouse,
				}
			],
			"payments": [
				{
					"mode_of_payment": original_mode_of_payment,
					"amount": -refund_amount_rupees,
				}
			],
		}
	)

	credit_note.validate_pos_opening_entry = types.MethodType(_hub_validate_pos_opening_entry, credit_note)  # ticket P3-33
	try:
		credit_note.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("Refund Log", {"hub_uuid": payload["hub_uuid"]}, ["name", "credit_note"], as_dict=True)
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced.name, "credit_note": raced.credit_note, "already_existed": True}
		raise err
	credit_note.submit()

	doc = frappe.get_doc(
		{
			"doctype": "Refund Log",
			"outlet": payload["outlet"],
			"restaurant_order": payload.get("order_id"),
			"sales_invoice": original.name,
			"credit_note": credit_note.name,
			"amount": payload["amount_paise"],
			"reason": payload["reason"],
			"requested_by": payload["requested_by_staff_id"],
			"approved_by": payload["approved_by_staff_id"],
			"device": device_name,
			"occurred_at": _hub_dt(payload["occurred_at"]),
			"hub_uuid": payload["hub_uuid"],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "name": doc.name, "credit_note": credit_note.name, "already_existed": False}


@frappe.whitelist()
def push_pin_reset(payload) -> dict:
	"""Ticket P4-34, option A: the Hub is the only thing that ever hashes a PIN (it holds
	HUB_PIN_PEPPER; Frappe never does) — this is the one place a resulting hash flows back UP,
	the mirror image of every other push_* here. Idempotent not by a hub_uuid field (Staff PIN
	has none, and adding one just for this felt like overkill for a single-field update) but by
	the request timestamp itself: if this staff member's CURRENT pin_reset_requested_at doesn't
	match what this push is responding to, it's either already been applied (the field is
	cleared below on success) or superseded by a newer request — either way, a no-op, not an
	error, matching every other push's "retried safely" guarantee."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	doc = frappe.get_doc("Staff PIN", payload["staff_id"])
	current = str(doc.pin_reset_requested_at) if doc.pin_reset_requested_at else None
	if current != payload["reset_requested_at"]:
		return {"status": "ok", "already_applied": True}

	doc.pin_hash = payload["new_pin_hash"]
	doc.pin_reset_requested_at = None
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"status": "ok", "already_applied": False}


@frappe.whitelist()
def push_availability_log(payload) -> dict:
	"""Ticket P2-12's audit trail synced up, same idempotent-by-hub_uuid pattern as
	push_void_log. `device` needs the same credential_id -> docname lookup; `changed_by` is
	already a Staff PIN docname (the Hub's staff id)."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("Availability Log", {"hub_uuid": payload["hub_uuid"]}, "name")
	if existing:
		return {"status": "ok", "name": existing, "already_existed": True}

	device_name = None
	if payload.get("device_credential_id"):
		device_name = frappe.db.get_value("Device", {"credential_id": payload["device_credential_id"]}, "name")

	doc = frappe.get_doc(
		{
			"doctype": "Availability Log",
			"outlet": payload["outlet"],
			"item_code": payload["item_code"],
			"available": 1 if payload["available"] else 0,
			"changed_by": payload["changed_by_staff_id"],
			"device": device_name,
			"occurred_at": _hub_dt(payload["occurred_at"]),
			"hub_uuid": payload["hub_uuid"],
		}
	)

	try:
		doc.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("Availability Log", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced, "already_existed": True}
		raise err

	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "name": doc.name, "already_existed": False}


@frappe.whitelist()
def push_shift_open(payload) -> dict:
	"""Ticket P4-20: a real shift-open, synced up the same idempotent-by-hub_uuid pattern as
	every other Hub push. `user` stays "Administrator" (the Hub's own shared service account,
	P1-14) — `opened_by` is the real Staff PIN who actually opened it on the Hub."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("POS Opening Entry", {"hub_uuid": payload["hub_uuid"]}, "name")
	if existing:
		return {"status": "ok", "name": existing, "already_existed": True}

	outlet_doc = frappe.get_doc("Outlet", payload["outlet"])
	doc = frappe.get_doc(
		{
			"doctype": "POS Opening Entry",
			"period_start_date": _hub_dt(payload["opened_at"]),
			# Ticket P3-33: the business date fixed on the Hub at shift open, not the sync-time wall clock.
			"posting_date": payload.get("business_date") or frappe.utils.nowdate(),
			"company": outlet_doc.company,
			"pos_profile": outlet_doc.pos_profile,
			# Known limitation (flagged on ticket P3-33): ERPNext lets one user hold only ONE open POS
			# Opening Entry across all POS Profiles, and the Hub's shared service user is Administrator, so
			# a second outlet opening a shift while the first has one open is refused ("Cashier is
			# currently assigned to another POS"). Multi-outlet needs one POS user per outlet (M2b).
			# `pos_user` is the hook for that; the default keeps today's single-outlet behaviour.
			"user": payload.get("pos_user") or "Administrator",
			"opened_by": payload["opened_by_staff_id"],
			"hub_uuid": payload["hub_uuid"],
			"balance_details": [{"mode_of_payment": "Cash", "opening_amount": payload["opening_cash_paise"] / 100}],
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("POS Opening Entry", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced, "already_existed": True}
		raise err

	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "name": doc.name, "already_existed": False}


@frappe.whitelist()
def push_shift_close(payload) -> dict:
	"""Ticket P4-21 (shift close) + P3-08 (POS Closing Entry consolidation). Idempotent by
	hub_uuid like every other push. Reuses ERPNext's own
	erpnext.accounts.doctype.pos_closing_entry.make_closing_entry_from_opening() to build the
	sales_invoices/payment_reconciliation/taxes tables rather than reimplementing that query —
	submitting the resulting doc is also what actually runs the real consolidation (its own
	on_submit() calls consolidate_pos_invoices()), so there is no separate "P3-08 step" to build,
	only this one submit.

	ERPNext's own builder always sets payment_reconciliation's opening_amount to 0 and never a
	closing_amount (its own UI leaves entering the counted cash to a human) — both are overwritten
	here from the Hub's own already-known values: the real opening amount from the linked POS
	Opening Entry, and the supervisor's actual counted cash from the Hub's shift-close payload."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	# Ticket P3-38: a Hub that says it can wait (accept_pending) hands the heavy part to a staggered, resumable background job
	# and is answered "pending" at once. A Hub that does not say so (an older one, N-1) gets the synchronous behaviour below.
	if payload.get("accept_pending"):
		from restaurant_pos.restaurant_pos.api.shift_close import submit_close

		return submit_close(payload)
	return _close_shift(payload)


def _close_shift(payload: dict) -> dict:
	"""Both steps of a shift close - the POS Closing Entry, then the stock entry - each IDEMPOTENT by its own hub_uuid, so
	this can be run again after any failure and does only what is left (ticket P3-38's "an interrupted job resumes without
	duplicates"). It used to return as soon as the closing entry existed, which meant a close whose stock entry failed was
	never completed on retry; the stock step now runs on every call and returns what exists once it has."""
	existing = frappe.db.get_value("POS Closing Entry", {"hub_uuid": payload["hub_uuid"]}, "name")
	if existing:
		stock_entry_name, missing_bom_items = _create_shift_stock_entry(payload)
		frappe.db.commit()
		return {"status": "ok", "name": existing, "already_existed": True, "stock_entry_name": stock_entry_name, "missing_bom_items": missing_bom_items}

	from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

	opening_entry = frappe.get_doc("POS Opening Entry", payload["pos_opening_entry"])
	closing_entry = make_closing_entry_from_opening(opening_entry)
	closing_entry.period_end_date = _hub_dt(payload["closed_at"])
	# (ERPNext itself forces a POS Closing Entry's posting date to the day it is created - its
	# set_posting_date_and_time() - so it cannot carry the business date; the invoices it consolidates do.)
	closing_entry.hub_uuid = payload["hub_uuid"]
	closing_entry.closed_by = payload["closed_by_staff_id"]

	real_opening = {d.mode_of_payment: d.opening_amount for d in opening_entry.balance_details}
	for row in closing_entry.payment_reconciliation:
		row.opening_amount = real_opening.get(row.mode_of_payment, 0)
		row.closing_amount = payload["counted_cash_paise"] / 100 if row.mode_of_payment == "Cash" else row.expected_amount
		row.difference = flt(row.closing_amount) - flt(row.opening_amount) - flt(row.expected_amount)

	try:
		closing_entry.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("POS Closing Entry", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced, "already_existed": True, "stock_entry_name": None, "missing_bom_items": []}
		raise err

	closing_entry.submit()
	frappe.db.commit()

	stock_entry_name, missing_bom_items = _create_shift_stock_entry(payload)
	frappe.db.commit()
	return {
		"status": "ok",
		"name": closing_entry.name,
		"already_existed": False,
		"stock_entry_name": stock_entry_name,
		"missing_bom_items": missing_bom_items,
	}


def _create_shift_stock_entry(payload) -> tuple:
	"""Ticket P3-12: "at shift close, create one Stock Entry computed from BOM x quantity sold;
	items without a BOM are reported, not silently skipped." Deliberately its own idempotency key
	(derived from the shift-close's own hub_uuid, not a fresh one) — a retried push_shift_close
	call (e.g. the POS Closing Entry succeeded but the response was lost) must never create a
	second Stock Entry for the same shift.

	Only ever consumes raw materials (CLAUDE.md rule 4 - recipes/BOMs are master data, owned by
	ERPNext, never re-derived Hub-side; the Hub only ever tells us "this much of this item_code
	sold"). A dish with no active default BOM contributes nothing to the entry and is returned in
	missing_bom_items instead - a real, visible report, not a silently dropped ingredient."""
	stock_hub_uuid = f"{payload['hub_uuid']}-stock"
	existing = frappe.db.get_value("Stock Entry", {"hub_uuid": stock_hub_uuid}, "name")
	if existing:
		return existing, []

	sold_items = payload.get("sold_items") or []
	if not sold_items:
		return None, []

	raw_material_qty = {}  # item_code -> total qty to deplete
	missing_bom_items = []
	for sold in sold_items:
		bom_name = frappe.db.get_value("BOM", {"item": sold["item_code"], "is_active": 1, "is_default": 1}, "name")
		if not bom_name:
			missing_bom_items.append(sold)
			continue
		bom = frappe.get_doc("BOM", bom_name)
		batch_qty = flt(bom.quantity) or 1
		ratio = flt(sold["qty"]) / batch_qty
		for bom_item in bom.items:
			raw_material_qty[bom_item.item_code] = raw_material_qty.get(bom_item.item_code, 0) + flt(bom_item.qty) * ratio

	if not raw_material_qty:
		return None, missing_bom_items

	outlet_doc = frappe.get_doc("Outlet", payload["outlet"])
	entry = frappe.new_doc("Stock Entry")
	entry.stock_entry_type = "Material Issue"
	entry.company = outlet_doc.company
	# Ticket P3-33: stock posts on the shift's business date, even when the shift closed after midnight.
	entry.posting_date = payload.get("business_date") or _hub_dt(payload["closed_at"]).date()
	entry.hub_uuid = stock_hub_uuid
	for item_code, qty in raw_material_qty.items():
		entry.append("items", {"item_code": item_code, "qty": qty, "s_warehouse": outlet_doc.warehouse})

	try:
		entry.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("Stock Entry", {"hub_uuid": stock_hub_uuid}, "name")
		if raced:
			frappe.db.rollback()
			return raced, missing_bom_items
		raise err

	entry.submit()
	frappe.db.commit()
	return entry.name, missing_bom_items


@frappe.whitelist()
def push_kot_timing(payload) -> dict:
	"""Ticket P3-10, reworked scope: PRD's own 'KOT timestamps sync to ERPNext for reporting'
	requirement, delivered as a one-row-per-served-item append-only audit record — the KOT
	doctype used to be a live, versioned Restaurant-Order-linked mirror (a second implementation
	of live order state in Frappe, which CLAUDE.md rule 4 forbids); it's been reworked to the
	same idempotent-by-hub_uuid, Data-not-Link pattern every other Hub push already uses.
	Idempotent by hub_uuid like every other push."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("KOT", {"hub_uuid": payload["hub_uuid"]}, "name")
	if existing:
		return {"status": "ok", "name": existing, "already_existed": True}

	# The Hub's own Course type is lowercase snake_case (packages/api-types); KOT's Select options
	# are Title Case — same boundary-normalization convention as apply-changes.ts's ROLE_MAP, just
	# applied in the opposite direction (Hub -> Frappe here, Frappe -> Hub there).
	course_map = {"starter": "Starter", "main": "Main", "dessert": "Dessert", "other": "Other"}

	doc = frappe.get_doc(
		{
			"doctype": "KOT",
			"outlet": payload["outlet"],
			"restaurant_order": payload["order_id"],
			"line_item_id": payload["line_id"],
			"item_code": payload["item_code"],
			"item_name": payload["name"],
			"station": payload["station_id"],
			"qty": payload["qty"],
			"course": course_map.get(payload.get("course"), payload.get("course")),
			"fired_at": _hub_dt(payload["fired_at"]),
			"started_at": _hub_dt(payload["started_at"]) if payload.get("started_at") else None,
			"ready_at": _hub_dt(payload["ready_at"]) if payload.get("ready_at") else None,
			"served_at": _hub_dt(payload["served_at"]),
			"hub_uuid": payload["hub_uuid"],
		}
	)

	try:
		doc.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("KOT", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced, "already_existed": True}
		raise err

	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "name": doc.name, "already_existed": False}


@frappe.whitelist()
def push_guest_consent(payload) -> dict:
	"""Ticket P4-25 (DPDP): one consent grant or withdrawal from the guest app, recorded append-only
	and idempotent by hub_uuid like every other push. `granted` 0 is a withdrawal. The wording the
	guest saw is stored with the record, so what they agreed to is always recoverable."""
	if isinstance(payload, str):
		payload = json.loads(payload)
	_hub_guard(payload.get("outlet"))

	existing = frappe.db.get_value("Guest Consent", {"hub_uuid": payload["hub_uuid"]}, "name")
	if existing:
		return {"status": "ok", "name": existing, "already_existed": True}

	doc = frappe.get_doc(
		{
			"doctype": "Guest Consent",
			"outlet": payload["outlet"],
			"hub_uuid": payload["hub_uuid"],
			"restaurant_order": payload.get("order_id"),
			"phone": payload["phone"],
			"purpose": payload["purpose"],
			"granted": 1 if payload["granted"] else 0,
			"text_version": payload["text_version"],
			"consent_text": payload["consent_text"],
			"captured_at": _hub_dt(payload["captured_at"]),
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except Exception as err:
		raced = frappe.db.get_value("Guest Consent", {"hub_uuid": payload["hub_uuid"]}, "name")
		if raced:
			frappe.db.rollback()
			return {"status": "ok", "name": raced, "already_existed": True}
		raise err

	doc.submit()
	frappe.db.commit()
	return {"status": "ok", "name": doc.name, "already_existed": False}


MAX_BATCH_RECORDS = 100


@frappe.whitelist()
def push_batch(kind: str, records) -> dict:
	"""Ticket P3-01: one request carries many records ("sent in batches through one bulk endpoint,
	not one request per record") so a busy shift costs Frappe a handful of requests instead of
	hundreds. Each record is run through the SAME idempotent per-record push as before - so retrying
	a half-processed batch is safe (hub_uuid dedupes) - and each is isolated: one bad record is
	reported as an error in its own slot and rolled back, it never fails its neighbours (the
	Hub's dead-letter handling, P3-37, counts per record from these results).

	Returns {"results": [...]} aligned index-for-index with `records`; each result is the per-record
	push's own dict, or {"status": "error", "message": ...}."""
	_hub_guard()
	if isinstance(records, str):
		records = json.loads(records)
	handler = _BATCH_HANDLERS.get(kind)
	if handler is None:
		frappe.throw(f"Unknown sync kind: {kind}", frappe.ValidationError)
	if len(records) > MAX_BATCH_RECORDS:
		frappe.throw(f"At most {MAX_BATCH_RECORDS} records per batch", frappe.ValidationError)

	results = []
	for record in records:
		try:
			results.append(handler(record))
		except Exception as err:
			frappe.db.rollback()
			message = str(err) or type(err).__name__
			frappe.log_error(title=f"push_batch {kind} record failed", message=frappe.get_traceback())
			results.append({"status": "error", "message": message})
	return {"results": results}


_BATCH_HANDLERS = {
	"payment": push_pos_invoice,
	"refund": push_refund,
	"void": push_void_log,
	"availability": push_availability_log,
	"shift_open": push_shift_open,
	"shift_close": push_shift_close,
	"kot_timing": push_kot_timing,
	"pin_reset": push_pin_reset,
	"guest_consent": push_guest_consent,
}

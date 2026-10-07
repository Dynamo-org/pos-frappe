# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P3-13: the three fields a menu item needs beyond what core ERPNext's Item doctype
# already has, so it can be entered entirely through the native Item form (CLAUDE.md rule 4) and
# still carry everything the Hub's menu sync (P1-26/P3-03) and the KDS board (P2-06) need.
# create_custom_fields() is idempotent (Frappe's own utility skips a field that already exists
# with the same definition), run from hooks.py's after_migrate so every environment gets these
# fields on `bench migrate` without a manual fixtures import step.

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	# Ticket P3-06's own open question — "does this ERPNext v16 site use POS Invoice + POS
	# Closing, or Sales Invoice directly" — is answered empirically by this bench's own POS
	# Settings.invoice_type, which is "Sales Invoice": these two fields go on Sales Invoice, not
	# POS Invoice, matching the actual configured behaviour rather than an assumption.
	"Sales Invoice": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "customer",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02: the Hub-generated idempotency key this invoice was synced with — a retried push checks this first, never creating a second invoice.",
		},
		{
			"fieldname": "hub_invoice_number",
			"label": "Hub Invoice Number",
			"fieldtype": "Data",
			"insert_after": "hub_uuid",
			"read_only": 1,
			"description": "Ticket P3-09: the Hub-owned per-outlet series number (e.g. KOR/26-27/00001) — final at print, distinct from this document's own Frappe naming series.",
		},
		{
			"fieldname": "verification_status",
			"label": "Verification Status",
			"fieldtype": "Select",
			"options": "Auto-confirmed\nPending Verification\nManually Verified\nMismatch Flagged",
			"insert_after": "hub_invoice_number",
			"read_only": 1,
			"description": "Ticket P4-18: cash is always Auto-confirmed (a real, in-hand exchange, no async step); the other three values are for the offline-UPI flow (P4-14/P4-15), not built yet.",
		},
		{
			"fieldname": "gateway_transaction_id",
			"label": "Gateway Transaction ID / RRN",
			"fieldtype": "Data",
			"insert_after": "verification_status",
			"read_only": 1,
			"description": "Ticket P4-18: null for cash. Populated by whichever payment gateway integration lands with P4-12/13.",
		},
		{
			"fieldname": "processed_by",
			"label": "Processed By",
			"fieldtype": "Link",
			"options": "Staff PIN",
			"insert_after": "gateway_transaction_id",
			"read_only": 1,
			"description": "Ticket P4-18: the staff member who recorded this payment on the Hub — same 'Hub staff id is the Staff PIN docname' convention as Void Log's requested_by/approved_by.",
		},
		{
			"fieldname": "hub_device",
			"label": "Hub Device",
			"fieldtype": "Link",
			"options": "Device",
			"insert_after": "processed_by",
			"read_only": 1,
			"description": "Ticket P4-18: which paired device processed this payment — resolved from the Hub's own credential_id, same as Void Log's device field.",
		},
		{
			"fieldname": "restaurant_order",
			"label": "Restaurant Order",
			"fieldtype": "Data",
			"insert_after": "hub_device",
			"read_only": 1,
			"description": "Ticket P4-10: the Hub's own order UUID string (Data, not a Link — orders are never mirrored into Frappe), same convention as KOT/Void Log's own restaurant_order field. A split bill produces more than one Sales Invoice sharing this same value.",
		},
		{
			"fieldname": "customer_business_name",
			"label": "Customer Business Name",
			"fieldtype": "Data",
			"insert_after": "tax_id",
			"read_only": 1,
			"description": "Ticket P4-19: an optional B2B customer's business name, captured at billing. The GSTIN itself goes on Sales Invoice's own native `tax_id` field (no custom field needed there) — `customer_name` stays fetch-only from the linked Customer record (always 'Walk-in Customer' here, per this project's own convention of invoice-level custom fields over creating a Customer record per bill), so the business name needs this separate field.",
		},
	],
	"Void Log": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "outlet",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to voids: the Hub-generated idempotency key this record was synced with.",
		},
	],
	"Refund Log": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "outlet",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to post-payment refunds (P4-16): the Hub-generated idempotency key this record was synced with — the refund's own local id, reused the same way a payment's own id is reused as its invoice push's hub_uuid.",
		},
	],
	"KOT": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "outlet",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to kitchen-timing records (P3-10, reworked scope): the Hub-generated idempotency key this record was synced with.",
		},
	],
	"Availability Log": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "outlet",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to 86/un-86 events: the Hub-generated idempotency key this record was synced with.",
		},
	],
	"POS Opening Entry": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "pos_profile",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to shift-open (P4-20): the Hub-generated idempotency key this entry was synced with.",
		},
		{
			"fieldname": "opened_by",
			"label": "Opened By",
			"fieldtype": "Link",
			"options": "Staff PIN",
			"insert_after": "hub_uuid",
			"read_only": 1,
			"description": "The real supervisor who opened the shift on the Hub — `user` stays the Hub's own service account (Administrator) since individual staff never get their own Frappe login (CLAUDE.md: Hub-to-Frappe auth is one shared credential, P1-14).",
		},
	],
	"POS Closing Entry": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "pos_opening_entry",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to shift-close (P4-21): the Hub-generated idempotency key this entry was synced with.",
		},
		{
			"fieldname": "closed_by",
			"label": "Closed By",
			"fieldtype": "Link",
			"options": "Staff PIN",
			"insert_after": "hub_uuid",
			"read_only": 1,
			"description": "The real supervisor who closed the shift on the Hub — see POS Opening Entry's `opened_by` for why `user` itself stays the service account.",
		},
	],
	"Stock Entry": [
		{
			"fieldname": "hub_uuid",
			"label": "Hub UUID",
			"fieldtype": "Data",
			"insert_after": "stock_entry_type",
			"unique": 1,
			"read_only": 1,
			"description": "Ticket P3-02's pattern applied to per-shift BOM stock depletion (P3-12): the Hub-generated idempotency key this entry was synced with.",
		},
	],
	"Item": [
		{
			"fieldname": "restaurant_station",
			"label": "Kitchen Station",
			"fieldtype": "Link",
			"options": "Station",
			"insert_after": "item_group",
			"description": "Which kitchen station prepares this item (ticket P2-06's KDS board).",
		},
		{
			"fieldname": "dietary_type",
			"label": "Veg / Non-veg",
			"fieldtype": "Select",
			"options": "\nVeg\nNon-veg",
			"insert_after": "restaurant_station",
		},
		{
			"fieldname": "modifier_groups",
			"label": "Modifier Groups",
			"fieldtype": "Table",
			"options": "Item Modifier Group",
			"insert_after": "dietary_type",
			"description": "Ticket P4-33: choices (size, spice, add-ons) the waiter picks when adding this item. Price changes are taxed at THIS item's own GST rate.",
		},
		{
			"fieldname": "is_complimentary",
			"label": "Complimentary (allows Rs.0 price)",
			"fieldtype": "Check",
			"insert_after": "standard_rate",
		},
		{
			"fieldname": "gst_hsn_code",
			"label": "HSN/SAC Code",
			"fieldtype": "Data",
			"insert_after": "item_tax_section_break",
			"description": "Ticket P4-3x (per-item GST): core ERPNext has no HSN field without India Compliance. "
			"If India Compliance is installed later and adds its own field under this same name, "
			"`create_custom_fields(update=True)` just updates this definition in place rather than "
			"conflicting with it — safe either way.",
		},
	],
}


def create_custom_fields_for_menu():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)

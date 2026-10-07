# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Device pairing (Security & Access §3, Tech Architecture §2/§8, ticket P1-12). Master data
# (Device, Pairing Code) is written only here, in Frappe — the Hub never mutates it directly
# (CLAUDE.md rule 4). The Hub calls consume_pairing_code() server-to-server when a device
# presents a scanned/typed code; generate_pairing_code() is what the Back Office Devices
# screen (Frontend Specs C.4) will call once it exists — that screen is a later ticket, so this
# is exercised via bench console / a direct API call for now.

import secrets

import frappe

from restaurant_pos.restaurant_pos.api.scope import require_hub_service, require_outlet
from frappe.utils import add_to_date, now_datetime

from restaurant_pos.restaurant_pos.doctype.outlet.outlet import ERP_MAPPING_FIELDS

# Human labels for ERP_MAPPING_FIELDS, for a failure message that names the specific missing
# link rather than a generic "not configured" (ticket P1-22's own wording).
_FIELD_LABELS = {
	"company": "Company",
	"branch": "Branch",
	"gstin_address": "GSTIN Address",
	"pos_profile": "POS Profile",
	"price_list": "Price List",
	"warehouse": "Warehouse",
	"invoice_series_prefix": "Invoice Series Prefix",
}

# 6 chars, A-Z/2-9, excluding O, 0, I, 1 (Validation & Conventions §3).
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
# Pairing code expiry — Validation & Conventions §3 marks this "pending client"; 15 minutes is
# a working default, not a decision.
DEFAULT_EXPIRY_MINUTES = 15


def _generate_code() -> str:
	code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
	# A collision is astronomically unlikely (32^6 possibilities) but cheap to guard anyway.
	if frappe.db.exists("Pairing Code", code):
		return _generate_code()
	return code


@frappe.whitelist()
def generate_pairing_code(outlet: str, device_type: str) -> dict:
	"""Manager-facing: create a one-time pairing code for a new device."""
	if not frappe.has_permission("Pairing Code", "create"):
		frappe.throw("Not permitted to generate a pairing code", frappe.PermissionError)
	# Never trust a client-sent outlet id without a permission check (CLAUDE.md rule 9).
	# Pairing a device to an outlet changes it: write, and branch scope.
	require_outlet(outlet, "write")

	code = _generate_code()
	expires_at = add_to_date(now_datetime(), minutes=DEFAULT_EXPIRY_MINUTES)

	doc = frappe.get_doc(
		{
			"doctype": "Pairing Code",
			"outlet": outlet,
			"device_type": device_type,
			"code": code,
			"expires_at": expires_at,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"code": code, "expires_at": str(expires_at)}

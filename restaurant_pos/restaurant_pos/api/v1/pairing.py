# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Sync API v1, device pairing half (Tech Architecture section 6: the Hub <-> restaurant_pos API is versioned by
# module, `api/v1/...` today; the Frappe app supports the current and the previous version, N-1). Called by the
# Hub, never by a browser, so it lives with the versioned Hub-facing methods and not with the Back Office's
# generate_pairing_code in api/pairing.py. Ticket P1-12 / P1-14 / P1-23.

import frappe
from frappe.utils import now_datetime

from restaurant_pos.restaurant_pos.api.pairing import _FIELD_LABELS
from restaurant_pos.restaurant_pos.api.scope import require_hub_service, require_outlet
from restaurant_pos.restaurant_pos.doctype.outlet.outlet import ERP_MAPPING_FIELDS


@frappe.whitelist()
def consume_pairing_code(code: str) -> dict:
	"""Called by the Hub, not by a browser, when a device presents a pairing code. Validates
	it, creates the Device record, and returns everything the device needs: a credential and
	its outlet's Hub URL and tier.

	Authenticated with the Hub's own pairing-issued credential (ticket P1-14, api/hub_credentials.py).
	"""
	require_hub_service()
	code = (code or "").strip().upper()
	if not code or not frappe.db.exists("Pairing Code", code):
		frappe.throw("This code doesn't exist.", frappe.DoesNotExistError)

	pairing_code = frappe.get_doc("Pairing Code", code)
	# Ticket P1-14: an Edge box's credential covers its own outlet only. Without this a box could consume a
	# pairing code issued for ANOTHER outlet and mint a device credential there.
	require_outlet(pairing_code.outlet, "read")
	if pairing_code.used_at:
		frappe.throw("This code has already been used.", frappe.ValidationError)
	if pairing_code.expires_at and now_datetime() > pairing_code.expires_at:
		frappe.throw("This code has expired. Ask your manager for a new one.", frappe.ValidationError)

	outlet = frappe.get_doc("Outlet", pairing_code.outlet)

	# Ticket P1-22: "Outlet must have Company, GSTIN address, POS Profile, Price List, Warehouse
	# and invoice series before any device can pair" — refused naming the specific missing
	# link(s), not a generic "not configured".
	missing = [_FIELD_LABELS[f] for f in ERP_MAPPING_FIELDS if not outlet.get(f)]
	if not outlet.hub_url:
		missing.append("Hub URL")
	if missing:
		frappe.throw(
			f"This outlet isn't fully configured yet — missing: {', '.join(missing)}.",
			frappe.ValidationError,
		)

	device = frappe.get_doc(
		{
			"doctype": "Device",
			"outlet": outlet.name,
			"device_type": pairing_code.device_type,
			"credential_id": frappe.generate_hash(length=32),
			"paired_at": now_datetime(),
		}
	)
	device.insert(ignore_permissions=True)

	pairing_code.used_at = now_datetime()
	pairing_code.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"device_credential": device.credential_id,
		"outlet": outlet.name,
		"device_type": pairing_code.device_type,
		"hub_url": outlet.hub_url,
		"hub_mode": outlet.hub_mode,
		"feature_flags": {},
	}

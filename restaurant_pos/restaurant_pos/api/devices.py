# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Backend for the Back Office's Devices screen (ticket P8.5-07 / P4-30, Frontend Specs C.4).
# `generate_pairing_code` already exists in pairing.py and is reused as-is (Frontend Specs C.4's
# QR is rendered client-side from that same code — no server-side QR payload is needed, see
# apps/back-office's DevicesScreen). This module adds the two operations pairing.py never
# needed until a manager-facing screen existed: listing paired devices and revoking one.
#
# Revoke deliberately does nothing beyond setting `revoked_at` (ticket P1-13's own kill-chain —
# sync/apply-changes.ts's revoked_devices pull, then socket disconnect + require-device-auth's
# next-request block — already reacts correctly to that field with zero Hub-side changes).

import frappe
from frappe.utils import now_datetime

from restaurant_pos.restaurant_pos.api.scope import require_outlet


@frappe.whitelist()
def list_devices(outlet: str) -> list:
	require_outlet(outlet, "read")
	if not frappe.has_permission("Device", "read"):
		frappe.throw("Not permitted to view devices", frappe.PermissionError)
	return frappe.get_list(
		"Device",
		filters={"outlet": outlet},
		fields=["name", "outlet", "device_type", "app_version", "paired_at", "last_seen_at", "revoked_at"],
		order_by="paired_at desc",
	)


@frappe.whitelist()
def revoke_device(device: str) -> dict:
	if not frappe.db.exists("Device", device):
		frappe.throw("Not permitted to revoke this device", frappe.PermissionError)  # same answer for missing and not-yours
	doc = frappe.get_doc("Device", device)
	if not frappe.has_permission(doc=doc, ptype="write"):
		frappe.throw("Not permitted to revoke this device", frappe.PermissionError)
	require_outlet(doc.outlet, "write")
	if not doc.revoked_at:
		doc.revoked_at = now_datetime()
		doc.save()
		frappe.db.commit()
	return {"name": doc.name, "revoked_at": str(doc.revoked_at)}

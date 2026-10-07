# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P1-23 (Tech Architecture section 6): the Hub <-> restaurant_pos sync API is versioned by module
# (`api/v1/...`, later `api/v2/...`) and this app supports the CURRENT version and the PREVIOUS one (N-1),
# because an Edge Hub that was offline can come back days later on an older build.
#
# Rules this file (and tests/verify_sync_compat.py, which enforces them) hold:
#   - a released version's method names, parameter names and result keys never change: they may only be added
#     to (a new optional parameter, a new result key). Anything else is a new version.
#   - when a new version N is added, N-1 stays for one release cycle, then is deleted from
#     SUPPORTED_SYNC_API_VERSIONS and from the package.
#   - every Hub call says which Hub build and which sync API version it speaks (X-Hub-Version,
#     X-Sync-Api-Version); `note_hub_call` records it on the caller's Hub Credential so an operator can see
#     which Hubs in the field are behind.

import frappe
from frappe.utils import add_to_date, now_datetime

CURRENT_SYNC_API_VERSION = 1
# Current and previous (N-1). Version 1 has no predecessor.
SUPPORTED_SYNC_API_VERSIONS = tuple(v for v in (CURRENT_SYNC_API_VERSION - 1, CURRENT_SYNC_API_VERSION) if v >= 1)

# A Hub Credential row is touched at most this often, so a chatty Hub does not write on every request.
TOUCH_INTERVAL_SECONDS = 300


class SyncApiVersionError(frappe.ValidationError):
	http_status_code = 426  # Upgrade Required


def _header(name: str) -> str | None:
	try:
		return frappe.get_request_header(name)
	except Exception:  # noqa: BLE001 - no request (a bench console call)
		return None


def note_hub_call() -> None:
	"""Called for every Hub-facing method. Refuses a sync API version this app no longer supports and records what
	the Hub reported about itself."""
	declared = _header("X-Sync-Api-Version")
	if declared and declared.isdigit() and int(declared) not in SUPPORTED_SYNC_API_VERSIONS:
		frappe.throw(
			f"Sync API version {declared} is not supported by this site (supported: {', '.join(map(str, SUPPORTED_SYNC_API_VERSIONS))}). Update the Hub.",
			SyncApiVersionError,
		)
	hub_version = _header("X-Hub-Version")
	if not (hub_version or declared):
		return
	name = frappe.db.get_value("Hub Credential", {"hub_user": frappe.session.user, "status": "Active"}, "name")
	if not name:
		return  # Administrator in a bench console, or a credential-less caller: nothing to record against
	last_seen = frappe.db.get_value("Hub Credential", name, "last_seen_at")
	if last_seen and last_seen > add_to_date(now_datetime(), seconds=-TOUCH_INTERVAL_SECONDS):
		return
	frappe.db.set_value(
		"Hub Credential",
		name,
		{
			"last_seen_at": now_datetime(),
			"hub_version": (hub_version or "")[:60] or None,
			"sync_api_version": int(declared) if declared and declared.isdigit() else None,
		},
		update_modified=False,
	)

# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P1-14 (Tech Architecture §14, Security & Access §3/§9a): the Hub's own credential to ERPNext is
# issued and revoked through PAIRING, not configured as a static API key.
#
#   1. An operator (System Manager) calls generate_hub_pairing_code(label, outlet?) - or presses the button
#      on the Hub Credential form. A one-time, 20-character code comes back ONCE (only its hash is kept).
#      With an outlet the credential is an Edge box's: it can only ever read and write that outlet. Without
#      one it is the Cloud Hub's: one credential for the whole tenant site.
#   2. The Hub exchanges the code (`node dist/cli/pair-hub.js`) through consume_hub_pairing_code. That is the
#      only guest-callable method here, so it is rate-limited, single-use and short-lived. Frappe creates a
#      dedicated service user (Hub Service role and nothing else, a password nobody knows), gives it a fresh
#      API key and secret, and returns them once. The Hub seals them in its own credential store.
#   3. The Hub rotates the secret on a schedule with rotate_hub_credential. The NEW secret is chosen by the Hub
#      and saved by it BEFORE this call, so a crash in between can never lock it out.
#   4. revoke_hub_credential disables the service user and removes its API key: the credential stops working
#      at once, on the next call.
#
# Nothing here returns an existing secret: a lost secret means a new pairing, never a lookup.

import hashlib
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, now_datetime

from restaurant_pos.restaurant_pos.api.scope import require_hub_service, require_org_wide
from restaurant_pos.restaurant_pos.setup.access_rights_setup import HUB_SERVICE_ROLE

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O, 0, I, 1 - same as device pairing codes
CODE_LENGTH = 20  # 32^20 = 100 bits: this code buys a Frappe credential, so it is NOT the 6-character device code
# How long a Hub pairing code lives. A working default (an operator pairs a server in one sitting); site config
# `restaurant_pos_hub_pairing_minutes` overrides it - configuration, not a decision.
DEFAULT_EXPIRY_MINUTES = 30
MIN_SECRET_LENGTH = 32
SERVICE_USER_DOMAIN = "hub-service.example.test"


def _normalise(code: str) -> str:
	return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _hash(code: str) -> str:
	return hashlib.sha256(_normalise(code).encode()).hexdigest()


def _display(code: str) -> str:
	return "-".join(code[i : i + 5] for i in range(0, CODE_LENGTH, 5))


def _require_operator():
	"""Issuing or revoking the credential the whole outlet fleet's sync runs on is an operator act: System
	Manager only, never a restaurant Owner/Manager, and never a branch-scoped one."""
	frappe.only_for("System Manager")
	require_org_wide("Managing Hub credentials")


def _as_administrator(fn):
	"""consume_hub_pairing_code runs as Guest (the Hub has no credential yet); the account work needs to run as the system."""
	previous = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return fn()
	finally:
		frappe.set_user(previous)


@frappe.whitelist()
def generate_hub_pairing_code(label: str, outlet: str | None = None) -> dict:
	_require_operator()
	label = (label or "").strip()
	if not label:
		frappe.throw(_("Say what this credential is for, e.g. the Cloud Hub server or an outlet's Edge box."), frappe.ValidationError)
	if outlet and not frappe.db.exists("Outlet", outlet):
		frappe.throw(_("Unknown outlet."), frappe.DoesNotExistError)

	code = "".join(secrets.choice(CODE_ALPHABET) for _unused in range(CODE_LENGTH))
	minutes = int(frappe.conf.get("restaurant_pos_hub_pairing_minutes") or DEFAULT_EXPIRY_MINUTES)
	expires_at = add_to_date(now_datetime(), minutes=minutes)
	doc = frappe.get_doc(
		{
			"doctype": "Hub Credential",
			"label": label,
			"outlet": outlet or None,
			"status": "Awaiting pairing",
			"pairing_code_hash": _hash(code),
			"pairing_expires_at": expires_at,
		}
	)
	doc.insert(ignore_permissions=True)  # the doctype grants no Create: only this method makes rows, and _require_operator() above is the gate
	frappe.db.commit()
	return {"name": doc.name, "code": _display(code), "expires_at": str(expires_at), "scope": outlet or "whole site"}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=10, seconds=60 * 10)  # per client IP and endpoint; the code itself is 100 bits
def consume_hub_pairing_code(code: str, hub_version: str | None = None, sync_api_version: int | str | None = None) -> dict:
	"""The Hub's one unauthenticated call. Single use; the response is the ONLY time the secret is ever shown."""

	def _consume():
		digest = _hash(code)
		name = frappe.db.get_value("Hub Credential", {"pairing_code_hash": digest, "status": "Awaiting pairing"}, "name")
		# one message for "no such code", "used" and "expired": do not tell a guesser which it was
		generic = _("This pairing code is not valid. Ask for a new one.")
		if not name:
			frappe.throw(generic, frappe.AuthenticationError)
		credential = frappe.get_doc("Hub Credential", name, for_update=True)
		if credential.status != "Awaiting pairing" or credential.pairing_code_hash != digest:
			frappe.throw(generic, frappe.AuthenticationError)
		if credential.pairing_expires_at and now_datetime() > credential.pairing_expires_at:
			frappe.throw(generic, frappe.AuthenticationError)

		user_name = f"{credential.name.lower()}@{SERVICE_USER_DOMAIN}"
		api_key = frappe.generate_hash(length=15)
		api_secret = secrets.token_hex(24)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": user_name,
				"first_name": f"Hub {credential.name}",
				"user_type": "System User",
				"send_welcome_email": 0,
				"enabled": 1,
				"api_key": api_key,
				"api_secret": api_secret,
				# nobody knows this password: the account signs in only by API token
				"new_password": secrets.token_urlsafe(40),
			}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		user.add_roles(HUB_SERVICE_ROLE)
		if credential.outlet:
			frappe.get_doc({"doctype": "User Permission", "user": user.name, "allow": "Outlet", "for_value": credential.outlet, "apply_to_all_doctypes": 1}).insert(
				ignore_permissions=True
			)

		frappe.flags.hub_credential_lifecycle = True
		try:
			credential.status = "Active"
			credential.hub_user = user.name
			credential.paired_at = now_datetime()
			credential.last_rotated_at = credential.paired_at
			credential.pairing_code_hash = None
			credential.pairing_expires_at = None
			credential.hub_version = (hub_version or "")[:60] or None
			credential.sync_api_version = int(sync_api_version) if str(sync_api_version or "").isdigit() else None
			credential.save(ignore_permissions=True)
		finally:
			frappe.flags.hub_credential_lifecycle = False
		frappe.db.commit()
		return {
			"credential": credential.name,
			"api_key": api_key,
			"api_secret": api_secret,
			"outlet": credential.outlet,
			"scope": credential.outlet or "whole site",
		}

	return _as_administrator(_consume)


def _own_credential():
	"""The Hub Credential row of the account making this call; refuses anything but an active Hub account."""
	require_hub_service()
	name = frappe.db.get_value("Hub Credential", {"hub_user": frappe.session.user, "status": "Active"}, "name")
	if not name:
		frappe.throw(_("This account is not an active Hub credential."), frappe.PermissionError)
	return frappe.get_doc("Hub Credential", name, for_update=True)


@frappe.whitelist()
def rotate_hub_credential(new_secret: str) -> dict:
	"""The Hub chose `new_secret` and has already saved it. From this call on the old secret is dead."""
	credential = _own_credential()
	new_secret = (new_secret or "").strip()
	if len(new_secret) < MIN_SECRET_LENGTH or not new_secret.isalnum():
		frappe.throw(_("The new secret must be at least {0} letters and digits.").format(MIN_SECRET_LENGTH), frappe.ValidationError)
	user = frappe.get_doc("User", credential.hub_user)
	user.api_secret = new_secret
	user.save(ignore_permissions=True)
	frappe.flags.hub_credential_lifecycle = True
	try:
		credential.last_rotated_at = now_datetime()
		credential.save(ignore_permissions=True)
	finally:
		frappe.flags.hub_credential_lifecycle = False
	frappe.db.commit()
	return {"credential": credential.name, "rotated_at": str(credential.last_rotated_at)}


@frappe.whitelist()
def rotate_outlet_push_secret(outlet: str) -> dict:
	"""Ticket P3-27: rotates the secret ERPNext signs this outlet's Hub pushes with. The Hub learns the new one on its next
	pull (a change hint is queued for it right away) and keeps accepting the previous one for an hour. Operator only,
	like every other Hub credential action; the secret itself is never returned."""
	_require_operator()
	if not frappe.db.exists("Outlet", outlet):
		frappe.throw(_("Unknown outlet."), frappe.DoesNotExistError)
	from restaurant_pos.restaurant_pos.api.hub_push import bump_master_version, rotate_push_secret, send_changed_hint

	rotate_push_secret(outlet)
	bump_master_version(outlet)
	frappe.get_doc(
		{"doctype": "Activity Log", "subject": f"Hub push secret of outlet {outlet} rotated", "reference_doctype": "Outlet", "reference_name": outlet, "status": "Success"}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.enqueue("restaurant_pos.restaurant_pos.api.hub_push.send_changed_hint", queue="short", outlet=outlet, enqueue_after_commit=True)
	return {"outlet": outlet, "rotated": True}


@frappe.whitelist()
def revoke_hub_credential(name: str) -> dict:
	"""Stops the credential working at once: the service user is disabled and its API key removed."""
	_require_operator()
	if not frappe.db.exists("Hub Credential", name):
		frappe.throw(_("Unknown Hub credential."), frappe.DoesNotExistError)
	credential = frappe.get_doc("Hub Credential", name, for_update=True)
	if credential.status == "Revoked":
		return {"credential": name, "status": "Revoked", "already": True}

	if credential.hub_user and frappe.db.exists("User", credential.hub_user):
		frappe.db.set_value("User", credential.hub_user, {"enabled": 0, "api_key": None}, update_modified=True)
		frappe.clear_cache(user=credential.hub_user)
	frappe.flags.hub_credential_lifecycle = True
	try:
		credential.status = "Revoked"
		credential.revoked_at = now_datetime()
		credential.pairing_code_hash = None
		credential.save()
	finally:
		frappe.flags.hub_credential_lifecycle = False
	frappe.get_doc(
		{
			"doctype": "Activity Log",
			"subject": f"Hub credential {name} revoked ({credential.label})",
			"reference_doctype": "Hub Credential",
			"reference_name": name,
			"status": "Success",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return {"credential": name, "status": "Revoked"}

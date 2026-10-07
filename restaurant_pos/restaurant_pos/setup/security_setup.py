# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

# Ticket P4-31 (Security & Access section 9): two-factor sign-in for the roles that can change money,
# menus, staff and access - Owner/Manager and System Manager. Frappe's own 2FA, configured, not rebuilt:
# a Role flag (`two_factor_auth`) says which roles need it and System Settings turns the machinery on, so
# every OTHER Desk user is unaffected unless someone configures them. The second factor is an authenticator
# app (TOTP) - it needs no SMS/email gateway, which this project has not chosen.
#
# What it never touches: the Hub and any API-key client. Token authentication has no interactive sign-in,
# so it is outside 2FA by design (and the Hub account is guarded by role + outlet scope instead, api/scope.py).
#
# Safety valve for development benches: set `restaurant_pos_skip_2fa_enforcement: 1` in the site config and
# nothing here is changed (the first admin on a fresh dev site would otherwise have to enrol an
# authenticator before they could log in). Production sites never set it.

import frappe

from restaurant_pos.branding import PRODUCT_NAME

ADMIN_ROLES_FOR_2FA = ("Owner/Manager", "System Manager")
TWO_FACTOR_METHOD = "OTP App"


def keep_two_factor_scoped_to_admin_roles() -> None:
	"""Frappe's System Settings flags the catch-all "All" role for 2FA the moment 2FA is switched on, which
	would force EVERY user - waiter included - through a second factor. Only the admin roles should be
	challenged ("other Desk users are unaffected unless configured"), so the catch-all is switched back off.
	Re-run by a System Settings on_update hook, so toggling the setting off and on again in Desk cannot
	silently widen it."""
	if frappe.db.get_value("Role", "All", "two_factor_auth"):
		frappe.db.set_value("Role", "All", "two_factor_auth", 0)
		frappe.clear_cache()


def reassert_admin_only_two_factor(doc=None, method=None) -> None:
	"""System Settings on_update: if enforcement is active (an admin role is flagged), keep it admin-only."""
	if frappe.db.get_value("Role", ADMIN_ROLES_FOR_2FA[0], "two_factor_auth"):
		keep_two_factor_scoped_to_admin_roles()


def enforce_admin_two_factor(force: bool = False) -> dict:
	"""Turns 2FA on for the admin roles. `force` ignores the dev skip flag (used by the verification
	test). Idempotent; returns what it set."""
	if not force and frappe.conf.get("restaurant_pos_skip_2fa_enforcement"):
		return {"skipped": True}

	for role in ADMIN_ROLES_FOR_2FA:
		if frappe.db.exists("Role", role):
			frappe.db.set_value("Role", role, "two_factor_auth", 1)

	settings = frappe.get_doc("System Settings")
	settings.enable_two_factor_auth = 1
	settings.two_factor_method = TWO_FACTOR_METHOD
	settings.otp_issuer_name = PRODUCT_NAME
	settings.save(ignore_permissions=True)
	keep_two_factor_scoped_to_admin_roles()
	frappe.db.commit()
	frappe.clear_cache()
	return {"skipped": False, "roles": list(ADMIN_ROLES_FOR_2FA), "method": TWO_FACTOR_METHOD}

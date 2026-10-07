# Copyright (c) 2026, the project contributors
# For license information, please see license.txt

"""Ticket P4-26: the name shown on the login page.

Only fills what is blank or still a stock Frappe default - a name an administrator has set in Website Settings is
never overwritten.
The colours and the logo tile come from public/css/web.css (aurora_ui's own tokens)."""

import frappe

from restaurant_pos.branding import PRODUCT_NAME

PLACEHOLDER_LOGO = "/assets/restaurant_pos/images/logo.svg"  # placeholder mark pending P0-23
STOCK_NAMES = ("Frappe", "ERPNext", "Frappe Framework")


def ensure_login_branding():
	current = frappe.db.get_single_value("Website Settings", "app_name")
	if not current or current in STOCK_NAMES:
		frappe.db.set_single_value("Website Settings", "app_name", PRODUCT_NAME)
	logo = frappe.db.get_single_value("Website Settings", "app_logo")
	if not logo or "erpnext-logo" in logo or "frappe-framework-logo" in logo:
		frappe.db.set_single_value("Website Settings", "app_logo", PLACEHOLDER_LOGO)
